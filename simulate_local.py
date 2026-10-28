"""
Local experiment simulation runner for Progress-Gated Application-Server Restart.
Runs self-contained local benchmarks using unique trial IDs and strictly matched schedules.
Prints empirical analysis reports directly from immutable event logs.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from application import create_app, state
from experiment.adapters import SimulatedRestartAdapter
from experiment.analysis import format_report
from experiment.models import PolicyConfig
from experiment.runner import ExperimentRunner
from experiment.workload import FaultSchedule, LatencySpikeSchedule


def run_local_simulation(
    policy_name: str = "progress_gated",
    scenario: str = "transient_latency",
    duration_seconds: float = 5.0,
    port: int = 5055,
) -> None:
    token = "sim-dev-token"
    app = create_app({
        "TESTING": True,
        "EXPERIMENT_MODE": True,
        "FAULT_INJECTION_TOKEN": token,
        "DEFAULT_WORK_DELAY_MS": 20,
    })

    # Start threaded server on background thread
    from werkzeug.serving import make_server
    server = make_server("127.0.0.1", port, app, threaded=True)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    time.sleep(0.5)  # Allow server to initialize
    base_url = f"http://127.0.0.1:{port}"

    # Generate a unique trial identifier so logs are never appended or overwritten
    timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    unique_run_id = f"trial_{policy_name}_{scenario}_{timestamp_str}_{uuid.uuid4().hex[:6]}"

    print(f"\n[+] Local test server running on {base_url}")
    print(f"[+] Trial ID: {unique_run_id}")
    print(f"[+] Policy: {policy_name} | Scenario: {scenario} | Duration: {duration_seconds}s")

    # Define simulated restart callback to reset server state
    def on_simulated_restart() -> None:
        state.set_fault_state("normal")
        print("    [!] Simulated restart executed: reset application fault state to 'normal'")

    adapter = SimulatedRestartAdapter(
        on_restart_callback=on_simulated_restart,
        simulated_delay_seconds=0.08,
    )

    # Common policy parameters across all policies
    config = PolicyConfig(
        latency_threshold_ms=150.0,
        observation_interval_seconds=0.4,
        hard_deadline_seconds=1.6,
        cooldown_seconds=1.8,
        recovery_consecutive_successes=2,
        healthy_latency_threshold_ms=80.0,
    )

    fault_schedules: List[FaultSchedule] = []
    latency_schedules: List[LatencySpikeSchedule] = []

    if scenario == "transient_latency":
        # Transient latency degradation: delay increases to 250ms from t=1.0s to t=2.5s (1.5s duration)
        # Requests continue completing successfully with 200 OK!
        latency_schedules.append(
            LatencySpikeSchedule(offset_seconds=1.0, duration_seconds=1.5, degraded_delay_ms=250.0)
        )
    elif scenario == "stalled_progress":
        # Genuine stalled progress fault: worker threads stall from t=1.0s until restart intervention
        fault_schedules.append(
            FaultSchedule(offset_seconds=1.0, fault_state="simulated_stalled_progress", stall_timeout_seconds=8.0)
        )
    elif scenario == "transient_overload":
        # Transient load shedding rejection (HTTP 503) from t=1.0s for 1.5s duration
        fault_schedules.append(
            FaultSchedule(offset_seconds=1.0, fault_state="temporary_overload", duration_seconds=1.5)
        )

    runner = ExperimentRunner(
        base_url=base_url,
        policy_name=policy_name,
        config=config,
        adapter=adapter,
        fault_token=token,
        log_dir="results",
        run_id=unique_run_id,
    )

    try:
        summary = runner.run(
            duration_seconds=duration_seconds,
            fault_schedules=fault_schedules,
            latency_schedules=latency_schedules,
        )
        print("\n" + format_report(summary))
    finally:
        server.shutdown()
        server_thread.join(timeout=1.0)
        print("[+] Test server shut down cleanly.\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run local progress-gated experiment simulation")
    parser.add_argument(
        "--batch",
        action="store_true",
        help="Run reproducible batch of all comparative policies and scenarios",
    )
    parser.add_argument(
        "--trials",
        type=int,
        default=5,
        help="Number of trials per combination in batch mode (default: 5)",
    )
    parser.add_argument(
        "--policy",
        choices=["control", "fixed_threshold", "progress_gated"],
        default="progress_gated",
        help="Restart policy to test (default: progress_gated)",
    )
    parser.add_argument(
        "--scenario",
        choices=["transient_latency", "stalled_progress", "transient_overload"],
        default="transient_latency",
        help="Workload scenario (default: transient_latency)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=5.0,
        help="Experiment duration in seconds (default: 5.0 for single run, 3.5 for batch)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5055,
        help="Port for local test server (default: 5055)",
    )
    args = parser.parse_args()

    if args.batch:
        from experiment.batch import BatchConfig, run_batch_experiment
        # Use user-specified duration if explicitly changed, else default to 3.5 for batch efficiency
        batch_duration = args.duration if "--duration" in sys.argv else 3.5
        batch_cfg = BatchConfig(
            trials_per_combination=args.trials,
            duration_seconds=batch_duration,
            port=args.port,
        )
        run_batch_experiment(batch_cfg)
    else:
        run_local_simulation(
            policy_name=args.policy,
            scenario=args.scenario,
            duration_seconds=args.duration,
            port=args.port,
        )
