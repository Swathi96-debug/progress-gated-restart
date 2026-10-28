"""
Unit tests for the live AWS experiment runner (run_live_aws.py).
Tests safety opt-ins, dry-run guarantees, credential checks, and ensures
that AWS restart API acceptance is never conflated with confirmed application recovery.
"""

from __future__ import annotations

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from experiment.adapters import ElasticBeanstalkRestartAdapter
from experiment.models import DecisionType, PolicyConfig, ProgressObservation
from experiment.policies import FixedThresholdPolicy, ProgressGatedPolicy
from run_live_aws import (
    check_aws_credentials_available,
    perform_dry_run_checks,
    run_live_aws_trial,
    validate_live_configuration,
    validate_url,
)


def test_validate_url_enforces_http_https_scheme() -> None:
    assert validate_url("http://my-env.elasticbeanstalk.com") == "http://my-env.elasticbeanstalk.com"
    assert validate_url("https://my-env.elasticbeanstalk.com/") == "https://my-env.elasticbeanstalk.com"

    with pytest.raises(ValueError, match="Invalid URL scheme"):
        validate_url("ftp://my-env.elasticbeanstalk.com")

    with pytest.raises(ValueError, match="Network host location cannot be empty"):
        validate_url("http://")


def test_missing_allow_restarts_aborts_restart_policies() -> None:
    """Verifies that policies capable of restarting abort immediately without --allow-restarts."""
    for pol in ["fixed_threshold", "progress_gated"]:
        with pytest.raises(PermissionError, match="SAFETY ABORT.*--allow-restarts"):
            validate_live_configuration(
                base_url="http://my-env.elasticbeanstalk.com",
                environment_name="test-env",
                policy=pol,
                scenario="transient_latency",
                allow_restarts=False,
                allow_fault_injection=False,
                dry_run=False,
            )


def test_missing_allow_fault_injection_aborts_fault_scenarios() -> None:
    """Verifies that scenarios requiring fault injection abort without --allow-fault-injection."""
    with pytest.raises(PermissionError, match="SAFETY ABORT.*--allow-fault-injection"):
        validate_live_configuration(
            base_url="http://my-env.elasticbeanstalk.com",
            environment_name="test-env",
            policy="control",
            scenario="stalled_progress",
            allow_restarts=False,
            allow_fault_injection=False,
            dry_run=False,
        )


def test_control_policy_runs_without_restart_authorization_or_env_name() -> None:
    """Control policy only observes natural behavior and never requires restart permissions or environment names."""
    # Should not raise any exception
    validate_live_configuration(
        base_url="http://my-env.elasticbeanstalk.com",
        environment_name="",
        policy="control",
        scenario="transient_latency",
        allow_restarts=False,
        allow_fault_injection=False,
        dry_run=False,
    )


def test_dry_run_allows_inspection_without_opt_ins() -> None:
    """Dry-run mode is permitted even without --allow-restarts or --allow-fault-injection."""
    validate_live_configuration(
        base_url="http://my-env.elasticbeanstalk.com",
        environment_name="test-env",
        policy="progress_gated",
        scenario="stalled_progress",
        allow_restarts=False,
        allow_fault_injection=False,
        dry_run=True,
    )


def test_dry_run_never_invokes_restart_api() -> None:
    """
    Proves that perform_dry_run_checks queries endpoints and read-only AWS APIs
    without ever calling RestartAppServer or mutating state.
    """
    mock_boto_client = MagicMock()
    mock_boto_client.describe_environments.return_value = {
        "Environments": [{"Status": "Ready", "Health": "Green"}]
    }

    config = PolicyConfig(
        latency_threshold_ms=500.0,
        observation_interval_seconds=2.0,
        cooldown_seconds=60.0,
    )

    class MockResp:
        def __init__(self, data: bytes, status: int = 200):
            self.data = data
            self.status = status
        def read(self) -> bytes:
            return self.data
        def __enter__(self) -> MockResp:
            return self
        def __exit__(self, *args) -> None:
            pass

    def mock_urlopen(req, timeout=5.0):
        url = req.full_url
        if url.endswith("/health"):
            return MockResp(json.dumps({"status": "healthy"}).encode("utf-8"))
        elif url.endswith("/metrics"):
            return MockResp(json.dumps({"completion_counter": 42, "pid": 999}).encode("utf-8"))
        else:
            return MockResp(json.dumps({"project": "Test Project"}).encode("utf-8"))

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        result = run_live_aws_trial(
            base_url="http://mock-eb.us-east-1.elasticbeanstalk.com",
            environment_name="live-test-env",
            policy="progress_gated",
            scenario="transient_latency",
            dry_run=True,
            allow_restarts=False,
            boto3_client=mock_boto_client,
        )

    assert result["dry_run"] is True
    assert result["checks"]["root_ok"] is True
    assert result["checks"]["health_ok"] is True
    assert result["checks"]["metrics_ok"] is True
    assert "Ready" in result["checks"]["aws_env_status"]

    # Critical Assertion: RestartAppServer MUST NEVER be called during dry-run
    mock_boto_client.restart_app_server.assert_not_called()


