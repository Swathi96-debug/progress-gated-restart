"""
Unit and integration tests for restart policies, adapters, and the restart controller.
Validates progress detection, immediate fixed-threshold triggers, counter resets,
process changes, cooldowns, and non-overlapping restarts.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from experiment.adapters import ElasticBeanstalkRestartAdapter, SimulatedRestartAdapter
from experiment.controller import RestartController
from experiment.models import DecisionType, PolicyConfig, ProgressObservation
from experiment.policies import FixedThresholdPolicy, NoRestartPolicy, ProgressGatedPolicy


@pytest.fixture
def config() -> PolicyConfig:
    return PolicyConfig(
        latency_threshold_ms=200.0,
        observation_interval_seconds=0.5,
        hard_deadline_seconds=2.0,
        cooldown_seconds=3.0,
        recovery_consecutive_successes=2,
        healthy_latency_threshold_ms=100.0,
    )


# ==============================================================================
# 1. NoRestartPolicy (Control Mode) Tests
# ==============================================================================

def test_no_restart_policy_never_restarts(config: PolicyConfig) -> None:
    policy = NoRestartPolicy(config)
    now = time.time()

    # Normal observation
    obs_normal = ProgressObservation(
        timestamp=now,
        iso_timestamp="2026-10-27T12:00:00",
        completion_counter=10,
        is_healthy=True,
        fault_state="normal",
        probe_latency_ms=50.0,
    )
    decision = policy.evaluate(obs_normal, now)
    assert decision.decision == DecisionType.NO_ACTION

    # Prolonged severe failure and high latency: still NO_ACTION (control mode)
    for offset in [1.0, 5.0, 10.0, 20.0]:
        obs_degraded = ProgressObservation(
            timestamp=now + offset,
            iso_timestamp=f"2026-10-27T12:00:{offset}",
            completion_counter=10,
            is_healthy=False,
            fault_state="hard_failure",
            probe_latency_ms=500.0,
        )
        decision = policy.evaluate(obs_degraded, now + offset)
        assert decision.decision == DecisionType.NO_ACTION
        assert policy.is_currently_degraded is True


def test_no_restart_policy_recovery_requires_n_consecutive_healthy_probes_post_degradation(config: PolicyConfig) -> None:
    """
    Verifies that NoRestartPolicy does NOT declare recovery early on 1 probe if there were
    healthy probes before degradation. It must require exactly N consecutive healthy probes
    after degradation has occurred.
    """
    policy = NoRestartPolicy(config)
    t = 1000.0

    # 1. Server starts healthy: 4 consecutive healthy probes before degradation
    for i in range(4):
        obs = ProgressObservation(
            timestamp=t + i * 0.5,
            iso_timestamp=f"2026-10-27T12:00:{i}",
            completion_counter=10 + i,
            is_healthy=True,
            fault_state="normal",
            probe_latency_ms=30.0,
        )
        dec = policy.evaluate(obs, t + i * 0.5)
        assert dec.decision == DecisionType.NO_ACTION
        # Not degraded yet, so recovery should not be detected
        assert policy.is_currently_degraded is False

    # 2. Degradation occurs
    t_deg = t + 2.5
    obs_deg = ProgressObservation(
        timestamp=t_deg,
        iso_timestamp="2026-10-27T12:00:10",
        completion_counter=14,
        is_healthy=True,
        fault_state="normal",
        probe_latency_ms=250.0,  # > 200.0ms threshold
    )
    dec = policy.evaluate(obs_deg, t_deg)
    assert dec.decision == DecisionType.NO_ACTION
    assert policy.is_currently_degraded is True
    # Crucial: consecutive healthy counter must be reset to 0 upon degradation!
    assert policy.consecutive_healthy_probes == 0

    # 3. First healthy probe after degradation (requires 2 consecutive)
    t_rec1 = t_deg + 0.5
    obs_rec1 = ProgressObservation(
        timestamp=t_rec1,
        iso_timestamp="2026-10-27T12:00:11",
        completion_counter=15,
        is_healthy=True,
        fault_state="normal",
        probe_latency_ms=30.0,  # <= 100ms
    )
    dec1 = policy.evaluate(obs_rec1, t_rec1)
    # Must NOT recover yet (only 1 healthy probe post-degradation, config requires 2)
    assert dec1.decision == DecisionType.NO_ACTION
    assert policy.is_currently_degraded is True
    assert policy.consecutive_healthy_probes == 1

    # 4. Second consecutive healthy probe after degradation
    t_rec2 = t_deg + 1.0
    obs_rec2 = ProgressObservation(
        timestamp=t_rec2,
        iso_timestamp="2026-10-27T12:00:12",
        completion_counter=16,
        is_healthy=True,
        fault_state="normal",
        probe_latency_ms=30.0,  # <= 100ms
    )
    dec2 = policy.evaluate(obs_rec2, t_rec2)
    # Now recovery must be validated
    assert dec2.decision == DecisionType.RECOVERY_DETECTED
    assert policy.is_currently_degraded is False
    assert policy.consecutive_healthy_probes == 2


# ==============================================================================
# 2. FixedThresholdPolicy Tests
# ==============================================================================

def test_fixed_threshold_triggers_immediately_at_latency_threshold(config: PolicyConfig) -> None:
    """
    Fixed-threshold policy: when the latency trigger is met, request a restart immediately,
    without waiting for the progress observation deadline.
    """
    policy = FixedThresholdPolicy(config)
    t0 = 1000.0

    # Normal observation: below latency threshold (50ms <= 200ms)
    obs0 = ProgressObservation(
        timestamp=t0,
        iso_timestamp="2026-10-27T12:00:00",
        completion_counter=5,
        is_healthy=True,
        fault_state="normal",
        probe_latency_ms=50.0,
    )
    assert policy.evaluate(obs0, t0).decision == DecisionType.NO_ACTION

    # Latency threshold crossed immediately (350ms > 200ms)
    t1 = 1000.1  # Only 100ms later, far before hard_deadline_seconds (2.0s)
    obs1 = ProgressObservation(
        timestamp=t1,
        iso_timestamp="2026-10-27T12:00:00.1",
        completion_counter=5,
        is_healthy=True,
        fault_state="normal",
        probe_latency_ms=350.0,
    )
    dec1 = policy.evaluate(obs1, t1)

    # Crucial assertion: FixedThreshold triggers immediately upon crossing latency threshold
    assert dec1.decision == DecisionType.INITIATE_RESTART
    assert "Fixed threshold triggered immediately" in dec1.reason


def test_fixed_threshold_enforces_cooldown(config: PolicyConfig) -> None:
    policy = FixedThresholdPolicy(config)
    t_restart = 1000.0
    policy.record_restart_executed(t_restart)

    # During cooldown (cooldown_seconds=3.0)
    t_during_cooldown = 1001.5
    obs = ProgressObservation(
        timestamp=t_during_cooldown,
        iso_timestamp="2026-10-27T12:00:01.5",
        completion_counter=0,
        is_healthy=False,
        fault_state="hard_failure",
        probe_latency_ms=500.0,
    )
    dec = policy.evaluate(obs, t_during_cooldown)
    assert dec.decision == DecisionType.COOLDOWN_ACTIVE


# ==============================================================================
# 3. ProgressGatedPolicy Tests
# ==============================================================================

def test_progress_gated_withholds_restart_and_avoids_restart_on_natural_recovery(config: PolicyConfig) -> None:
    """
    When latency trigger is met, progress-gated policy observes progress.
    While work is progressing (delta > 0), restart is withheld.
    When the server naturally recovers, RECOVERY_DETECTED is returned and NO restart occurs.
    """
    policy = ProgressGatedPolicy(config)
    t0 = 1000.0

    # Initial baseline
    obs0 = ProgressObservation(
        timestamp=t0,
        iso_timestamp="2026-10-27T12:00:00",
        completion_counter=100,
        is_healthy=True,
        fault_state="normal",
        probe_latency_ms=50.0,
    )
    policy.evaluate(obs0, t0)

    # High latency (350ms > 200ms) but forward progress occurs (counter 100 -> 105)
    t1 = 1000.6  # Past observation interval (0.5s)
    obs1 = ProgressObservation(
        timestamp=t1,
        iso_timestamp="2026-10-27T12:00:00.6",
        completion_counter=105,  # delta = +5
        is_healthy=True,
        fault_state="normal",
        probe_latency_ms=350.0,
    )
    dec1 = policy.evaluate(obs1, t1)
    assert dec1.decision == DecisionType.OBSERVE_PROGRESS
    assert "Forward progress observed" in dec1.reason

    # Service naturally recovers: healthy probe 1 (latency 60ms <= 100ms)
    t2 = 1001.2
    obs2 = ProgressObservation(
        timestamp=t2,
        iso_timestamp="2026-10-27T12:00:01.2",
        completion_counter=110,
        is_healthy=True,
        fault_state="normal",
        probe_latency_ms=60.0,
    )
    dec2 = policy.evaluate(obs2, t2)
    assert dec2.decision == DecisionType.NO_ACTION

    # Healthy probe 2 (satisfies recovery_consecutive_successes=2)
    t3 = 1001.8
    obs3 = ProgressObservation(
        timestamp=t3,
        iso_timestamp="2026-10-27T12:00:01.8",
        completion_counter=115,
        is_healthy=True,
        fault_state="normal",
        probe_latency_ms=55.0,
    )
    dec3 = policy.evaluate(obs3, t3)
    assert dec3.decision == DecisionType.RECOVERY_DETECTED
    assert policy.has_recovered is True
    assert policy.is_currently_degraded is False


def test_progress_gated_restarts_on_confirmed_stall(config: PolicyConfig) -> None:
    """
    When latency trigger is met and progress is confirmed stalled (delta == 0
    over the observation interval), the policy initiates a restart.
    """
    policy = ProgressGatedPolicy(config)
    t0 = 1000.0

    # Initial baseline
    obs0 = ProgressObservation(
        timestamp=t0,
        iso_timestamp="2026-10-27T12:00:00",
        completion_counter=100,
        is_healthy=True,
        fault_state="normal",
        probe_latency_ms=50.0,
    )
    policy.evaluate(obs0, t0)

    # Initial degradation observed at t1
    t1 = 1000.1
    obs1 = ProgressObservation(
        timestamp=t1,
        iso_timestamp="2026-10-27T12:00:00.1",
        completion_counter=100,  # delta = 0
        is_healthy=True,
        fault_state="simulated_stalled_progress",
        probe_latency_ms=400.0,
    )
    dec1 = policy.evaluate(obs1, t1)
    # Within initial observation interval (0.1s < 0.5s), observes
    assert dec1.decision == DecisionType.OBSERVE_PROGRESS

    # Confirmed stall after observation interval (0.6s >= 0.5s)
    t2 = 1000.6
    obs2 = ProgressObservation(
        timestamp=t2,
        iso_timestamp="2026-10-27T12:00:00.6",
        completion_counter=100,  # Still 100! Zero work done!
        is_healthy=True,
        fault_state="simulated_stalled_progress",
        probe_latency_ms=400.0,
    )
    dec2 = policy.evaluate(obs2, t2)
    assert dec2.decision == DecisionType.INITIATE_RESTART
    assert "Confirmed stalled progress" in dec2.reason


def test_progress_gated_restarts_on_hard_deadline_expiration(config: PolicyConfig) -> None:
    """
    Even if slow progress occurs, if degradation persists continuously
    and reaches hard_deadline_seconds (2.0s), restart is triggered.
    """
    policy = ProgressGatedPolicy(config)
    t0 = 1000.0
    obs0 = ProgressObservation(
        timestamp=t0,
        iso_timestamp="2026-10-27T12:00:00",
        completion_counter=10,
        is_healthy=True,
        fault_state="normal",
        probe_latency_ms=50.0,
    )
    policy.evaluate(obs0, t0)

    # Degradation starts at t1
    t1 = 1000.5
    obs1 = ProgressObservation(
        timestamp=t1,
        iso_timestamp="2026-10-27T12:00:00.5",
        completion_counter=11,  # delta = +1
        is_healthy=True,
        fault_state="normal",
        probe_latency_ms=300.0,
    )
    assert policy.evaluate(obs1, t1).decision == DecisionType.OBSERVE_PROGRESS

    # Continues degraded past hard deadline (t2 = 1002.6, 2.1s >= 2.0s hard deadline)
    t2 = 1002.6
    obs2 = ProgressObservation(
        timestamp=t2,
        iso_timestamp="2026-10-27T12:00:02.6",
        completion_counter=12,  # slow progress
        is_healthy=True,
        fault_state="normal",
        probe_latency_ms=300.0,
    )
    dec2 = policy.evaluate(obs2, t2)
    assert dec2.decision == DecisionType.INITIATE_RESTART
    assert "hard deadline expired" in dec2.reason.lower()


def test_progress_gated_handles_counter_reset_cleanly(config: PolicyConfig) -> None:
    """
    Ensures that a counter drop (e.g. after restart or container reload)
    is never mistaken for new completed work nor immediately treated as a stall.
    """
    policy = ProgressGatedPolicy(config)
    t0 = 1000.0

    obs0 = ProgressObservation(
        timestamp=t0,
        iso_timestamp="2026-10-27T12:00:00",
        completion_counter=500,
        is_healthy=True,
        fault_state="normal",
        probe_latency_ms=40.0,
    )
    policy.evaluate(obs0, t0)

    # Server restarted: counter reset to 0
    t1 = 1001.0
    obs1 = ProgressObservation(
        timestamp=t1,
        iso_timestamp="2026-10-27T12:00:01",
        completion_counter=0,  # Dropped from 500 to 0
        is_healthy=True,
        fault_state="normal",
        probe_latency_ms=40.0,
    )
    dec1 = policy.evaluate(obs1, t1)
    assert obs1.is_counter_reset is True
    assert policy.last_counter == 0
    assert dec1.decision == DecisionType.NO_ACTION


def test_progress_gated_handles_process_change_cleanly(config: PolicyConfig) -> None:
    """
    Ensures PID changes re-establish baseline and are not misinterpreted as stalled work.
    """
    policy = ProgressGatedPolicy(config)
    t0 = 1000.0

    obs0 = ProgressObservation(
        timestamp=t0,
        iso_timestamp="2026-10-27T12:00:00",
        completion_counter=50,
        pid=1010,
        is_healthy=True,
        fault_state="normal",
        probe_latency_ms=40.0,
    )
    policy.evaluate(obs0, t0)

    # Process restarted: PID 2020, counter reset to 5
    t1 = 1001.0
    obs1 = ProgressObservation(
        timestamp=t1,
        iso_timestamp="2026-10-27T12:00:01",
        completion_counter=5,
        pid=2020,
        is_healthy=True,
        fault_state="normal",
        probe_latency_ms=40.0,
    )
    dec1 = policy.evaluate(obs1, t1)
    assert obs1.is_process_change is True
    assert policy.last_pid == 2020
    assert policy.last_counter == 5


def test_progress_gated_handles_probe_failure_without_false_stall(config: PolicyConfig) -> None:
    """
    Missing metrics or probe failure must never be automatically interpreted
    as proof of stalled progress.
    """
    policy = ProgressGatedPolicy(config)
    t0 = 1000.0
    obs0 = ProgressObservation(
        timestamp=t0,
        iso_timestamp="2026-10-27T12:00:00",
        completion_counter=50,
        is_healthy=True,
        fault_state="normal",
        probe_latency_ms=40.0,
    )
    policy.evaluate(obs0, t0)

    # Network glitch: probe fails, no completion counter
    t1 = 1001.0
    obs_fail = ProgressObservation(
        timestamp=t1,
        iso_timestamp="2026-10-27T12:00:01",
        completion_counter=None,
        is_healthy=False,
        probe_success=False,
        fault_state="unknown",
        probe_latency_ms=2000.0,
    )
    dec1 = policy.evaluate(obs_fail, t1)
    assert dec1.decision == DecisionType.OBSERVE_PROGRESS
    assert "withholding stall conclusion" in dec1.reason


# ==============================================================================
# 4. Restart Adapters Tests
# ==============================================================================

def test_simulated_restart_adapter() -> None:
    callback_called = False

    def on_restart() -> None:
        nonlocal callback_called
        callback_called = True

    adapter = SimulatedRestartAdapter(on_restart_callback=on_restart, simulated_delay_seconds=0.01)
    assert adapter.is_simulated is True
    assert adapter.adapter_type == "simulated"

    result = adapter.restart(reason="Testing simulated restart")
    assert result.success is True
    assert result.is_simulated is True
    assert result.adapter_type == "simulated"
    assert callback_called is True
    assert result.details["state_mutated"] is True


def test_elastic_beanstalk_restart_adapter_mocked_client() -> None:
    mock_boto = MagicMock()
    mock_boto.restart_app_server.return_value = {
        "ResponseMetadata": {"HTTPStatusCode": 200, "RequestId": "test-req-123"}
    }

    adapter = ElasticBeanstalkRestartAdapter(
        environment_name="my-test-eb-env",
        region_name="us-east-1",
        boto3_client=mock_boto,
    )
    assert adapter.is_simulated is False
    assert adapter.adapter_type == "aws_elastic_beanstalk"

    result = adapter.restart(reason="Testing AWS EB restart")
    assert result.success is True
    assert result.is_simulated is False
    assert result.adapter_type == "aws_elastic_beanstalk"
    mock_boto.restart_app_server.assert_called_once_with(EnvironmentName="my-test-eb-env")


def test_elastic_beanstalk_restart_adapter_handles_api_exception() -> None:
    mock_boto = MagicMock()
    mock_boto.restart_app_server.side_effect = RuntimeError("AWS rate limit exceeded")

    adapter = ElasticBeanstalkRestartAdapter(
        environment_name="my-test-eb-env",
        boto3_client=mock_boto,
    )
    result = adapter.restart(reason="Testing AWS exception handling")
    assert result.success is False
    assert "AWS Elastic Beanstalk RestartAppServer failed" in str(result.error_message)


# ==============================================================================
# 5. RestartController Guard Tests (No Duplicate/Concurrent Restarts)
# ==============================================================================

def test_restart_controller_prevents_duplicate_concurrent_restarts(config: PolicyConfig) -> None:
    policy = FixedThresholdPolicy(config)

    adapter = SimulatedRestartAdapter(simulated_delay_seconds=0.1)
    controller = RestartController(policy=policy, adapter=adapter)

    results = []

    def call_restart(idx: int) -> None:
        res = controller.trigger_restart(f"Concurrent attempt {idx}", current_time=time.time())
        results.append(res)

    t1 = threading.Thread(target=call_restart, args=(1,))
    t2 = threading.Thread(target=call_restart, args=(2,))

    t1.start()
    time.sleep(0.01)
    t2.start()

    t1.join()
    t2.join()

    # One call succeeded, one call was blocked / rejected as duplicate
    successful_runs = [r for r in results if r is not None]
    assert len(successful_runs) == 1
    assert controller.total_restarts_triggered == 1
