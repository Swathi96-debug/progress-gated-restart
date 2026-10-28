"""
Orchestrates end-to-end experiment benchmark executions with bounded durations.
Coordinates workload generation, background policy monitoring, and immutable logging.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

from experiment.adapters import RestartAdapter, SimulatedRestartAdapter
from experiment.analysis import analyze_experiment_log
from experiment.controller import RestartController
from experiment.logger import ExperimentLogger
from experiment.models import PolicyConfig
from experiment.policies import BasePolicy, FixedThresholdPolicy, NoRestartPolicy, ProgressGatedPolicy
from experiment.workload import FaultSchedule, LatencySpikeSchedule, WorkloadGenerator

logger = logging.getLogger("progress_gated_restart.runner")


class ExperimentRunner:
    """
    Executes bounded experiment runs under controlled workload and fault schedules.
    """

    def __init__(
        self,
        base_url: str,
        policy_name: str,
        config: Optional[PolicyConfig] = None,
        adapter: Optional[RestartAdapter] = None,
        fault_token: Optional[str] = None,
        log_dir: str = "results",
        run_id: Optional[str] = None,
        probe_timeout_seconds: float = 2.0,
        request_interval_seconds: float = 0.1,
        default_work_delay_ms: float = 20.0,
        random_seed: Optional[int] = None,
        arrival_jitter_ratio: float = 0.0,
        latency_jitter_ratio: float = 0.0,
        scenario_name: Optional[str] = None,
        scenario_params: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.base_url = base_url
        self.policy_config = config or PolicyConfig()
        self.adapter = adapter or SimulatedRestartAdapter()
        self.fault_token = fault_token
        self.exp_logger = ExperimentLogger(log_dir=log_dir, run_id=run_id)

        # Log complete trial metadata, seeds, and configuration
        self.exp_logger.log_trial_metadata({
            "trial_id": run_id,
            "policy_name": policy_name,
            "scenario": scenario_name or "unknown",
            "random_seed": random_seed,
            "arrival_jitter_ratio": arrival_jitter_ratio,
            "latency_jitter_ratio": latency_jitter_ratio,
            "scenario_params": scenario_params or {},
            "policy_config": {
                "latency_threshold_ms": self.policy_config.latency_threshold_ms,
                "observation_interval_seconds": self.policy_config.observation_interval_seconds,
                "hard_deadline_seconds": self.policy_config.hard_deadline_seconds,
                "cooldown_seconds": self.policy_config.cooldown_seconds,
                "recovery_consecutive_successes": self.policy_config.recovery_consecutive_successes,
                "healthy_latency_threshold_ms": self.policy_config.healthy_latency_threshold_ms,
            },
        })

        # Policy instantiation
        if policy_name.lower() in ("norestart", "no_restart", "control"):
            self.policy: BasePolicy = NoRestartPolicy(self.policy_config)
        elif policy_name.lower() in ("fixedthreshold", "fixed_threshold"):
            self.policy = FixedThresholdPolicy(self.policy_config)
        elif policy_name.lower() in ("progressgated", "progress_gated"):
            self.policy = ProgressGatedPolicy(self.policy_config)
        else:
            raise ValueError(f"Unknown policy name: {policy_name}")

        self.controller = RestartController(
            policy=self.policy,
            adapter=self.adapter,
            logger=self.exp_logger,
            probe_timeout_seconds=probe_timeout_seconds,
        )

        self.workload = WorkloadGenerator(
            base_url=self.base_url,
            request_interval_seconds=request_interval_seconds,
            default_work_delay_ms=default_work_delay_ms,
            fault_token=self.fault_token,
            logger=self.exp_logger,
            random_seed=random_seed,
            arrival_jitter_ratio=arrival_jitter_ratio,
            latency_jitter_ratio=latency_jitter_ratio,
        )

    def run(
        self,
        duration_seconds: float = 10.0,
        fault_schedules: Optional[List[FaultSchedule]] = None,
        latency_schedules: Optional[List[LatencySpikeSchedule]] = None,
    ) -> Dict[str, Any]:
        """
        Executes bounded benchmark run. Strictly bounded by duration_seconds.
        """
        stop_event = threading.Event()

        # Run monitoring loop on a daemon thread
        monitor_thread = threading.Thread(
            target=self.controller.run_monitoring_loop,
            args=(self.base_url, duration_seconds, stop_event),
            daemon=True,
        )
        monitor_thread.start()

        # Run workload on main thread (which also handles fault injection & latency spikes)
        try:
            self.workload.run_workload(
                duration_seconds=duration_seconds,
                fault_schedules=fault_schedules,
                latency_schedules=latency_schedules,
            )
        finally:
            stop_event.set()
            self.workload.stop()
            monitor_thread.join(timeout=3.0)

        # Produce and return analysis summary
        return analyze_experiment_log(self.exp_logger.jsonl_path)