def test_api_acceptance_is_not_treated_as_confirmed_recovery() -> None:
    """
    Proves that when RestartAppServer succeeds with HTTP 200, the controller and policy
    do NOT mark the application as recovered until healthy probes subsequently pass.
    """
    mock_boto_client = MagicMock()
    mock_boto_client.restart_app_server.return_value = {
        "ResponseMetadata": {"HTTPStatusCode": 200, "RequestId": "req-xyz-123"}
    }

    adapter = ElasticBeanstalkRestartAdapter(
        environment_name="my-env",
        region_name="us-east-1",
        boto3_client=mock_boto_client,
    )

    # 1. Execute restart via adapter
    restart_res = adapter.restart(reason="Threshold exceeded")
    assert restart_res.success is True
    assert restart_res.is_simulated is False
    assert restart_res.adapter_type == "aws_elastic_beanstalk"
    mock_boto_client.restart_app_server.assert_called_once_with(EnvironmentName="my-env")

    # 2. Check policy state: recording a restart marks system as degraded and unrecovered!
    config = PolicyConfig(
        latency_threshold_ms=500.0,
        observation_interval_seconds=2.0,
        cooldown_seconds=60.0,
        recovery_consecutive_successes=2,
        healthy_latency_threshold_ms=300.0,
    )
    policy = ProgressGatedPolicy(config)
    t0 = 1000.0
    policy.record_restart_executed(t0)

    assert policy.is_currently_degraded is True
    assert policy.has_recovered is False
    assert policy.consecutive_healthy_probes == 0

    # 3. First probe post-restart is healthy, but recovery requires 2 consecutive checks:
    obs1 = ProgressObservation(
        timestamp=t0 + 2.0,
        iso_timestamp="2026-10-27T12:00:02",
        completion_counter=10,
        is_healthy=True,
        fault_state="normal",
        probe_latency_ms=200.0,
    )
    dec1 = policy.evaluate(obs1, t0 + 2.0)
    # Recovery must NOT be declared on 1 probe:
    assert dec1.decision != DecisionType.RECOVERY_DETECTED
    assert policy.has_recovered is False

    # 4. Second probe post-restart is healthy: now recovery is validated!
    obs2 = ProgressObservation(
        timestamp=t0 + 4.0,
        iso_timestamp="2026-10-27T12:00:04",
        completion_counter=12,
        is_healthy=True,
        fault_state="normal",
        probe_latency_ms=190.0,
    )
    dec2 = policy.evaluate(obs2, t0 + 4.0)
    assert dec2.decision == DecisionType.RECOVERY_DETECTED
    assert policy.has_recovered is True


