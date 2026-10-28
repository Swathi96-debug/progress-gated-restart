"""
Unit tests for ExperimentLogger, results analysis script, and workload scenarios.
Validates metric accuracy, restart deduplication, trial identifiers, and honest recovery reporting.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from unittest.mock import MagicMock

from experiment.analysis import analyze_experiment_log, format_report
from experiment.logger import ExperimentLogger
from experiment.workload import LatencySpikeSchedule, WorkloadGenerator


def test_experiment_logger_writes_trial_id_and_csv() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        logger = ExperimentLogger(log_dir=tmp_dir, run_id="trial_alpha_42")

        logger.log_request(
            request_id="req-1",
            endpoint="/work",
            status_code=200,
            latency_ms=25.5,
            delay_ms=20.0,
            success=True,
        )
        logger.log_request(
            request_id="req-2",
            endpoint="/work",
            status_code=503,
            latency_ms=5.0,
            delay_ms=20.0,
            success=False,
            error_message="HTTP 503",
        )
        logger.log_progress_observation({
            "completion_counter": 1,
            "pid": 1234,
            "is_healthy": True,
            "probe_latency_ms": 12.0,
        })
        logger.log_policy_decision({
            "policy_name": "ProgressGatedPolicy",
            "decision": "OBSERVE_PROGRESS",
            "reason": "Forward progress",
        })
        logger.log_restart_event({
            "restart_id": "sim-123",
            "adapter_type": "simulated",
            "is_simulated": True,
            "success": True,
            "duration_seconds": 0.05,
        })
        logger.log_recovery_event({
            "policy_name": "ProgressGatedPolicy",
            "reason": "Recovered",
        })

        assert os.path.exists(logger.jsonl_path)
        assert os.path.exists(logger.requests_csv_path)

        with open(logger.jsonl_path, mode="r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]

        assert len(lines) == 6
        for line in lines:
            assert line.get("trial_id") == "trial_alpha_42"
            assert "timestamp" in line
            assert "iso_timestamp" in line

        with open(logger.requests_csv_path, mode="r", encoding="utf-8") as f:
            rows = f.readlines()
        assert len(rows) == 3  # 1 header + 2 requests
        assert "trial_id" in rows[0]
        assert "trial_alpha_42" in rows[1]


def test_analyze_experiment_log_accuracy_and_segregation() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        log_file = os.path.join(tmp_dir, "synthetic_run.jsonl")

        records = [
            {"trial_id": "run_01", "event_type": "request", "timestamp": 1000.0, "latency_ms": 20.0, "success": True},
            {"trial_id": "run_01", "event_type": "request", "timestamp": 1001.0, "latency_ms": 30.0, "success": True},
            {"trial_id": "run_01", "event_type": "request", "timestamp": 1002.0, "latency_ms": 100.0, "success": False},
            {"trial_id": "run_01", "event_type": "request", "timestamp": 1003.0, "latency_ms": 110.0, "success": False},
            {"trial_id": "run_01", "event_type": "request", "timestamp": 1004.0, "latency_ms": 25.0, "success": True},
            {"trial_id": "run_01", "event_type": "fault_event", "timestamp": 1001.5, "fault_state": "temporary_overload"},
            {
                "trial_id": "run_01",
                "event_type": "restart_event",
                "timestamp": 1002.5,
                "restart_id": "sim-1",
                "adapter_type": "simulated",
                "is_simulated": True,
                "duration_seconds": 0.02,
                "reason": "Fixed threshold",
            },
            {
                "trial_id": "run_01",
                "event_type": "restart_event",
                "timestamp": 1003.5,
                "restart_id": "aws-eb-99",
                "adapter_type": "aws_elastic_beanstalk",
                "is_simulated": False,
                "duration_seconds": 1.25,
                "reason": "AWS EB restart",
            },
            {
                "trial_id": "run_01",
                "event_type": "recovery_event",
                "timestamp": 1004.5,
                "policy_name": "FixedThresholdPolicy",
                "reason": "Recovered",
            },
        ]

        with open(log_file, mode="w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

        analysis = analyze_experiment_log(log_file)

        assert analysis["total_requests"] == 5
        assert analysis["successful_requests"] == 3
        assert analysis["failed_requests"] == 2
        assert analysis["success_rate_percent"] == 60.0
        assert analysis["trial_ids"] == ["run_01"]

        # Latency calculations
        assert analysis["latency_metrics"]["min_ms"] == 20.0
        assert analysis["latency_metrics"]["max_ms"] == 110.0

        # Restart breakdown: strictly segregated
        assert analysis["restarts"]["total_unique_restarts"] == 2
        assert analysis["restarts"]["simulated_restarts_count"] == 1
        assert analysis["restarts"]["aws_restarts_count"] == 1

        # Recovery time: from fault (1001.5) to recovery (1004.5) = 3.0 seconds
        assert analysis["measured_recovery_time_seconds"] == 3.0

        # Estimated downtime
        assert analysis["estimated_downtime_seconds"] == 2.0


def test_analyze_experiment_log_deduplicates_duplicate_restarts() -> None:
    """Ensures duplicate entries with the same restart_id are never counted twice."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        log_file = os.path.join(tmp_dir, "dup_restart_run.jsonl")

        records = [
            {
                "trial_id": "run_dup",
                "event_type": "restart_event",
                "timestamp": 1002.5,
                "restart_id": "sim-unique-1",
                "adapter_type": "simulated",
                "is_simulated": True,
            },
            # Duplicate log line of same restart_id
            {
                "trial_id": "run_dup",
                "event_type": "restart_event",
                "timestamp": 1002.5,
                "restart_id": "sim-unique-1",
                "adapter_type": "simulated",
                "is_simulated": True,
            },
        ]

        with open(log_file, mode="w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

        analysis = analyze_experiment_log(log_file)
        assert analysis["restarts"]["total_unique_restarts"] == 1
        assert analysis["restarts"]["simulated_restarts_count"] == 1


def test_analyze_experiment_log_no_recovery_reports_none() -> None:
    """
    When no valid recovery event occurred, recovery time must be None and not fabricated.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        log_file = os.path.join(tmp_dir, "unrecovered_run.jsonl")

        records = [
            {"trial_id": "run_unrec", "event_type": "request", "timestamp": 1000.0, "latency_ms": 20.0, "success": True},
            {"trial_id": "run_unrec", "event_type": "request", "timestamp": 1001.0, "latency_ms": 100.0, "success": False},
            {"trial_id": "run_unrec", "event_type": "fault_event", "timestamp": 1001.0, "fault_state": "hard_failure"},
            # Notice: no recovery_event in log!
        ]

        with open(log_file, mode="w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

        analysis = analyze_experiment_log(log_file)
        assert analysis["measured_recovery_time_seconds"] is None
        assert analysis["all_recovery_times_seconds"] is None

        report = format_report(analysis)
        assert "N/A (No recovery event occurred before trial ended)" in report
        assert "Estimated Downtime (Request-Inferred)" in report


def test_analyze_experiment_log_ignores_failed_fault_injection() -> None:
    """
    Verifies that a fault event marked with success=False (e.g. 403 Forbidden)
    is not counted as an active fault baseline for recovery calculation.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        log_file = os.path.join(tmp_dir, "failed_fault_run.jsonl")

        records = [
            {"trial_id": "run_failed_fault", "event_type": "request", "timestamp": 1000.0, "latency_ms": 20.0, "success": True},
            {"trial_id": "run_failed_fault", "event_type": "request", "timestamp": 1001.0, "latency_ms": 22.0, "success": True},
            # A rejected fault injection attempt
            {"trial_id": "run_failed_fault", "event_type": "fault_event", "timestamp": 1001.0, "fault_state": "simulated_stalled_progress", "success": False, "error": "HTTP Error 403: FORBIDDEN"},
            {"trial_id": "run_failed_fault", "event_type": "request", "timestamp": 1002.0, "latency_ms": 21.0, "success": True},
        ]

        with open(log_file, mode="w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

        analysis = analyze_experiment_log(log_file)
        # Because no fault was successfully injected, there was no fault baseline
        assert analysis["measured_recovery_time_seconds"] is None
        assert analysis["successful_requests"] == 3
        assert analysis["total_requests"] == 3
        assert analysis["success_rate_percent"] == 100.0


def test_workload_schedule_reproducibility_and_dispatch_count() -> None:
    """
    Verifies that the workload generator dispatches the exact scheduled number
    of attempted requests across a target duration.
    """
    generator = WorkloadGenerator(
        base_url="http://127.0.0.1:9",
        request_interval_seconds=0.05,
        default_work_delay_ms=10.0,
        max_workers=5,
    )
    # Mock network call to measure dispatch count and delay schedules deterministically
    dispatched_delays = []

    def mock_send_request(delay_ms: float):
        dispatched_delays.append(delay_ms)
        generator.total_requests_sent += 1
        return {"success": True}

    generator._send_work_request = mock_send_request  # type: ignore

    spike = LatencySpikeSchedule(offset_seconds=0.1, duration_seconds=0.1, degraded_delay_ms=250.0)

    # 0.3 seconds at 0.05 interval = exactly 6 scheduled requests
    generator.run_workload(
        duration_seconds=0.3,
        latency_schedules=[spike],
    )

    assert generator.total_requests_sent == 6
    # Offsets: 0.0 (normal), 0.05 (normal), 0.10 (spike), 0.15 (spike), 0.20 (normal), 0.25 (normal)
    assert 250.0 in dispatched_delays
    assert 10.0 in dispatched_delays


def test_batch_aggregates_computation() -> None:
    """Verifies that compute_aggregates accurately calculates mean, std, and handles unrecovered trials."""
    from experiment.batch import AggregateStats, TrialResult, compute_aggregates

    trials = [
        # 3 recovered trials for policy_A
        TrialResult(
            trial_id="t1", scenario="scen_1", policy="policy_A", trial_index=1,
            total_requests=40, successful_requests=40, failed_requests=0, success_rate_percent=100.0,
            simulated_restarts=1, real_aws_restarts=0, total_unique_restarts=1,
            recovery_time_seconds=2.0, estimated_downtime_seconds=0.0,
            mean_latency_ms=25.0, p95_latency_ms=45.0, log_file="t1.jsonl",
        ),
        TrialResult(
            trial_id="t2", scenario="scen_1", policy="policy_A", trial_index=2,
            total_requests=40, successful_requests=40, failed_requests=0, success_rate_percent=100.0,
            simulated_restarts=1, real_aws_restarts=0, total_unique_restarts=1,
            recovery_time_seconds=3.0, estimated_downtime_seconds=0.0,
            mean_latency_ms=27.0, p95_latency_ms=48.0, log_file="t2.jsonl",
        ),
        # 2 unrecovered trials for control policy
        TrialResult(
            trial_id="t3", scenario="scen_1", policy="control", trial_index=1,
            total_requests=40, successful_requests=10, failed_requests=30, success_rate_percent=25.0,
            simulated_restarts=0, real_aws_restarts=0, total_unique_restarts=0,
            recovery_time_seconds=None, estimated_downtime_seconds=3.0,
            mean_latency_ms=2500.0, p95_latency_ms=5000.0, log_file="t3.jsonl",
        ),
        TrialResult(
            trial_id="t4", scenario="scen_1", policy="control", trial_index=2,
            total_requests=40, successful_requests=12, failed_requests=28, success_rate_percent=30.0,
            simulated_restarts=0, real_aws_restarts=0, total_unique_restarts=0,
            recovery_time_seconds=None, estimated_downtime_seconds=2.8,
            mean_latency_ms=2400.0, p95_latency_ms=4900.0, log_file="t4.jsonl",
        ),
    ]

    aggregates = compute_aggregates(trials)
    assert len(aggregates) == 2

    # Map by policy
    agg_map = {a.policy: a for a in aggregates}

    # Policy A
    agg_a = agg_map["policy_A"]
    assert agg_a.trials_count == 2
    assert agg_a.recovered_trials_count == 2
    assert agg_a.successful_requests_mean == 40.0
    assert agg_a.successful_requests_std == 0.0
    assert agg_a.simulated_restarts_mean == 1.0
    assert agg_a.real_aws_restarts_mean == 0.0
    assert agg_a.recovery_time_mean == 2.5
    assert agg_a.recovery_time_std == 0.71  # stdev of [2.0, 3.0] is ~0.7071

    # Control policy: unrecovered
    agg_c = agg_map["control"]
    assert agg_c.trials_count == 2
    assert agg_c.recovered_trials_count == 0
    assert agg_c.successful_requests_mean == 11.0
    assert agg_c.successful_requests_std == 1.41
    assert agg_c.recovery_time_mean is None  # Must be None, not fabricated!
    assert agg_c.recovery_time_std is None
    assert agg_c.simulated_restarts_mean == 0.0


def test_batch_write_summary_csv_and_tables() -> None:
    """Verifies that write_summary_csv and write_aggregated_table_csv produce valid CSV files."""
    import csv
    from experiment.batch import AggregateStats, TrialResult, compute_aggregates, write_aggregated_table_csv, write_summary_csv

    trials = [
        TrialResult(
            trial_id="run_a", scenario="transient_latency", policy="control", trial_index=1,
            total_requests=50, successful_requests=50, failed_requests=0, success_rate_percent=100.0,
            simulated_restarts=0, real_aws_restarts=0, total_unique_restarts=0,
            recovery_time_seconds=2.1, estimated_downtime_seconds=0.0,
            mean_latency_ms=30.0, p95_latency_ms=50.0, log_file="run_a.jsonl",
        ),
        TrialResult(
            trial_id="run_b", scenario="stalled_progress", policy="control", trial_index=1,
            total_requests=50, successful_requests=5, failed_requests=45, success_rate_percent=10.0,
            simulated_restarts=0, real_aws_restarts=0, total_unique_restarts=0,
            recovery_time_seconds=None, estimated_downtime_seconds=4.5,
            mean_latency_ms=2000.0, p95_latency_ms=4000.0, log_file="run_b.jsonl",
        ),
    ]

    aggregates = compute_aggregates(trials)

    with tempfile.TemporaryDirectory() as tmp_dir:
        summary_csv = os.path.join(tmp_dir, "batch_summary.csv")
        agg_csv = os.path.join(tmp_dir, "batch_agg.csv")

        write_summary_csv(trials, aggregates, summary_csv)
        write_aggregated_table_csv(aggregates, agg_csv)

        assert os.path.exists(summary_csv)
        assert os.path.exists(agg_csv)

        # Inspect summary CSV
        with open(summary_csv, mode="r", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))

        # 2 trial rows + 2 * (1 MEAN + 1 STD) = 6 rows
        assert len(reader) == 6
        trial_rows = [r for r in reader if r["row_type"] == "TRIAL"]
        mean_rows = [r for r in reader if r["row_type"] == "SUMMARY_MEAN"]
        std_rows = [r for r in reader if r["row_type"] == "SUMMARY_STD"]

        assert len(trial_rows) == 2
        assert len(mean_rows) == 2
        assert len(std_rows) == 2

        # Check unrecovered format
        stalled_row = next(r for r in trial_rows if r["scenario"] == "stalled_progress")
        assert stalled_row["recovery_time_seconds"] == "N/A"

        # Check aggregate format
        stalled_mean = next(r for r in mean_rows if r["scenario"] == "stalled_progress")
        assert stalled_mean["recovery_time_seconds"] == "N/A"
        assert stalled_mean["real_aws_restarts"] == "0.00"


def test_batch_unique_trial_ids_and_immutability() -> None:
    """Verifies that batch execution generates distinct trial IDs and separate log files."""
    from application import create_app
    from experiment.batch import BatchConfig, run_batch_experiment

    with tempfile.TemporaryDirectory() as tmp_dir:
        # Run a mini batch: 2 policies, 1 scenario, 2 trials = 4 trials total
        # Using mock/local server
        app = create_app({
            "TESTING": True,
            "EXPERIMENT_MODE": True,
            "FAULT_INJECTION_TOKEN": "test-batch-tok",
        })
        import threading
        from werkzeug.serving import make_server
        server = make_server("127.0.0.1", 5077, app, threaded=True)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        time.sleep(0.3)

        try:
            cfg = BatchConfig(
                policies=["control", "fixed_threshold"],
                scenarios=["transient_latency"],
                trials_per_combination=2,
                duration_seconds=0.5,
                request_interval_seconds=0.1,
                log_dir=tmp_dir,
                base_url="http://127.0.0.1:5077",
                fault_token="test-batch-tok",
            )

            results, aggregates, csv_path = run_batch_experiment(cfg)

            assert len(results) == 4
            trial_ids = [r.trial_id for r in results]
            # Every trial ID must be unique
            assert len(set(trial_ids)) == 4

            # Every trial must have a separate JSONL log file in tmp_dir
            jsonl_files = [f for f in os.listdir(tmp_dir) if f.endswith(".jsonl")]
            assert len(jsonl_files) == 4

            assert os.path.exists(csv_path)
        finally:
            server.shutdown()
            t.join(timeout=1.0)


def test_workload_seed_reproducibility() -> None:
    """Verifies that WorkloadGenerator with identical random seeds produces identical request plans."""
    from experiment.workload import LatencySpikeSchedule, WorkloadGenerator

    spikes = [LatencySpikeSchedule(offset_seconds=1.0, duration_seconds=1.0, degraded_delay_ms=250.0)]
    gen1 = WorkloadGenerator(
        base_url="http://127.0.0.1:5000",
        request_interval_seconds=0.1,
        default_work_delay_ms=20.0,
        random_seed=42,
        arrival_jitter_ratio=0.2,
        latency_jitter_ratio=0.3,
    )
    gen2 = WorkloadGenerator(
        base_url="http://127.0.0.1:5000",
        request_interval_seconds=0.1,
        default_work_delay_ms=20.0,
        random_seed=42,
        arrival_jitter_ratio=0.2,
        latency_jitter_ratio=0.3,
    )
    gen3 = WorkloadGenerator(
        base_url="http://127.0.0.1:5000",
        request_interval_seconds=0.1,
        default_work_delay_ms=20.0,
        random_seed=999,
        arrival_jitter_ratio=0.2,
        latency_jitter_ratio=0.3,
    )

    plan1 = gen1.generate_request_plan(3.0, spikes)
    plan2 = gen2.generate_request_plan(3.0, spikes)
    plan3 = gen3.generate_request_plan(3.0, spikes)

    # Identical seeds must produce bit-for-bit identical schedules
    assert plan1 == plan2
    assert len(plan1) == 30

    # Different seed must produce a different schedule
    assert plan1 != plan3

    # Deterministic baseline mode (no seed, no jitter)
    gen_det = WorkloadGenerator(
        base_url="http://127.0.0.1:5000",
        request_interval_seconds=0.1,
        default_work_delay_ms=20.0,
        random_seed=None,
        arrival_jitter_ratio=0.0,
        latency_jitter_ratio=0.0,
    )
    plan_det = gen_det.generate_request_plan(1.0)
    expected_offsets = [round(i * 0.1, 4) for i in range(10)]
    assert [p[0] for p in plan_det] == expected_offsets
    assert all(p[1] == 20.0 for p in plan_det)


def test_matched_workload_across_policies() -> None:
    """Verifies that across comparison policies, repetition k shares the exact same seed and request plan."""
    from experiment.workload import LatencySpikeSchedule, WorkloadGenerator

    spikes = [LatencySpikeSchedule(offset_seconds=1.0, duration_seconds=1.0, degraded_delay_ms=300.0)]
    base_seed = 12345
    trial_idx = 3
    matched_seed = base_seed + trial_idx * 1000

    control_gen = WorkloadGenerator("http://127.0.0.1:5000", random_seed=matched_seed, arrival_jitter_ratio=0.15, latency_jitter_ratio=0.25)
    fixed_gen = WorkloadGenerator("http://127.0.0.1:5000", random_seed=matched_seed, arrival_jitter_ratio=0.15, latency_jitter_ratio=0.25)
    pg_gen = WorkloadGenerator("http://127.0.0.1:5000", random_seed=matched_seed, arrival_jitter_ratio=0.15, latency_jitter_ratio=0.25)

    control_plan = control_gen.generate_request_plan(3.5, spikes)
    fixed_plan = fixed_gen.generate_request_plan(3.5, spikes)
    pg_plan = pg_gen.generate_request_plan(3.5, spikes)

    assert control_plan == fixed_plan == pg_plan
    assert len(control_plan) == 35


def test_parameterized_scenarios_and_flapping_overload() -> None:
    """Verifies that BatchConfig accepts customized scenario parameters and flapping overload schedules."""
    from experiment.batch import BatchConfig

    cfg = BatchConfig(
        scenarios=["transient_latency", "stalled_progress", "flapping_overload"],
        scenario_params={
            "transient_latency": {"duration_seconds": 1.5, "degraded_delay_ms": 400.0},
            "stalled_progress": {"stall_timeout_seconds": 3.0},
            "flapping_overload": {"burst_count": 3, "burst_duration_seconds": 0.3, "recovery_gap_seconds": 0.2},
        },
        random_seed=777,
        arrival_jitter_ratio=0.1,
        latency_jitter_ratio=0.2,
    )
    assert cfg.scenario_params["transient_latency"]["duration_seconds"] == 1.5
    assert cfg.scenario_params["stalled_progress"]["stall_timeout_seconds"] == 3.0
    assert cfg.scenario_params["flapping_overload"]["burst_count"] == 3
    assert cfg.random_seed == 777


def test_batch_aggregates_with_median_and_unrecovered() -> None:
    """Verifies that compute_aggregates computes median, percentiles, and tracks unrecovered count."""
    from experiment.batch import TrialResult, compute_aggregates

    trials = [
        TrialResult(
            trial_id="r1", scenario="stalled_progress", policy="control", trial_index=1,
            total_requests=35, successful_requests=10, failed_requests=25, success_rate_percent=28.57,
            simulated_restarts=0, real_aws_restarts=0, total_unique_restarts=0,
            recovery_time_seconds=None, estimated_downtime_seconds=2.4,
            mean_latency_ms=1800.0, p95_latency_ms=2500.0, log_file="r1.jsonl",
        ),
        TrialResult(
            trial_id="r2", scenario="stalled_progress", policy="control", trial_index=2,
            total_requests=35, successful_requests=12, failed_requests=23, success_rate_percent=34.29,
            simulated_restarts=0, real_aws_restarts=0, total_unique_restarts=0,
            recovery_time_seconds=None, estimated_downtime_seconds=2.2,
            mean_latency_ms=1750.0, p95_latency_ms=2400.0, log_file="r2.jsonl",
        ),
        TrialResult(
            trial_id="r3", scenario="stalled_progress", policy="control", trial_index=3,
            total_requests=35, successful_requests=10, failed_requests=25, success_rate_percent=28.57,
            simulated_restarts=0, real_aws_restarts=0, total_unique_restarts=0,
            recovery_time_seconds=None, estimated_downtime_seconds=2.5,
            mean_latency_ms=1820.0, p95_latency_ms=2550.0, log_file="r3.jsonl",
        ),
    ]

    aggregates = compute_aggregates(trials)
    assert len(aggregates) == 1
    agg = aggregates[0]
    assert agg.trials_count == 3
    assert agg.recovered_trials_count == 0
    assert agg.unrecovered_trials_count == 3
    assert agg.successful_requests_median == 10.0
    assert agg.recovery_time_mean is None
    assert agg.recovery_time_median is None
    assert agg.estimated_downtime_median == 2.4


