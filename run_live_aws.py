"""
Live AWS Elastic Beanstalk experiment runner for Progress-Gated Application-Server Restart.

SAFETY GUARANTEES:
1. EXPLICIT OPT-IN: Live restarts require an explicit '--allow-restarts' CLI flag.
   Without this flag, any policy that triggers restarts will abort before execution.
2. FAULT SAFETY: Live fault injection requires '--allow-fault-injection' and a valid token.
   Fault injection is strictly disabled by default.
3. DRY-RUN MODE: '--dry-run' inspects connectivity, health, and metrics without calling
   any restart API or injecting any faults.
4. NO SILENT SIMULATION: Never silently falls back to SimulatedRestartAdapter in live mode.
   Uses ElasticBeanstalkRestartAdapter with AWS Boto3 SDK.
5. ISOLATED PERMISSIONS: Control policy runs without requiring any AWS restart permissions.
6. IMMUTABLE LOGS: Every trial generates a unique ID and immutable log file.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from experiment.adapters import ElasticBeanstalkRestartAdapter, RestartAdapter
from experiment.analysis import analyze_experiment_log, format_report
from experiment.models import PolicyConfig
from experiment.policies import NoRestartPolicy
from experiment.runner import ExperimentRunner
from experiment.workload import FaultSchedule, LatencySpikeSchedule

logger = logging.getLogger("progress_gated_restart.live_aws")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def validate_url(url: str) -> str:
    """Validates that the provided URL has a valid HTTP/HTTPS scheme and network location."""
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Invalid URL scheme '{parsed.scheme}'. Base URL must begin with 'http://' or 'https://'.")
    if not parsed.netloc:
        raise ValueError(f"Invalid URL '{url}'. Network host location cannot be empty.")
    return url.rstrip("/")


def is_local_url(url: str) -> bool:
    """Checks whether the URL targets localhost, loopback, or local interface."""
    parsed = urllib.parse.urlparse(url.strip())
    hostname = (parsed.hostname or "").lower()
    return hostname in ("localhost", "127.0.0.1", "0.0.0.0", "::1") or hostname.endswith(".local")


def check_aws_credentials_available() -> bool:
    """
    Checks if AWS credentials can be discovered via standard Boto3/Botocore credential resolution.
    Does NOT invoke AWS APIs or print secret credentials.
    """
    try:
        import boto3
        session = boto3.Session()
        credentials = session.get_credentials()
        return credentials is not None and credentials.access_key is not None
    except Exception:
        return False


class ProhibitedRestartAdapter(RestartAdapter):
    """
    Mechanically enforced no-restart adapter for validation phases (e.g., transient-latency validation).
    Guarantees that AWS restart APIs can NEVER be called.
    If any policy or controller path triggers a restart, this adapter intercepts it,
    records the safety violation, and returns a failed RestartResult without invoking AWS.
    """

    def __init__(self, environment_name: str = "") -> None:
        self.environment_name = environment_name
        self.restart_attempted: bool = False
        self.restart_history: List[Any] = []

    def restart(self, reason: str = "") -> Any:
        from experiment.models import RestartResult
        self.restart_attempted = True
        logger.error(
            "MECHANICAL RESTART PROHIBITION: Intercepted restart attempt in no-restart mode! Reason: %s",
            reason,
        )
        res = RestartResult(
            success=False,
            adapter_type="prohibited_no_restart",
            is_simulated=False,
            started_at=datetime.now(timezone.utc).isoformat(),
            completed_at=datetime.now(timezone.utc).isoformat(),
            duration_seconds=0.0,
            restart_id=f"prohibited_{uuid.uuid4().hex[:8]}",
            reason=f"BLOCKED_BY_SAFETY_PROHIBITION: {reason}",
            error_message="MECHANICAL RESTART PROHIBITION: Restarts are prohibited during this validation phase.",
        )
        self.restart_history.append(res)
        return res

    @property
    def is_simulated(self) -> bool:
        return False

    @property
    def adapter_type(self) -> str:
        return "prohibited_no_restart"


def _print_stalled_progress_remediation(base_url: str, fault_token: Optional[str]) -> None:
    """Prints clear, safe instructions for clearing application server fault state."""
    token_str = fault_token or "$env:FAULT_INJECTION_TOKEN"
    clean_url = base_url.rstrip("/")
    print("\n" + "=" * 76)
    print("EMERGENCY REMEDIATION: RESTORING APPLICATION SERVER")
    print("=" * 76)
    print("An interrupted or aborted 'stalled_progress' trial may leave worker threads")
    print("blocked on the server until the internal 45-second timeout expires.")
    print("To immediately restore normal operation WITHOUT claiming experiment recovery:")
    print("\n1. Via HTTP (Clears fault_state without container restart):")
    print(f'   Invoke-RestMethod -Method POST -Uri "{clean_url}/experiment/fault" `')
    print(f'     -Headers @{{"X-Fault-Token"="{token_str}"}} `')
    print('     -ContentType "application/json" `')
    print('     -Body \'{"fault_state": "normal"}\'')
    print("\n2. Via AWS CLI (Force a clean Elastic Beanstalk container restart):")
    print('   aws elasticbeanstalk restart-app-server --environment-name <ENV_NAME>')
    print("\n3. Or wait for the server\'s internal stall safety timeout to release (<= 45s).")
    print("=" * 76 + "\n")


def validate_live_configuration(
    base_url: str,
    environment_name: str,
    policy: str,
    scenario: str,
    allow_restarts: bool,
    allow_fault_injection: bool,
    dry_run: bool,
    fault_token: Optional[str] = None,
    prohibit_restarts: bool = False,
) -> None:
    """
    Enforces safety constraints, required parameters, and credential availability.
    Aborts immediately if required authorization is missing.
    """
    validate_url(base_url)

    normalized_policy = policy.lower().strip()
    if normalized_policy not in ("control", "no_restart", "norestart", "fixed_threshold", "progress_gated"):
        raise ValueError(f"Unknown policy '{policy}'. Allowed: control, fixed_threshold, progress_gated")

    # Conflict check: cannot simultaneously authorize and prohibit restarts
    if allow_restarts and prohibit_restarts:
        raise ValueError("Conflicting options: Cannot specify both --allow-restarts and --prohibit-restarts.")

    # Environment name is mandatory for policies that invoke AWS Elastic Beanstalk (unless in dry-run or restarts prohibited)
    if (
        normalized_policy not in ("control", "no_restart", "norestart")
        and not dry_run
        and not prohibit_restarts
        and not environment_name.strip()
    ):
        raise ValueError(f"Policy '{policy}' requires an explicit '--environment-name'.")

    # Safety Opt-in for live restarts
    requires_restarts = (normalized_policy in ("fixed_threshold", "progress_gated")) and not prohibit_restarts
    if requires_restarts and not dry_run and not allow_restarts:
        raise PermissionError(
            f"SAFETY ABORT: Policy '{policy}' can trigger live AWS Elastic Beanstalk server restarts. "
            f"You must explicitly provide the '--allow-restarts' flag to authorize live infrastructure actions, "
            f"or use '--prohibit-restarts' for mechanically enforced no-restart validation."
        )

    # Safety Opt-in for server fault injection
    scenario_injects_fault = scenario in ("stalled_progress", "hard_failure", "temporary_overload")
    if scenario_injects_fault and not dry_run:
        if not allow_fault_injection:
            raise PermissionError(
                f"SAFETY ABORT: Scenario '{scenario}' requires active fault injection. "
                f"You must explicitly provide '--allow-fault-injection' and configure FAULT_INJECTION_TOKEN."
            )
        effective_token = (fault_token or os.environ.get("FAULT_INJECTION_TOKEN", "")).strip()
        if not effective_token:
            raise ValueError(
                f"SAFETY ABORT: Scenario '{scenario}' requires a valid configured fault token. "
                f"Provide --fault-token or set the FAULT_INJECTION_TOKEN environment variable."
            )

    # Check AWS credentials for policies that invoke AWS APIs
    if requires_restarts and not dry_run:
        if not check_aws_credentials_available():
            raise RuntimeError(
                f"AWS CREDENTIALS MISSING: Policy '{policy}' requires live AWS access to Elastic Beanstalk. "
                f"No credentials could be discovered. Please configure AWS credentials via AWS_PROFILE, "
                f"environment variables, or an IAM role."
            )


def perform_dry_run_checks(
    base_url: str,
    environment_name: str,
    region: str,
    policy: str,
    scenario: str,
    config: PolicyConfig,
    duration: float,
    allow_restarts: bool,
    allow_fault_injection: bool,
    probe_timeout: float = 5.0,
    prohibit_restarts: bool = False,
    check_aws: bool = False,
    boto3_client: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Conducts non-mutating connectivity checks against the deployed application endpoints
    and prints the planned execution plan. Never calls restart APIs or injects faults.
    """
    print("\n" + "=" * 76)
    print("PROGRESS-GATED AWS ELASTIC BEANSTALK: DRY-RUN PRE-FLIGHT VERIFICATION")
    print("=" * 76)
    print(f"Target Base URL:       {base_url}")
    print(f"Target Environment:    {environment_name or 'N/A (Local / Unspecified)'} [{region}]")
    print(f"Policy Under Test:     {policy}")
    print(f"Scenario:              {scenario}")
    if prohibit_restarts:
        print("Restart Safety Mode:   MECHANICALLY PROHIBITED (Zero AWS restart calls possible)")
    print(f"Planned Duration:      {duration:.1f}s")
    print(f"Probe Timeout:         {probe_timeout:.1f}s")
    print(f"Restarts Authorized:   {'YES (--allow-restarts set)' if allow_restarts else 'NO (Restarts blocked)'}")
    print(f"Fault Auth Authorized: {'YES (--allow-fault-injection set)' if allow_fault_injection else 'NO (Fault injection blocked)'}")
    print("-" * 76)

    results: Dict[str, Any] = {
        "root_ok": False,
        "health_ok": False,
        "metrics_ok": False,
        "aws_env_status": None,
    }

    # 1. Check Root Endpoint
    try:
        req = urllib.request.Request(f"{base_url}/", headers={"User-Agent": "LiveAWS-DryRun/1.0"})
        with urllib.request.urlopen(req, timeout=probe_timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            results["root_ok"] = (resp.status == 200)
            print(f"  [OK] Root Endpoint (GET /):        HTTP {resp.status} | Project: '{data.get('project')}'")
    except Exception as exc:
        print(f"  [FAIL] Root Endpoint (GET /):        FAILED ({exc})")

    # 2. Check Health Endpoint
    try:
        req = urllib.request.Request(f"{base_url}/health", headers={"User-Agent": "LiveAWS-DryRun/1.0"})
        with urllib.request.urlopen(req, timeout=probe_timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            results["health_ok"] = (resp.status == 200 and data.get("status") == "healthy")
            print(f"  [OK] Health Check (GET /health):  HTTP {resp.status} | Status: '{data.get('status')}'")
    except Exception as exc:
        print(f"  [FAIL] Health Check (GET /health):  FAILED ({exc})")

    # 3. Check Metrics Endpoint
    try:
        req = urllib.request.Request(f"{base_url}/metrics", headers={"User-Agent": "LiveAWS-DryRun/1.0"})
        with urllib.request.urlopen(req, timeout=probe_timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            counter = data.get("completion_counter")
            pid = data.get("pid")
            results["metrics_ok"] = (resp.status == 200 and counter is not None)
            print(f"  [OK] Metrics Check (GET /metrics): HTTP {resp.status} | PID: {pid} | Counter: {counter}")
    except Exception as exc:
        print(f"  [FAIL] Metrics Check (GET /metrics): FAILED ({exc})")

    # 4. Read-Only AWS Status Check (only if explicitly requested via --check-aws or if remote AWS URL)
    is_local = is_local_url(base_url)
    should_call_aws = bool(environment_name and (check_aws or not is_local))

    if should_call_aws:
        try:
            if boto3_client is not None:
                client = boto3_client
            else:
                import boto3
                client = boto3.client("elasticbeanstalk", region_name=region)
            resp = client.describe_environments(EnvironmentNames=[environment_name])
            envs = resp.get("Environments", [])
            if envs:
                env_status = envs[0].get("Status")
                env_health = envs[0].get("Health")
                results["aws_env_status"] = f"{env_status} ({env_health})"
                print(f"  [OK] AWS Elastic Beanstalk Status: Environment '{environment_name}' is {env_status} | Health: {env_health}")
            else:
                print(f"  [WARN] AWS Elastic Beanstalk Status: Environment '{environment_name}' not found in region {region}")
        except Exception as exc:
            print(f"  [WARN] AWS Environment Status Check: Skipped or inaccessible ({exc})")
    elif is_local:
        results["aws_env_status"] = "Skipped (Local Base URL)"
        print("  [*] AWS Status Check: Skipped (Local base URL: pass --check-aws to query AWS APIs)")

    print("-" * 76)
    print("PLANNED LIVE POLICY CONFIGURATION:")
    print(f"  Latency Trigger Threshold (T): {config.latency_threshold_ms} ms")
    print(f"  Healthy Latency Threshold:     {config.healthy_latency_threshold_ms} ms")
    print(f"  Observation Polling Interval:  {config.observation_interval_seconds} s")
    print(f"  Restart Cooldown Window:       {config.cooldown_seconds} s")
    print(f"  Progress Hard Deadline:        {config.hard_deadline_seconds} s")
    print(f"  Recovery Criterion:            {config.recovery_consecutive_successes} consecutive healthy checks")
    print("=" * 76)
    print("[+] Dry-run completed cleanly. Zero restart APIs invoked. Zero faults injected.\n")
    return results


def run_live_aws_trial(
    base_url: str,
    environment_name: str = "",
    policy: str = "progress_gated",
    scenario: str = "transient_latency",
    region: str = "us-east-1",
    duration_seconds: float = 120.0,
    observation_interval_seconds: float = 2.0,
    latency_threshold_ms: float = 500.0,
    healthy_threshold_ms: float = 300.0,
    recovery_consecutive_successes: int = 3,
    cooldown_seconds: float = 60.0,
    hard_deadline_seconds: float = 60.0,
    probe_timeout_seconds: float = 5.0,
    request_interval_seconds: float = 0.2,
    default_work_delay_ms: float = 50.0,
    degraded_delay_ms: float = 750.0,
    fault_offset_seconds: float = 20.0,
    fault_duration_seconds: float = 20.0,
    allow_restarts: bool = False,
    allow_fault_injection: bool = False,
    prohibit_restarts: bool = False,
    check_aws: bool = False,
    dry_run: bool = False,
    fault_token: Optional[str] = None,
    log_dir: str = "results",
    boto3_client: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Executes a bounded live trial against an AWS Elastic Beanstalk environment.
    Guarantees that restart APIs are called only when explicitly authorized.
    """
    validate_live_configuration(
        base_url=base_url,
        environment_name=environment_name,
        policy=policy,
        scenario=scenario,
        allow_restarts=allow_restarts,
        allow_fault_injection=allow_fault_injection,
        dry_run=dry_run,
        fault_token=fault_token,
        prohibit_restarts=prohibit_restarts,
    )

    policy_config = PolicyConfig(
        latency_threshold_ms=latency_threshold_ms,
        observation_interval_seconds=observation_interval_seconds,
        hard_deadline_seconds=hard_deadline_seconds,
        cooldown_seconds=cooldown_seconds,
        recovery_consecutive_successes=recovery_consecutive_successes,
        healthy_latency_threshold_ms=healthy_threshold_ms,
    )

    if dry_run:
        dry_results = perform_dry_run_checks(
            base_url=base_url,
            environment_name=environment_name,
            region=region,
            policy=policy,
            scenario=scenario,
            config=policy_config,
            duration=duration_seconds,
            allow_restarts=allow_restarts,
            allow_fault_injection=allow_fault_injection,
            probe_timeout=probe_timeout_seconds,
            prohibit_restarts=prohibit_restarts,
            check_aws=check_aws,
            boto3_client=boto3_client,
        )
        return {"dry_run": True, "checks": dry_results}

    # Live Execution Mode
    normalized_policy = policy.lower().strip()
    is_control = normalized_policy in ("control", "no_restart", "norestart")

    # Select live adapter: NEVER silently fallback to SimulatedRestartAdapter
    if prohibit_restarts:
        # Mechanically block any restart attempt; zero AWS API calls can ever be made
        adapter: RestartAdapter = ProhibitedRestartAdapter(environment_name=environment_name)
    elif is_control:
        # Control never initiates restarts; pass live adapter or dummy without requiring AWS permissions
        adapter = ElasticBeanstalkRestartAdapter(
            environment_name=environment_name or "control-no-restart-env",
            region_name=region,
            boto3_client=boto3_client,
        )
    else:
        adapter = ElasticBeanstalkRestartAdapter(
            environment_name=environment_name,
            region_name=region,
            boto3_client=boto3_client,
        )

    timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    unique_run_id = f"live_trial_{policy}_{scenario}_{timestamp_str}_{uuid.uuid4().hex[:6]}"

    print(f"\n[+] Starting Live AWS Experiment Trial: {unique_run_id}")
    print(f"[+] Target Base URL:       {base_url}")
    print(f"[+] Target Environment:    {environment_name} [{region}]")
    print(f"[+] Policy:                {policy} | Scenario: {scenario}")
    print(f"[+] Duration:              {duration_seconds}s | Request Interval: {request_interval_seconds}s")
    print(f"[+] Cooldown:              {cooldown_seconds}s | Observation Interval: {observation_interval_seconds}s")
    print(f"[+] Adapter:               {adapter.adapter_type} (is_simulated={adapter.is_simulated})")
    if prohibit_restarts:
        print("[+] Restart Safety Mode:   MECHANICALLY PROHIBITED (Zero AWS restart calls possible)")

    fault_schedules: List[FaultSchedule] = []
    latency_schedules: List[LatencySpikeSchedule] = []

    if scenario == "transient_latency":
        # Elevated latency without rejection (work continues completing successfully)
        latency_schedules.append(
            LatencySpikeSchedule(
                offset_seconds=fault_offset_seconds,
                duration_seconds=fault_duration_seconds,
                degraded_delay_ms=degraded_delay_ms,
            )
        )
    elif scenario == "stalled_progress":
        # Stalled progress fault injected via /experiment/fault
        effective_token = fault_token or os.environ.get("FAULT_INJECTION_TOKEN", "")
        fault_schedules.append(
            FaultSchedule(
                offset_seconds=fault_offset_seconds,
                fault_state="simulated_stalled_progress",
                stall_timeout_seconds=45.0,
            )
        )

    runner = ExperimentRunner(
        base_url=base_url,
        policy_name=policy,
        config=policy_config,
        adapter=adapter,
        fault_token=fault_token or os.environ.get("FAULT_INJECTION_TOKEN", ""),
        log_dir=log_dir,
        run_id=unique_run_id,
        probe_timeout_seconds=probe_timeout_seconds,
        request_interval_seconds=request_interval_seconds,
        default_work_delay_ms=default_work_delay_ms,
    )

    effective_fault_token = fault_token or os.environ.get("FAULT_INJECTION_TOKEN", "")

    try:
        summary = runner.run(
            duration_seconds=duration_seconds,
            fault_schedules=fault_schedules,
            latency_schedules=latency_schedules,
        )
    except KeyboardInterrupt:
        print("\n[!] Trial Interrupted by User (SIGINT / KeyboardInterrupt)!")
        print("[*] Trial Recovery Status: UNRECOVERED (Interrupted before completion)")
        if scenario == "stalled_progress":
            _print_stalled_progress_remediation(base_url, effective_fault_token)
        raise
    except Exception as exc:
        print(f"\n[!] Trial Terminated with Exception: {exc}")
        print("[*] Trial Recovery Status: UNRECOVERED (Terminated with error)")
        if scenario == "stalled_progress":
            _print_stalled_progress_remediation(base_url, effective_fault_token)
        raise

    # Enforce mechanical restart prohibition check
    if prohibit_restarts and getattr(adapter, "restart_attempted", False):
        summary["trial_failed"] = True
        summary["safety_violation"] = True
        summary["safety_violation_reason"] = (
            "SAFETY VIOLATION: A restart was initiated during a prohibited-restart validation phase. "
            "The restart call was mechanically intercepted and prevented from reaching AWS."
        )
        print("\n" + "!" * 76)
        print("[!] TRIAL FAILED SAFETY CRITERIA:")
        print(f"    {summary['safety_violation_reason']}")
        print("    Zero cloud infrastructure APIs were invoked.")
        print("!" * 76 + "\n")

    # Honest recovery evaluation
    if summary.get("measured_recovery_time_seconds") is None:
        summary["recovered"] = False
        print("[*] Trial Recovery Status: UNRECOVERED (No recovery criteria met before trial ended)")
    else:
        summary["recovered"] = True
        print(f"[*] Trial Recovery Status: RECOVERED ({summary['measured_recovery_time_seconds']}s)")

    print("\n" + format_report(summary))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run live AWS Elastic Beanstalk progress-gated experiment trial with safety controls"
    )
    parser.add_argument("--base-url", required=True, help="Base HTTP/HTTPS URL of Elastic Beanstalk environment")
    parser.add_argument("--environment-name", default="", help="AWS Elastic Beanstalk environment name")
    parser.add_argument("--region", default="us-east-1", help="AWS region (default: us-east-1)")
    parser.add_argument(
        "--policy",
        choices=["control", "fixed_threshold", "progress_gated"],
        default="progress_gated",
        help="Policy under test (default: progress_gated)",
    )
    parser.add_argument(
        "--scenario",
        choices=["transient_latency", "stalled_progress", "normal"],
        default="transient_latency",
        help="Failure scenario to execute (default: transient_latency)",
    )
    parser.add_argument("--duration", type=float, default=120.0, help="Trial duration in seconds (default: 120.0)")
    parser.add_argument("--observation-interval", type=float, default=2.0, help="Observation polling interval in seconds (default: 2.0)")
    parser.add_argument("--latency-threshold", type=float, default=500.0, help="Latency trigger threshold T in ms (default: 500.0)")
    parser.add_argument("--healthy-threshold", type=float, default=300.0, help="Healthy latency threshold in ms (default: 300.0)")
    parser.add_argument("--recovery-criteria", type=int, default=3, help="Consecutive healthy probes for recovery (default: 3)")
    parser.add_argument("--cooldown", type=float, default=60.0, help="Restart cooldown window in seconds (default: 60.0)")
    parser.add_argument("--hard-deadline", type=float, default=60.0, help="Progress-gated hard deadline in seconds (default: 60.0)")
    parser.add_argument("--probe-timeout", type=float, default=5.0, help="Probe HTTP timeout in seconds (default: 5.0)")
    parser.add_argument("--request-interval", type=float, default=0.2, help="Workload request dispatch interval in seconds (default: 0.2)")
    parser.add_argument("--work-delay", type=float, default=50.0, help="Baseline work delay in ms (default: 50.0)")
    parser.add_argument("--degraded-delay", type=float, default=750.0, help="Degraded work delay in ms for latency spike (default: 750.0)")
    parser.add_argument("--fault-offset", type=float, default=20.0, help="Fault onset offset in seconds (default: 20.0)")
    parser.add_argument("--fault-duration", type=float, default=20.0, help="Fault duration in seconds (default: 20.0)")

    # Safety opt-in flags
    parser.add_argument("--dry-run", action="store_true", help="Perform pre-flight checks and display plan without mutating APIs")
    parser.add_argument("--check-aws", action="store_true", help="Explicitly perform AWS connectivity and describe-environments check in dry-run mode")
    parser.add_argument("--allow-restarts", action="store_true", help="Explicitly authorize live AWS server restart calls")
    parser.add_argument("--prohibit-restarts", action="store_true", help="Mechanically intercept and block all restart attempts (for validation phases)")
    parser.add_argument("--allow-fault-injection", action="store_true", help="Explicitly authorize live fault injection via /experiment/fault")
    parser.add_argument("--fault-token", default=None, help="Fault injection secret token (or set via FAULT_INJECTION_TOKEN env var)")
    parser.add_argument("--log-dir", default="results", help="Directory for trial logs (default: results)")

    args = parser.parse_args()

    try:
        run_live_aws_trial(
            base_url=args.base_url,
            environment_name=args.environment_name,
            policy=args.policy,
            scenario=args.scenario,
            region=args.region,
            duration_seconds=args.duration,
            observation_interval_seconds=args.observation_interval,
            latency_threshold_ms=args.latency_threshold,
            healthy_threshold_ms=args.healthy_threshold,
            recovery_consecutive_successes=args.recovery_criteria,
            cooldown_seconds=args.cooldown,
            hard_deadline_seconds=args.hard_deadline,
            probe_timeout_seconds=args.probe_timeout,
            request_interval_seconds=args.request_interval,
            default_work_delay_ms=args.work_delay,
            degraded_delay_ms=args.degraded_delay,
            fault_offset_seconds=args.fault_offset,
            fault_duration_seconds=args.fault_duration,
            allow_restarts=args.allow_restarts,
            allow_fault_injection=args.allow_fault_injection,
            prohibit_restarts=args.prohibit_restarts,
            check_aws=args.check_aws,
            dry_run=args.dry_run,
            fault_token=args.fault_token,
            log_dir=args.log_dir,
        )
    except KeyboardInterrupt:
        print("\n[!] Execution Interrupted by User.", file=sys.stderr)
        sys.exit(130)
    except Exception as err:
        print(f"\n[!] Execution Aborted: {err}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