def test_cli_timing_parameters_properly_wired_to_components() -> None:
    """
    Verifies that all CLI timing parameters (probe_timeout, request_interval, work_delay,
    degraded_delay, observation_interval, hard_deadline, cooldown, thresholds)
    are actively passed and wired to the runner, controller, policy, and workload.
    """
    mock_boto = MagicMock()
    mock_boto.describe_environments.return_value = {
        "Environments": [{"Status": "Ready", "Health": "Green"}]
    }

    class MockResp:
        def __init__(self, data: bytes):
            self.data = data
        def read(self) -> bytes:
            return self.data
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    with patch("urllib.request.urlopen", return_value=MockResp(b'{"status": "healthy"}')), \
         patch("run_live_aws.check_aws_credentials_available", return_value=True), \
         patch("run_live_aws.format_report", return_value="Report OK"), \
         patch("run_live_aws.ExperimentRunner") as MockRunner:

        mock_instance = MagicMock()
        mock_instance.run.return_value = {
            "trial_id": "test-trial-123",
            "policy": "progress_gated",
            "scenario": "transient_latency",
            "total_requests": 10,
            "successful_requests": 10,
            "failed_requests": 0,
            "timed_out_requests": 0,
            "restart_count": 0,
            "simulated_restart_count": 0,
            "aws_restart_count": 0,
            "measured_recovery_time_seconds": 2.5,
            "downtime_seconds": 0.0,
            "recovered": True,
            "log_path": "logs/test.jsonl",
        }
        MockRunner.return_value = mock_instance

        run_live_aws_trial(
            base_url="http://mock-eb.elasticbeanstalk.com",
            environment_name="test-env",
            policy="progress_gated",
            scenario="transient_latency",
            duration_seconds=30.0,
            observation_interval_seconds=2.5,
            latency_threshold_ms=450.0,
            healthy_threshold_ms=250.0,
            recovery_consecutive_successes=4,
            cooldown_seconds=45.0,
            hard_deadline_seconds=20.0,
            probe_timeout_seconds=1.75,
            request_interval_seconds=0.35,
            default_work_delay_ms=35.0,
            degraded_delay_ms=650.0,
            fault_offset_seconds=5.0,
            fault_duration_seconds=10.0,
            allow_restarts=True,
            boto3_client=mock_boto,
        )

        MockRunner.assert_called_once()
        call_kwargs = MockRunner.call_args.kwargs

        # Verify runner receives base_url and custom probe/workload parameters
        assert call_kwargs["base_url"] == "http://mock-eb.elasticbeanstalk.com"
        assert call_kwargs["probe_timeout_seconds"] == 1.75
        assert call_kwargs["request_interval_seconds"] == 0.35
        assert call_kwargs["default_work_delay_ms"] == 35.0

        # Verify policy config passed to runner has all thresholds and timing
        policy_cfg = call_kwargs["config"]
        assert policy_cfg.observation_interval_seconds == 2.5
        assert policy_cfg.latency_threshold_ms == 450.0
        assert policy_cfg.healthy_latency_threshold_ms == 250.0
        assert policy_cfg.recovery_consecutive_successes == 4
        assert policy_cfg.cooldown_seconds == 45.0
        assert policy_cfg.hard_deadline_seconds == 20.0

        # Verify run() call received duration and latency schedule
        mock_instance.run.assert_called_once()
        run_kwargs = mock_instance.run.call_args.kwargs
        assert run_kwargs["duration_seconds"] == 30.0
        lat_sched = run_kwargs["latency_schedules"][0]
        assert lat_sched.degraded_delay_ms == 650.0
        assert lat_sched.offset_seconds == 5.0
        assert lat_sched.duration_seconds == 10.0


def test_controller_handles_failed_probes_during_container_reload_without_false_recovery() -> None:
    """
    Simulates a container reload where urllib throws URLError (Connection Refused / 502)
    during restart. Proves the controller reports probe_success=False, is_healthy=False,
    and policy does NOT declare recovery or corrupt stall evaluation.
    """
    from experiment.adapters import SimulatedRestartAdapter
    from experiment.controller import RestartController

    config = PolicyConfig(
        latency_threshold_ms=500.0,
        observation_interval_seconds=1.0,
        cooldown_seconds=30.0,
        recovery_consecutive_successes=2,
        hard_deadline_seconds=15.0,
    )
    policy = ProgressGatedPolicy(config)
    adapter = SimulatedRestartAdapter()
    controller = RestartController(
        policy=policy,
        adapter=adapter,
        probe_timeout_seconds=0.5,
    )

    # 1. Trigger initial degradation and restart
    obs_deg = ProgressObservation(
        timestamp=100.0,
        iso_timestamp="2026-10-27T12:00:00",
        completion_counter=5,
        is_healthy=True,
        fault_state="simulated_stalled_progress",
        probe_latency_ms=600.0,
    )
    # Trigger degradation
    policy.evaluate(obs_deg, 100.0)
    # Observe stall delta=0
    obs_stall = ProgressObservation(
        timestamp=101.5,
        iso_timestamp="2026-10-27T12:00:01",
        completion_counter=5,
        is_healthy=True,
        fault_state="simulated_stalled_progress",
        probe_latency_ms=650.0,
    )
    dec = policy.evaluate(obs_stall, 101.5)
    assert dec.decision == DecisionType.INITIATE_RESTART
    controller.trigger_restart(reason=dec.reason, current_time=101.5)

    assert policy.is_currently_degraded is True
    assert policy.has_recovered is False

    # 2. Mid-reload probe fails with URLError (simulating connection refused during container restart)
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
        probe_obs = controller.probe_server(base_url="http://mock-reloading-server.com")

    assert probe_obs.probe_success is False
    assert probe_obs.is_healthy is False
    assert "Connection refused" in (probe_obs.error_message or "")

    # 3. Feed failed probe into policy: verify NO false recovery and NO double-restart
    dec_mid = policy.evaluate(probe_obs, 102.5)
    # Must wait or observe, never falsely recover
    assert dec_mid.decision != DecisionType.RECOVERY_DETECTED
    assert dec_mid.decision != DecisionType.INITIATE_RESTART  # Still in cooldown
    assert policy.has_recovered is False


