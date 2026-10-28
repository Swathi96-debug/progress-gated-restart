"""
Results analysis script for reproducible evaluation of experiment logs.
Calculates request success/failure metrics, latency percentiles, restart counts,
estimated request-inferred downtime, and validated recovery times directly from immutable event logs.

METHODOLOGICAL DEFINITIONS:
1. RECOVERY TIME:
   The elapsed duration (in seconds) from the initial scheduled fault injection timestamp
   until the first subsequent validated recovery event (which requires N consecutive healthy,
   low-latency probes). If no valid recovery event occurred before trial termination,
   recovery time is reported as null / None (unrecovered). It is NEVER reported as a measured
   value when no valid recovery event exists.

2. DOWNTIME (REQUEST-INFERRED ESTIMATE):
   The empirical duration (in seconds) between the first failed request and the first subsequent
   successful request. Because client request arrival is discrete rather than continuous, this
   metric is strictly labeled as an ESTIMATED request-inferred downtime, not a continuous
   kernel-measured outage.

3. RESTART COUNT DEDUPLICATION:
   Restart operations are tracked by unique restart identifiers (UUIDs). Duplicate log entries
   with the same restart_id are never double-counted. Simulated restarts are strictly segregated
   from AWS Elastic Beanstalk API actions.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Set


def analyze_experiment_log(jsonl_path: str) -> Dict[str, Any]:
    """
    Parses an immutable JSONL experiment log and calculates empirical benchmark metrics.
    """
    if not os.path.exists(jsonl_path):
        raise FileNotFoundError(f"Log file not found: {jsonl_path}")

    requests: List[Dict[str, Any]] = []
    observations: List[Dict[str, Any]] = []
    policy_decisions: List[Dict[str, Any]] = []
    restart_events: List[Dict[str, Any]] = []
    fault_events: List[Dict[str, Any]] = []
    recovery_events: List[Dict[str, Any]] = []
    trial_metadata: Dict[str, Any] = {}
    trial_ids: Set[str] = set()

    with open(jsonl_path, mode="r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            if "trial_id" in entry:
                trial_ids.add(str(entry["trial_id"]))

            event_type = entry.get("event_type")
            if event_type == "request":
                requests.append(entry)
            elif event_type == "progress_observation":
                observations.append(entry)
            elif event_type == "policy_decision":
                policy_decisions.append(entry)
            elif event_type == "restart_event":
                restart_events.append(entry)
            elif event_type == "fault_event":
                # Only record successfully executed faults as baselines for recovery calculation
                if entry.get("success") is not False:
                    fault_events.append(entry)
            elif event_type == "recovery_event":
                recovery_events.append(entry)
            elif event_type == "trial_metadata":
                trial_metadata.update(entry)

    # 1. Request Telemetry
    total_requests = len(requests)
    successful_requests = sum(1 for r in requests if r.get("success") is True)
    failed_requests = total_requests - successful_requests
    success_rate = round((successful_requests / total_requests * 100.0), 2) if total_requests > 0 else 0.0

    latencies = [float(r.get("latency_ms", 0)) for r in requests if r.get("latency_ms") is not None]
    latencies.sort()

    def percentile(p: float) -> float:
        if not latencies:
            return 0.0
        k = (len(latencies) - 1) * (p / 100.0)
        f = int(k)
        c = min(f + 1, len(latencies) - 1)
        d = k - f
        return round(latencies[f] + d * (latencies[c] - latencies[f]), 2)

    latency_metrics = {
        "min_ms": round(latencies[0], 2) if latencies else 0.0,
        "mean_ms": round(sum(latencies) / len(latencies), 2) if latencies else 0.0,
        "median_ms": percentile(50),
        "p90_ms": percentile(90),
        "p95_ms": percentile(95),
        "p99_ms": percentile(99),
        "max_ms": round(latencies[-1], 2) if latencies else 0.0,
    }

    # 2. Restart Telemetry (Strict Deduplication and Simulation Segregation)
    unique_restarts: List[Dict[str, Any]] = []
    seen_restart_ids: Set[str] = set()

    for r in restart_events:
        rid = str(r.get("restart_id", ""))
        if rid and rid in seen_restart_ids:
            continue  # Prevent double-counting duplicate log entries
        if rid:
            seen_restart_ids.add(rid)
        unique_restarts.append(r)

    simulated_restarts = [r for r in unique_restarts if r.get("is_simulated") is True]
    aws_restarts = [r for r in unique_restarts if r.get("is_simulated") is False]

    restart_metrics = {
        "total_unique_restarts": len(unique_restarts),
        "simulated_restarts_count": len(simulated_restarts),
        "aws_restarts_count": len(aws_restarts),
        "restart_details": [
            {
                "restart_id": r.get("restart_id"),
                "adapter_type": r.get("adapter_type"),
                "is_simulated": r.get("is_simulated"),
                "duration_seconds": r.get("duration_seconds"),
                "reason": r.get("reason"),
            }
            for r in unique_restarts
        ],
    }

    # 3. Policy Decisions Summary
    decision_counts: Dict[str, int] = {}
    for d in policy_decisions:
        dec = d.get("decision", "UNKNOWN")
        decision_counts[dec] = decision_counts.get(dec, 0) + 1

    # 4. Validated Recovery Time Calculation
    # Do not report recovery time as a measured value when no valid recovery event exists!
    first_recovery_time: Optional[float] = None
    all_recovery_times: List[float] = []

    if fault_events and recovery_events:
        for fault in fault_events:
            fault_time = float(fault.get("timestamp", 0))
            subsequent_recoveries = [
                float(rec.get("timestamp", 0)) - fault_time
                for rec in recovery_events
                if float(rec.get("timestamp", 0)) >= fault_time
            ]
            if subsequent_recoveries:
                val = round(min(subsequent_recoveries), 2)
                all_recovery_times.append(val)
                if first_recovery_time is None:
                    first_recovery_time = val

    # 5. Estimated Request-Inferred Downtime
    # Strictly labeled as an estimate rather than a directly measured continuous outage
    estimated_downtime_seconds = 0.0
    if requests:
        failure_start: Optional[float] = None
        for r in requests:
            ts = float(r.get("timestamp", 0))
            is_success = r.get("success") is True
            if not is_success:
                if failure_start is None:
                    failure_start = ts
            else:
                if failure_start is not None:
                    estimated_downtime_seconds += max(0.0, ts - failure_start)
                    failure_start = None
        # Handle trailing failure period if test ended during failure
        if failure_start is not None and requests:
            estimated_downtime_seconds += max(0.0, float(requests[-1].get("timestamp", 0)) - failure_start)

    estimated_downtime_seconds = round(estimated_downtime_seconds, 2)

    return {
        "log_path": jsonl_path,
        "trial_ids": sorted(trial_ids),
        "total_requests": total_requests,
        "successful_requests": successful_requests,
        "failed_requests": failed_requests,
        "success_rate_percent": success_rate,
        "latency_metrics": latency_metrics,
        "restarts": restart_metrics,
        "decision_counts": decision_counts,
        "measured_recovery_time_seconds": first_recovery_time,
        "all_recovery_times_seconds": all_recovery_times if all_recovery_times else None,
        "estimated_downtime_seconds": estimated_downtime_seconds,
        "trial_metadata": trial_metadata,
        "is_unrecovered": (first_recovery_time is None and len(fault_events) > 0),
    }


def format_report(summary: Dict[str, Any]) -> str:
    """Formats the summary dictionary into an honest, human-readable report."""
    rec_time_str = (
        f"{summary['measured_recovery_time_seconds']}s"
        if summary["measured_recovery_time_seconds"] is not None
        else "N/A (No recovery event occurred before trial ended)"
    )

    lines = [
        "=" * 70,
        "PROGRESS-GATED RESTART EXPERIMENT ANALYSIS REPORT",
        "=" * 70,
        f"Log Source:              {summary['log_path']}",
        f"Trial ID(s):             {', '.join(summary['trial_ids']) if summary['trial_ids'] else 'N/A'}",
        f"Total Work Requests:     {summary['total_requests']}",
        f"Successful Requests:     {summary['successful_requests']}",
        f"Failed Requests:         {summary['failed_requests']}",
        f"Success Rate:            {summary['success_rate_percent']}%",
        "-" * 70,
        "LATENCY DISTRIBUTION (ms):",
        f"  Min: {summary['latency_metrics']['min_ms']} | Mean: {summary['latency_metrics']['mean_ms']} | Median (P50): {summary['latency_metrics']['median_ms']}",
        f"  P90: {summary['latency_metrics']['p90_ms']} | P95: {summary['latency_metrics']['p95_ms']} | P99: {summary['latency_metrics']['p99_ms']} | Max: {summary['latency_metrics']['max_ms']}",
        "-" * 70,
        "RESTART OPERATIONS (Deduplicated):",
        f"  Total Unique Restarts:    {summary['restarts']['total_unique_restarts']}",
        f"  Simulated Restarts:       {summary['restarts']['simulated_restarts_count']}",
        f"  Real AWS Restarts:        {summary['restarts']['aws_restarts_count']}",
        "-" * 70,
        "POLICY DECISIONS:",
    ]
    for dec, count in summary["decision_counts"].items():
        lines.append(f"  {dec:<20}: {count}")

    lines.extend([
        "-" * 70,
        f"Estimated Downtime (Request-Inferred): {summary['estimated_downtime_seconds']}s",
        f"Validated Recovery Time:               {rec_time_str}",
        "=" * 70,
    ])
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyze progress-gated experiment log")
    parser.add_argument("log_file", help="Path to the .jsonl log file")
    args = parser.parse_args()

    try:
        report = analyze_experiment_log(args.log_file)
        print(format_report(report))
    except Exception as err:
        print(f"Error analyzing log: {err}", file=sys.stderr)
        sys.exit(1)
