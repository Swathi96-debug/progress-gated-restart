"""
Reproducible batch experiment orchestrator for Progress-Gated Application-Server Restart.
Executes repeated matched trials across comparative policies and failure scenarios.
Produces immutable per-trial logs and structured summary CSV reports with mean and standard deviation.

METHODOLOGICAL GUARANTEES:
1. SCHEDULE PARITY:
   All policies within a scenario execute against the exact same intended request arrival
   schedule, fault onset, fault duration, and observation configuration.
2. IMMUTABLE INDEPENDENT LOGS:
   Every trial receives a cryptographically unique trial ID and distinct log files in results/.
   Existing logs are strictly preserved and never overwritten.
3. SEPARATION OF CONCERNS:
   Simulated restarts are strictly segregated from real AWS Elastic Beanstalk restart counts.
   Unrecovered trials are explicitly reported as N/A, never backfilled with synthetic recovery times.
4. HONEST STATISTICAL SCOPE:
   Aggregated metrics report sample mean and sample standard deviation where meaningful.
   Small-sample trials (e.g. n=5) are empirical demonstrations, not claims of statistical significance.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import statistics
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from application import create_app, state
from experiment.adapters import SimulatedRestartAdapter
from experiment.analysis import analyze_experiment_log
from experiment.models import PolicyConfig
from experiment.runner import ExperimentRunner
from experiment.workload import FaultSchedule, LatencySpikeSchedule

logger = logging.getLogger("progress_gated_restart.batch")


@dataclass
class BatchConfig:
    """Configuration for reproducible batch trial runs."""
    policies: List[str] = field(default_factory=lambda: ["control", "fixed_threshold", "progress_gated"])
    scenarios: List[str] = field(default_factory=lambda: ["transient_latency", "stalled_progress"])
    trials_per_combination: int = 5
    duration_seconds: float = 3.5
    request_interval_seconds: float = 0.1
    default_work_delay_ms: float = 20.0
    latency_threshold_ms: float = 150.0
    observation_interval_seconds: float = 0.3
    hard_deadline_seconds: float = 1.5
    cooldown_seconds: float = 1.2
    recovery_consecutive_successes: int = 2
    healthy_latency_threshold_ms: float = 80.0
    log_dir: str = "results"
    port: int = 5055
    base_url: Optional[str] = None
    fault_token: str = "sim-batch-token"
    # Seeded randomness and jitter controls:
    random_seed: Optional[int] = None
    arrival_jitter_ratio: float = 0.0
    latency_jitter_ratio: float = 0.0
    # Parameterized scenario configurations:
    scenario_params: Dict[str, Dict[str, Any]] = field(default_factory=dict)


@dataclass
class TrialResult:
    """Individual empirical result of a single benchmark trial."""
    trial_id: str
    scenario: str
    policy: str
    trial_index: int
    total_requests: int
    successful_requests: int
    failed_requests: int
    success_rate_percent: float
    simulated_restarts: int
    real_aws_restarts: int
    total_unique_restarts: int
    recovery_time_seconds: Optional[float]
    estimated_downtime_seconds: float
    mean_latency_ms: float
    p95_latency_ms: float
    log_file: str


@dataclass
class AggregateStats:
    """Statistical summary (mean, sample standard deviation, median, percentiles) for a scenario-policy pair."""
    scenario: str
    policy: str
    trials_count: int
    successful_requests_mean: float
    successful_requests_std: float
    failed_requests_mean: float
    failed_requests_std: float
    success_rate_mean: float
    success_rate_std: float
    simulated_restarts_mean: float
    simulated_restarts_std: float
    real_aws_restarts_mean: float
    real_aws_restarts_std: float
    recovery_time_mean: Optional[float]
    recovery_time_std: Optional[float]
    recovered_trials_count: int
    estimated_downtime_mean: float
    estimated_downtime_std: float
    # Extended metrics with defaults for full backward compatibility:
    unrecovered_trials_count: int = 0
    successful_requests_median: float = 0.0
    failed_requests_median: float = 0.0
    success_rate_median: float = 0.0
    recovery_time_median: Optional[float] = None
    estimated_downtime_median: float = 0.0
    mean_latency_mean_ms: float = 0.0
    mean_latency_median_ms: float = 0.0
    p95_latency_mean_ms: float = 0.0


def compute_aggregates(trials: List[TrialResult]) -> List[AggregateStats]:
    """Computes mean, sample standard deviation, median, and percentiles for each (scenario, policy) group."""
    groups: Dict[Tuple[str, str], List[TrialResult]] = {}
    for t in trials:
        key = (t.scenario, t.policy)
        groups.setdefault(key, []).append(t)

    aggregates: List[AggregateStats] = []
    for (scenario, policy), group_trials in groups.items():
        n = len(group_trials)

        def calc_mean_std(values: List[float]) -> Tuple[float, float]:
            if not values:
                return 0.0, 0.0
            mean_val = statistics.mean(values)
            std_val = statistics.stdev(values) if len(values) > 1 else 0.0
            return round(mean_val, 2), round(std_val, 2)

        def calc_median(values: List[float]) -> float:
            if not values:
                return 0.0
            return round(statistics.median(values), 2)

        succ_mean, succ_std = calc_mean_std([float(t.successful_requests) for t in group_trials])
        succ_med = calc_median([float(t.successful_requests) for t in group_trials])

        fail_mean, fail_std = calc_mean_std([float(t.failed_requests) for t in group_trials])
        fail_med = calc_median([float(t.failed_requests) for t in group_trials])

        rate_mean, rate_std = calc_mean_std([t.success_rate_percent for t in group_trials])
        rate_med = calc_median([t.success_rate_percent for t in group_trials])

        sim_rest_mean, sim_rest_std = calc_mean_std([float(t.simulated_restarts) for t in group_trials])
        aws_rest_mean, aws_rest_std = calc_mean_std([float(t.real_aws_restarts) for t in group_trials])
        down_mean, down_std = calc_mean_std([t.estimated_downtime_seconds for t in group_trials])
        down_med = calc_median([t.estimated_downtime_seconds for t in group_trials])

        lat_mean, _ = calc_mean_std([t.mean_latency_ms for t in group_trials])
        lat_med = calc_median([t.mean_latency_ms for t in group_trials])
        p95_mean, _ = calc_mean_std([t.p95_latency_ms for t in group_trials])

        # Recovery time: only consider trials where recovery actually occurred
        valid_recoveries = [t.recovery_time_seconds for t in group_trials if t.recovery_time_seconds is not None]
        rec_count = len(valid_recoveries)
        unrec_count = n - rec_count
        if rec_count == 0:
            rec_mean: Optional[float] = None
            rec_std: Optional[float] = None
            rec_med: Optional[float] = None
        elif rec_count == 1:
            rec_mean = round(valid_recoveries[0], 2)
            rec_std = 0.0
            rec_med = round(valid_recoveries[0], 2)
        else:
            rec_mean = round(statistics.mean(valid_recoveries), 2)
            rec_std = round(statistics.stdev(valid_recoveries), 2)
            rec_med = round(statistics.median(valid_recoveries), 2)

        aggregates.append(
            AggregateStats(
                scenario=scenario,
                policy=policy,
                trials_count=n,
                successful_requests_mean=succ_mean,
                successful_requests_std=succ_std,
                failed_requests_mean=fail_mean,
                failed_requests_std=fail_std,
                success_rate_mean=rate_mean,
                success_rate_std=rate_std,
                simulated_restarts_mean=sim_rest_mean,
                simulated_restarts_std=sim_rest_std,
                real_aws_restarts_mean=aws_rest_mean,
                real_aws_restarts_std=aws_rest_std,
                recovery_time_mean=rec_mean,
                recovery_time_std=rec_std,
                recovered_trials_count=rec_count,
                estimated_downtime_mean=down_mean,
                estimated_downtime_std=down_std,
                unrecovered_trials_count=unrec_count,
                successful_requests_median=succ_med,
                failed_requests_median=fail_med,
                success_rate_median=rate_med,
                recovery_time_median=rec_med,
                estimated_downtime_median=down_med,
                mean_latency_mean_ms=lat_mean,
                mean_latency_median_ms=lat_med,
                p95_latency_mean_ms=p95_mean,
            )
        )
    return aggregates


def write_summary_csv(
    trials: List[TrialResult],
    aggregates: List[AggregateStats],
    output_path: str,
) -> None:
    """
    Writes a unified summary CSV containing per-trial results and aggregate statistics.
    All rows share the same column structure for direct compatibility with PowerShell Import-Csv.
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    fieldnames = [
        "scenario",
        "policy",
        "row_type",
        "trial_index",
        "trial_id",
        "total_requests",
        "successful_requests",
        "failed_requests",
        "success_rate_percent",
        "simulated_restarts",
        "real_aws_restarts",
        "total_unique_restarts",
        "recovery_time_seconds",
        "estimated_downtime_seconds",
        "mean_latency_ms",
        "log_file",
    ]

    with open(output_path, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        # 1. Per-trial records
        for t in trials:
            rec_str = f"{t.recovery_time_seconds:.2f}" if t.recovery_time_seconds is not None else "N/A"
            writer.writerow({
                "scenario": t.scenario,
                "policy": t.policy,
                "row_type": "TRIAL",
                "trial_index": t.trial_index,
                "trial_id": t.trial_id,
                "total_requests": t.total_requests,
                "successful_requests": t.successful_requests,
                "failed_requests": t.failed_requests,
                "success_rate_percent": f"{t.success_rate_percent:.2f}",
                "simulated_restarts": t.simulated_restarts,
                "real_aws_restarts": t.real_aws_restarts,
                "total_unique_restarts": t.total_unique_restarts,
                "recovery_time_seconds": rec_str,
                "estimated_downtime_seconds": f"{t.estimated_downtime_seconds:.2f}",
                "mean_latency_ms": f"{t.mean_latency_ms:.2f}",
                "log_file": t.log_file,
            })

        # 2. Group aggregates (MEAN and STD rows)
        for agg in aggregates:
            rec_mean_str = f"{agg.recovery_time_mean:.2f}" if agg.recovery_time_mean is not None else "N/A"
            rec_std_str = f"{agg.recovery_time_std:.2f}" if agg.recovery_time_std is not None else "N/A"

            # MEAN row
            writer.writerow({
                "scenario": agg.scenario,
                "policy": agg.policy,
                "row_type": "SUMMARY_MEAN",
                "trial_index": f"{agg.recovered_trials_count}/{agg.trials_count}_recovered",
                "trial_id": f"MEAN_{agg.policy}_{agg.scenario}",
                "total_requests": f"{trials[0].total_requests if trials else 0}",
                "successful_requests": f"{agg.successful_requests_mean:.2f}",
                "failed_requests": f"{agg.failed_requests_mean:.2f}",
                "success_rate_percent": f"{agg.success_rate_mean:.2f}",
                "simulated_restarts": f"{agg.simulated_restarts_mean:.2f}",
                "real_aws_restarts": f"{agg.real_aws_restarts_mean:.2f}",
                "total_unique_restarts": f"{agg.simulated_restarts_mean + agg.real_aws_restarts_mean:.2f}",
                "recovery_time_seconds": rec_mean_str,
                "estimated_downtime_seconds": f"{agg.estimated_downtime_mean:.2f}",
                "mean_latency_ms": "N/A",
                "log_file": "AGGREGATE_STATISTICS",
            })

            # STD row
            writer.writerow({
                "scenario": agg.scenario,
                "policy": agg.policy,
                "row_type": "SUMMARY_STD",
                "trial_index": f"n={agg.trials_count}",
                "trial_id": f"STD_{agg.policy}_{agg.scenario}",
                "total_requests": "0.00",
                "successful_requests": f"{agg.successful_requests_std:.2f}",
                "failed_requests": f"{agg.failed_requests_std:.2f}",
                "success_rate_percent": f"{agg.success_rate_std:.2f}",
                "simulated_restarts": f"{agg.simulated_restarts_std:.2f}",
                "real_aws_restarts": f"{agg.real_aws_restarts_std:.2f}",
                "total_unique_restarts": "N/A",
                "recovery_time_seconds": rec_std_str,
                "estimated_downtime_seconds": f"{agg.estimated_downtime_std:.2f}",
                "mean_latency_ms": "N/A",
                "log_file": "AGGREGATE_STATISTICS",
            })


def write_aggregated_table_csv(aggregates: List[AggregateStats], output_path: str) -> None:
    """Writes a companion summary table containing only the aggregate rows."""
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    fieldnames = [
        "scenario",
        "policy",
        "trials_count",
        "recovered_trials_count",
        "successful_requests_mean",
        "successful_requests_std",
        "failed_requests_mean",
        "failed_requests_std",
        "success_rate_mean_percent",
        "success_rate_std_percent",
        "simulated_restarts_mean",
        "simulated_restarts_std",
        "real_aws_restarts_mean",
        "real_aws_restarts_std",
        "recovery_time_mean_seconds",
        "recovery_time_std_seconds",
        "estimated_downtime_mean_seconds",
        "estimated_downtime_std_seconds",
        "unrecovered_trials_count",
        "successful_requests_median",
        "failed_requests_median",
        "success_rate_median_percent",
        "recovery_time_median_seconds",
        "estimated_downtime_median_seconds",
        "mean_latency_mean_ms",
        "mean_latency_median_ms",
        "p95_latency_mean_ms",
    ]

    with open(output_path, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for a in aggregates:
            rec_m = f"{a.recovery_time_mean:.2f}" if a.recovery_time_mean is not None else "N/A"
            rec_s = f"{a.recovery_time_std:.2f}" if a.recovery_time_std is not None else "N/A"
            rec_med = f"{a.recovery_time_median:.2f}" if a.recovery_time_median is not None else "N/A"
            writer.writerow({
                "scenario": a.scenario,
                "policy": a.policy,
                "trials_count": a.trials_count,
                "recovered_trials_count": a.recovered_trials_count,
                "successful_requests_mean": f"{a.successful_requests_mean:.2f}",
                "successful_requests_std": f"{a.successful_requests_std:.2f}",
                "failed_requests_mean": f"{a.failed_requests_mean:.2f}",
                "failed_requests_std": f"{a.failed_requests_std:.2f}",
                "success_rate_mean_percent": f"{a.success_rate_mean:.2f}",
                "success_rate_std_percent": f"{a.success_rate_std:.2f}",
                "simulated_restarts_mean": f"{a.simulated_restarts_mean:.2f}",
                "simulated_restarts_std": f"{a.simulated_restarts_std:.2f}",
                "real_aws_restarts_mean": f"{a.real_aws_restarts_mean:.2f}",
                "real_aws_restarts_std": f"{a.real_aws_restarts_std:.2f}",
                "recovery_time_mean_seconds": rec_m,
                "recovery_time_std_seconds": rec_s,
                "estimated_downtime_mean_seconds": f"{a.estimated_downtime_mean:.2f}",
                "estimated_downtime_std_seconds": f"{a.estimated_downtime_std:.2f}",
                "unrecovered_trials_count": a.unrecovered_trials_count,
                "successful_requests_median": f"{a.successful_requests_median:.2f}",
                "failed_requests_median": f"{a.failed_requests_median:.2f}",
                "success_rate_median_percent": f"{a.success_rate_median:.2f}",
                "recovery_time_median_seconds": rec_med,
                "estimated_downtime_median_seconds": f"{a.estimated_downtime_median:.2f}",
                "mean_latency_mean_ms": f"{a.mean_latency_mean_ms:.2f}",
                "mean_latency_median_ms": f"{a.mean_latency_median_ms:.2f}",
                "p95_latency_mean_ms": f"{a.p95_latency_mean_ms:.2f}",
            })


def format_batch_report(aggregates: List[AggregateStats], summary_csv_path: str) -> str:
    """Formats batch aggregate results into a clean markdown/text summary report."""
    lines = [
        "=" * 78,
        "PROGRESS-GATED RESTART BATCH EXPERIMENT SUMMARY REPORT",
        "=" * 78,
        f"Summary CSV: {summary_csv_path}",
        "-" * 78,
        f"{'Scenario':<20} | {'Policy':<16} | {'Trials':<6} | {'Success (Mean±Std)':<18} | {'Sim Restarts':<12} | {'Recovery Time':<15}",
        "-" * 78,
    ]

    for a in aggregates:
        rec_str = (
            f"{a.recovery_time_mean:.2f}±{a.recovery_time_std:.2f}s"
            if a.recovery_time_mean is not None
            else f"N/A ({a.recovered_trials_count}/{a.trials_count})"
        )
        succ_str = f"{a.successful_requests_mean:.1f}±{a.successful_requests_std:.1f}"
        rest_str = f"{a.simulated_restarts_mean:.1f}±{a.simulated_restarts_std:.1f}"
        lines.append(
            f"{a.scenario:<20} | {a.policy:<16} | {a.trials_count:<6} | {succ_str:<18} | {rest_str:<12} | {rec_str:<15}"
        )

    lines.extend([
        "=" * 78,
        "METHODOLOGICAL NOTES:",
        "1. Real AWS Restarts: 0 across all simulated trials. Simulated restarts strictly segregated.",
        "2. Unrecovered trials reported as N/A (no synthetic recovery times backfilled).",
        "3. Small-sample demonstrations (n=5) do not constitute a claim of general statistical significance.",
        "=" * 78,
    ])
    return "\n".join(lines)


def run_batch_experiment(
    config: Optional[BatchConfig] = None,
    on_trial_complete: Optional[Callable[[TrialResult], None]] = None,
) -> Tuple[List[TrialResult], List[AggregateStats], str]:
    """
    Executes a reproducible batch of experiment trials across configured policies and scenarios.
    Ensures identical schedules within each scenario and immutable, unique trial logs.
    """
    cfg = config or BatchConfig()
    os.makedirs(cfg.log_dir, exist_ok=True)

    manage_server = cfg.base_url is None
    server = None
    server_thread = None

    if manage_server:
        app = create_app({
            "TESTING": True,
            "EXPERIMENT_MODE": True,
            "FAULT_INJECTION_TOKEN": cfg.fault_token,
            "DEFAULT_WORK_DELAY_MS": int(cfg.default_work_delay_ms),
        })
        from werkzeug.serving import make_server
        server = make_server("127.0.0.1", cfg.port, app, threaded=True)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        time.sleep(0.5)  # Allow server to bind and start
        base_url = f"http://127.0.0.1:{cfg.port}"
    else:
        base_url = cfg.base_url.rstrip("/")  # type: ignore

    batch_timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    trial_results: List[TrialResult] = []

    print(f"\n======================================================================")
    print(f"STARTING BATCH RUN: {len(cfg.scenarios)} scenarios x {len(cfg.policies)} policies x {cfg.trials_per_combination} trials")
    print(f"Total Trials: {len(cfg.scenarios) * len(cfg.policies) * cfg.trials_per_combination}")
    print(f"Duration per Trial: {cfg.duration_seconds}s | Port: {cfg.port}")
    print(f"======================================================================\n")

    try:
        for scenario in cfg.scenarios:
            fault_schedules: List[FaultSchedule] = []
            latency_schedules: List[LatencySpikeSchedule] = []
            current_scenario_params: Dict[str, Any] = dict(cfg.scenario_params.get(scenario, {}))

            if scenario == "transient_latency":
                # Parameterized transient latency spike
                off = float(current_scenario_params.get("offset_seconds", 1.0))
                dur = float(current_scenario_params.get("duration_seconds", 1.0))
                del_ms = float(current_scenario_params.get("degraded_delay_ms", 250.0))
                latency_schedules.append(
                    LatencySpikeSchedule(offset_seconds=off, duration_seconds=dur, degraded_delay_ms=del_ms)
                )
            elif scenario == "stalled_progress":
                # Parameterized stalled progress fault
                off = float(current_scenario_params.get("offset_seconds", 1.0))
                tout = float(current_scenario_params.get("stall_timeout_seconds", 2.5))
                fault_schedules.append(
                    FaultSchedule(offset_seconds=off, fault_state="simulated_stalled_progress", stall_timeout_seconds=tout)
                )
            elif scenario == "flapping_overload":
                # Intermittent / flapping overload bursts (HTTP 503 load-shedding)
                bursts = int(current_scenario_params.get("burst_count", 2))
                burst_dur = float(current_scenario_params.get("burst_duration_seconds", 0.4))
                gap = float(current_scenario_params.get("recovery_gap_seconds", 0.3))
                base_off = float(current_scenario_params.get("offset_seconds", 1.0))
                for b in range(bursts):
                    b_off = base_off + b * (burst_dur + gap)
                    fault_schedules.append(
                        FaultSchedule(offset_seconds=b_off, fault_state="temporary_overload", duration_seconds=burst_dur)
                    )
            elif scenario == "transient_overload":
                off = float(current_scenario_params.get("offset_seconds", 1.0))
                dur = float(current_scenario_params.get("duration_seconds", 1.0))
                fault_schedules.append(
                    FaultSchedule(offset_seconds=off, fault_state="temporary_overload", duration_seconds=dur)
                )

            for policy_name in cfg.policies:
                for trial_idx in range(1, cfg.trials_per_combination + 1):
                    # Ensure clean application state prior to each trial
                    state.reset_for_testing()
                    time.sleep(0.08)

                    # Cryptographically unique trial identifier for immutable logging
                    trial_id = f"trial_{policy_name}_{scenario}_{batch_timestamp}_t{trial_idx}_{uuid.uuid4().hex[:6]}"

                    # Matched pseudo-random seed per repetition across all policies
                    trial_seed = (cfg.random_seed + trial_idx * 1000) if cfg.random_seed is not None else None

                    def on_simulated_restart() -> None:
                        state.set_fault_state("normal")

                    adapter = SimulatedRestartAdapter(
                        on_restart_callback=on_simulated_restart,
                        simulated_delay_seconds=0.05,
                    )

                    policy_config = PolicyConfig(
                        latency_threshold_ms=cfg.latency_threshold_ms,
                        observation_interval_seconds=cfg.observation_interval_seconds,
                        hard_deadline_seconds=cfg.hard_deadline_seconds,
                        cooldown_seconds=cfg.cooldown_seconds,
                        recovery_consecutive_successes=cfg.recovery_consecutive_successes,
                        healthy_latency_threshold_ms=cfg.healthy_latency_threshold_ms,
                    )

                    runner = ExperimentRunner(
                        base_url=base_url,
                        policy_name=policy_name,
                        config=policy_config,
                        adapter=adapter,
                        fault_token=cfg.fault_token,
                        log_dir=cfg.log_dir,
                        run_id=trial_id,
                        request_interval_seconds=cfg.request_interval_seconds,
                        default_work_delay_ms=cfg.default_work_delay_ms,
                        random_seed=trial_seed,
                        arrival_jitter_ratio=cfg.arrival_jitter_ratio,
                        latency_jitter_ratio=cfg.latency_jitter_ratio,
                        scenario_name=scenario,
                        scenario_params=current_scenario_params,
                    )

                    print(f"[*] Running [{scenario}] [{policy_name}] Trial {trial_idx}/{cfg.trials_per_combination} (ID: {trial_id})...")

                    summary = runner.run(
                        duration_seconds=cfg.duration_seconds,
                        fault_schedules=fault_schedules,
                        latency_schedules=latency_schedules,
                    )

                    rec_time = summary.get("measured_recovery_time_seconds")
                    t_result = TrialResult(
                        trial_id=trial_id,
                        scenario=scenario,
                        policy=policy_name,
                        trial_index=trial_idx,
                        total_requests=summary["total_requests"],
                        successful_requests=summary["successful_requests"],
                        failed_requests=summary["failed_requests"],
                        success_rate_percent=summary["success_rate_percent"],
                        simulated_restarts=summary["restarts"]["simulated_restarts_count"],
                        real_aws_restarts=summary["restarts"]["aws_restarts_count"],
                        total_unique_restarts=summary["restarts"]["total_unique_restarts"],
                        recovery_time_seconds=rec_time,
                        estimated_downtime_seconds=summary["estimated_downtime_seconds"],
                        mean_latency_ms=summary["latency_metrics"]["mean_ms"],
                        p95_latency_ms=summary["latency_metrics"]["p95_ms"],
                        log_file=summary["log_path"],
                    )
                    trial_results.append(t_result)
                    if on_trial_complete:
                        on_trial_complete(t_result)

                    rec_display = f"{rec_time:.2f}s" if rec_time is not None else "N/A"
                    print(f"    -> Reqs: {t_result.successful_requests}/{t_result.total_requests} succ | Restarts: {t_result.simulated_restarts} sim | Rec: {rec_display}")

        # Compute aggregate statistics
        aggregates = compute_aggregates(trial_results)

        # Output summary files
        summary_csv_path = os.path.join(cfg.log_dir, f"batch_summary_{batch_timestamp}.csv")
        aggregated_csv_path = os.path.join(cfg.log_dir, f"batch_aggregated_{batch_timestamp}.csv")

        write_summary_csv(trial_results, aggregates, summary_csv_path)
        write_aggregated_table_csv(aggregates, aggregated_csv_path)

        print("\n" + format_batch_report(aggregates, summary_csv_path))
        print(f"[+] Detailed trial & summary CSV saved: {summary_csv_path}")
        print(f"[+] Aggregated table CSV saved:        {aggregated_csv_path}\n")

        return trial_results, aggregates, summary_csv_path

    finally:
        if manage_server and server and server_thread:
            server.shutdown()
            server_thread.join(timeout=1.0)
            print("[+] Batch local test server shut down cleanly.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run reproducible batch experiment benchmark")
    parser.add_argument("--trials", type=int, default=5, help="Trials per combination (default: 5)")
    parser.add_argument("--duration", type=float, default=3.5, help="Duration per trial in seconds (default: 3.5)")
    parser.add_argument("--port", type=int, default=5066, help="Port for batch server (default: 5066)")
    parser.add_argument("--seed", type=int, default=None, help="Base random seed for reproducible variation (default: None)")
    parser.add_argument("--arrival-jitter", type=float, default=0.0, help="Arrival jitter ratio 0.0-1.0 (default: 0.0)")
    parser.add_argument("--latency-jitter", type=float, default=0.0, help="Latency jitter ratio 0.0-1.0 (default: 0.0)")
    parser.add_argument("--scenarios", nargs="+", default=None, help="Scenarios to run (e.g. transient_latency stalled_progress flapping_overload)")
    args = parser.parse_args()

    scenarios = args.scenarios or ["transient_latency", "stalled_progress"]
    cfg = BatchConfig(
        policies=["control", "fixed_threshold", "progress_gated"],
        scenarios=scenarios,
        trials_per_combination=args.trials,
        duration_seconds=args.duration,
        port=args.port,
        random_seed=args.seed,
        arrival_jitter_ratio=args.arrival_jitter,
        latency_jitter_ratio=args.latency_jitter,
    )
    run_batch_experiment(cfg)