def test_restart_in_progress_lock_prevents_concurrent_restart_dispatch() -> None:
    """
    Verifies that RestartController's _restart_lock and _restart_in_progress flag
    block overlapping restart executions if multiple threads or rapid evaluations request restart.
    """
    from experiment.adapters import SimulatedRestartAdapter
    from experiment.controller import RestartController

    config = PolicyConfig(cooldown_seconds=0.0)  # zero cooldown to test the in-progress guard
    policy = ProgressGatedPolicy(config)
    adapter = SimulatedRestartAdapter(simulated_delay_seconds=0.05)
    controller = RestartController(
        policy=policy,
        adapter=adapter,
    )

    # Set controller's _restart_in_progress to True (as would happen during an active API call)
    controller._restart_in_progress = True

    # Attempt to execute restart while one is in progress
    res = controller.trigger_restart(reason="Concurrent trigger", current_time=200.0)
    assert res is None, "Should reject restart while another restart is in progress"
    assert len(adapter.restart_history) == 0


def test_aws_adapter_handles_client_error_cleanly() -> None:
    """
    Verifies that if AWS returns an error (e.g., botocore ClientError or connection timeout),
    the ElasticBeanstalkRestartAdapter captures it, returns success=False, and sets error details.
    """
    from botocore.exceptions import ClientError

    mock_boto = MagicMock()
    mock_boto.restart_app_server.side_effect = ClientError(
        {"Error": {"Code": "InvalidParameterValue", "Message": "Environment not found"}},
        "RestartAppServer",
    )

    adapter = ElasticBeanstalkRestartAdapter(
        environment_name="bad-env",
        region_name="us-east-1",
        boto3_client=mock_boto,
    )

    res = adapter.restart(reason="Fault test")
    assert res.success is False
    assert res.is_simulated is False
    assert "InvalidParameterValue" in (res.error_message or "")
    assert len(adapter.restart_history) == 1
    assert adapter.restart_history[0].success is False


def test_stalled_progress_aborts_without_configured_fault_token() -> None:
    """Verifies that stalled_progress cannot run if allow_fault_injection is set but token is missing."""
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(ValueError, match="SAFETY ABORT.*valid configured fault token"):
            validate_live_configuration(
                base_url="http://my-env.elasticbeanstalk.com",
                environment_name="test-env",
                policy="control",
                scenario="stalled_progress",
                allow_restarts=False,
                allow_fault_injection=True,
                fault_token="",
                dry_run=False,
            )


def test_conflicting_allow_and_prohibit_restarts_aborts() -> None:
    """Verifies that passing both --allow-restarts and --prohibit-restarts raises ValueError."""
    with pytest.raises(ValueError, match="Conflicting options"):
        validate_live_configuration(
            base_url="http://my-env.elasticbeanstalk.com",
            environment_name="test-env",
            policy="progress_gated",
            scenario="transient_latency",
            allow_restarts=True,
            prohibit_restarts=True,
            allow_fault_injection=False,
            dry_run=False,
        )


def test_transient_latency_validation_prohibited_restarts_intercepts_restart_attempt() -> None:
    """
    Proves that in mechanically enforced no-restart mode (prohibit_restarts=True),
    if a policy (such as fixed_threshold) attempts to trigger a restart during transient latency,
    the restart call is intercepted safely, zero AWS APIs are called, and the trial fails safely.
    """
    from experiment.adapters import ElasticBeanstalkRestartAdapter

    mock_boto = MagicMock()

    class MockResp:
        def __init__(self, data: bytes):
            self.data = data
        def read(self) -> bytes:
            return self.data
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    # We mock runner.run to simulate fixed_threshold evaluating high latency and calling adapter.restart()
    def mock_run_with_restart(duration_seconds, fault_schedules, latency_schedules):
        # Retrieve the adapter passed to ExperimentRunner
        runner_instance = mock_runner_cls.call_args.kwargs
        adapter = runner_instance["adapter"]
        # Trigger restart on adapter
        restart_res = adapter.restart(reason="Fixed-threshold latency exceeded 500ms")
        assert restart_res.success is False
        assert restart_res.adapter_type == "prohibited_no_restart"

        return {
            "trial_ids": ["test-prohibited-trial-1"],
            "policy": "fixed_threshold",
            "scenario": "transient_latency",
            "total_requests": 20,
            "successful_requests": 20,
            "failed_requests": 0,
            "success_rate_percent": 100.0,
            "latency_metrics": {"min_ms": 50.0, "mean_ms": 400.0, "median_ms": 400.0, "p90_ms": 750.0, "p95_ms": 750.0, "p99_ms": 750.0, "max_ms": 750.0},
            "restarts": {"total_unique_restarts": 0, "simulated_restarts_count": 0, "aws_restarts_count": 0},
            "measured_recovery_time_seconds": None,
            "estimated_downtime_seconds": 0.0,
            "log_path": "logs/test.jsonl",
        }

    with patch("urllib.request.urlopen", return_value=MockResp(b'{"status": "healthy"}')), \
         patch("run_live_aws.format_report", return_value="Report OK"), \
         patch("run_live_aws.ExperimentRunner") as mock_runner_cls:

        mock_inst = MagicMock()
        mock_inst.run.side_effect = mock_run_with_restart
        mock_runner_cls.return_value = mock_inst

        summary = run_live_aws_trial(
            base_url="http://mock-eb.elasticbeanstalk.com",
            environment_name="test-env",
            policy="fixed_threshold",
            scenario="transient_latency",
            prohibit_restarts=True,
            allow_restarts=False,
            boto3_client=mock_boto,
        )

    # Critical assertions:
    # 1. AWS restart_app_server was NEVER called
    mock_boto.restart_app_server.assert_not_called()
    # 2. Trial was marked as failed safety criteria
    assert summary["safety_violation"] is True
    assert summary["trial_failed"] is True
    assert "SAFETY VIOLATION" in summary["safety_violation_reason"]
    # 3. AWS restart count is 0
    assert summary["restarts"]["aws_restarts_count"] == 0


def test_transient_latency_validation_prohibited_restarts_succeeds_when_no_restart_requested() -> None:
    """
    Proves that transient-latency validation with prohibit_restarts=True succeeds
    when the policy does not request a restart.
    """
    mock_boto = MagicMock()

    class MockResp:
        def __init__(self, data: bytes):
            self.data = data
        def read(self) -> bytes:
            return self.data
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    with patch("urllib.request.urlopen", return_value=MockResp(b'{"status": "healthy"}')), \
         patch("run_live_aws.format_report", return_value="Report OK"), \
         patch("run_live_aws.ExperimentRunner") as mock_runner_cls:

        mock_inst = MagicMock()
        mock_inst.run.return_value = {
            "trial_ids": ["test-prohibited-trial-success"],
            "policy": "progress_gated",
            "scenario": "transient_latency",
            "total_requests": 20,
            "successful_requests": 20,
            "failed_requests": 0,
            "success_rate_percent": 100.0,
            "latency_metrics": {"min_ms": 50.0, "mean_ms": 100.0, "median_ms": 50.0, "p90_ms": 120.0, "p95_ms": 120.0, "p99_ms": 120.0, "max_ms": 120.0},
            "restarts": {"total_unique_restarts": 0, "simulated_restarts_count": 0, "aws_restarts_count": 0},
            "measured_recovery_time_seconds": 2.1,
            "estimated_downtime_seconds": 0.0,
            "log_path": "logs/test.jsonl",
        }
        mock_runner_cls.return_value = mock_inst

        summary = run_live_aws_trial(
            base_url="http://mock-eb.elasticbeanstalk.com",
            environment_name="test-env",
            policy="progress_gated",
            scenario="transient_latency",
            prohibit_restarts=True,
            allow_restarts=False,
            boto3_client=mock_boto,
        )

    mock_boto.restart_app_server.assert_not_called()
    assert summary.get("safety_violation", False) is False
    assert summary.get("trial_failed", False) is False
    assert summary["recovered"] is True


def test_interrupted_stalled_progress_reports_unrecovered_and_outputs_remediation(capsys) -> None:
    """
    Verifies that when a stalled_progress trial is interrupted by KeyboardInterrupt,
    the runner prints the emergency remediation instructions and UNRECOVERED status.
    """
    with patch("urllib.request.urlopen"), \
         patch("run_live_aws.check_aws_credentials_available", return_value=True), \
         patch("run_live_aws.ExperimentRunner") as mock_runner_cls:

        mock_inst = MagicMock()
        mock_inst.run.side_effect = KeyboardInterrupt("Interrupted")
        mock_runner_cls.return_value = mock_inst

        with pytest.raises(KeyboardInterrupt):
            run_live_aws_trial(
                base_url="http://mock-eb.elasticbeanstalk.com",
                environment_name="test-env",
                policy="progress_gated",
                scenario="stalled_progress",
                allow_restarts=True,
                allow_fault_injection=True,
                fault_token="test-secret-token",
            )

    captured = capsys.readouterr()
    assert "UNRECOVERED (Interrupted before completion)" in captured.out
    assert "EMERGENCY REMEDIATION: RESTORING APPLICATION SERVER" in captured.out
    assert "/experiment/fault" in captured.out
    assert "test-secret-token" in captured.out


def test_local_dry_run_without_environment_name_succeeds() -> None:
    """
    Verifies that a local dry-run against 127.0.0.1 or localhost succeeds without requiring
    an environment name, AWS credentials, or calling any AWS or mutating APIs.
    """
    class MockResp:
        def __init__(self, data: bytes):
            self.data = data
            self.status = 200
        def read(self) -> bytes:
            return self.data
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    def mock_urlopen(req, timeout=5.0):
        url = req.full_url
        if url.endswith("/health"):
            return MockResp(json.dumps({"status": "healthy"}).encode("utf-8"))
        elif url.endswith("/metrics"):
            return MockResp(json.dumps({"completion_counter": 15, "pid": 1234}).encode("utf-8"))
        else:
            return MockResp(json.dumps({"project": "Local Test"}).encode("utf-8"))

    mock_boto = MagicMock()

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        result = run_live_aws_trial(
            base_url="http://127.0.0.1:5000",
            environment_name="",
            policy="progress_gated",
            scenario="transient_latency",
            dry_run=True,
            boto3_client=mock_boto,
        )

    assert result["dry_run"] is True
    assert result["checks"]["root_ok"] is True
    assert result["checks"]["health_ok"] is True
    assert result["checks"]["metrics_ok"] is True
    assert result["checks"]["aws_env_status"] == "Skipped (Local Base URL)"

    # Verify zero AWS calls were made
    mock_boto.describe_environments.assert_not_called()
    mock_boto.restart_app_server.assert_not_called()


def test_local_dry_run_with_environment_name_skips_aws_unless_check_aws_set() -> None:
    """
    Verifies that for local URLs, describe_environments is skipped even if environment_name is provided,
    unless check_aws=True is explicitly set.
    """
    class MockResp:
        def __init__(self, data: bytes):
            self.data = data
            self.status = 200
        def read(self) -> bytes:
            return self.data
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    mock_boto = MagicMock()
    mock_boto.describe_environments.return_value = {
        "Environments": [{"Status": "Ready", "Health": "Green"}]
    }

    with patch("urllib.request.urlopen", return_value=MockResp(b'{"status": "healthy"}')):
        # 1. Without check_aws: should skip AWS call
        res_skipped = run_live_aws_trial(
            base_url="http://localhost:5000",
            environment_name="my-env",
            policy="progress_gated",
            scenario="transient_latency",
            dry_run=True,
            check_aws=False,
            boto3_client=mock_boto,
        )
        assert res_skipped["checks"]["aws_env_status"] == "Skipped (Local Base URL)"
        mock_boto.describe_environments.assert_not_called()

        # 2. With check_aws=True: calls describe_environments
        res_called = run_live_aws_trial(
            base_url="http://localhost:5000",
            environment_name="my-env",
            policy="progress_gated",
            scenario="transient_latency",
            dry_run=True,
            check_aws=True,
            boto3_client=mock_boto,
        )
        assert "Ready" in res_called["checks"]["aws_env_status"]
        mock_boto.describe_environments.assert_called_once_with(EnvironmentNames=["my-env"])


def test_live_run_without_environment_name_still_aborts() -> None:
    """
    Proves that for live runs (dry_run=False), safety checks are NOT weakened:
    an environment name is still strictly required.
    """
    with pytest.raises(ValueError, match="requires an explicit '--environment-name'"):
        validate_live_configuration(
            base_url="http://127.0.0.1:5000",
            environment_name="",
            policy="progress_gated",
            scenario="transient_latency",
            allow_restarts=True,
            allow_fault_injection=False,
            dry_run=False,
        )



