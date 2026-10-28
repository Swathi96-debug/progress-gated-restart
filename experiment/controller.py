"""
Restart controller implementing progress monitoring, policy evaluation,
duplicate restart prevention, and structured event dispatching.
Kept strictly separate from the Flask web application.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from experiment.adapters import RestartAdapter
from experiment.logger import ExperimentLogger
from experiment.models import DecisionType, PolicyDecision, ProgressObservation
from experiment.policies import BasePolicy

logger = logging.getLogger("progress_gated_restart.controller")


class RestartController:
    """
    Independent control agent that monitors application progress,
    evaluates restart policies, and executes guarded, non-overlapping restarts.
    """

    def __init__(
        self,
        policy: BasePolicy,
        adapter: RestartAdapter,
        logger: Optional[ExperimentLogger] = None,
        probe_timeout_seconds: float = 2.0,
    ) -> None:
        self.policy = policy
        self.adapter = adapter
        self.exp_logger = logger
        self.probe_timeout_seconds = probe_timeout_seconds

        # Concurrency guard: prevents overlapping / duplicate restart operations
        self._restart_lock = threading.Lock()
        self._restart_in_progress: bool = False

        self.last_observation: Optional[ProgressObservation] = None
        self.total_restarts_triggered: int = 0

    @property
    def is_restart_in_progress(self) -> bool:
        with self._restart_lock:
            return self._restart_in_progress

    def probe_server(self, base_url: str) -> ProgressObservation:
        """
        Polls /health and /metrics with bounded timeouts.
        Returns a structured ProgressObservation.
        """
        now_ts = time.time()
        iso_ts = datetime.now(timezone.utc).isoformat()
        start_perf = time.perf_counter()

        metrics_url = f"{base_url.rstrip('/')}/metrics"
        health_url = f"{base_url.rstrip('/')}/health"

        health_data: Dict[str, Any] = {}
        metrics_data: Dict[str, Any] = {}
        probe_error: Optional[str] = None
        is_healthy = False

        # 1. Probe /health
        try:
            req_health = urllib.request.Request(health_url, headers={"User-Agent": "RestartController/1.0"})
            with urllib.request.urlopen(req_health, timeout=self.probe_timeout_seconds) as resp:
                if resp.status == 200:
                    health_data = json.loads(resp.read().decode("utf-8"))
                    is_healthy = (health_data.get("status") == "healthy")
        except urllib.error.HTTPError as http_err:
            probe_error = f"Health HTTP error: {http_err.code}"
            is_healthy = False
        except Exception as exc:
            probe_error = f"Health connection error: {exc}"
            is_healthy = False

        # 2. Probe /metrics
        try:
            req_metrics = urllib.request.Request(metrics_url, headers={"User-Agent": "RestartController/1.0"})
            with urllib.request.urlopen(req_metrics, timeout=self.probe_timeout_seconds) as resp:
                if resp.status == 200:
                    metrics_data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as http_err:
            probe_error = (probe_error + f" | Metrics HTTP error: {http_err.code}") if probe_error else f"Metrics HTTP error: {http_err.code}"
        except Exception as exc:
            probe_error = (probe_error + f" | Metrics error: {exc}") if probe_error else f"Metrics error: {exc}"

        probe_latency_ms = (time.perf_counter() - start_perf) * 1000.0

        probe_success = bool(metrics_data and ("completion_counter" in metrics_data))
        fault_state = metrics_data.get("current_fault_state") or health_data.get("fault_state") or "unknown"

        # Use reported work request latency from telemetry if available; otherwise use probe roundtrip latency
        reported_work_latency = metrics_data.get("last_work_latency_ms")
        effective_latency_ms = (
            float(reported_work_latency)
            if reported_work_latency is not None and float(reported_work_latency) > 0
            else round(probe_latency_ms, 2)
        )

        observation = ProgressObservation(
            timestamp=now_ts,
            iso_timestamp=iso_ts,
            completion_counter=metrics_data.get("completion_counter"),
            pid=metrics_data.get("pid") or health_data.get("pid"),
            uptime_seconds=metrics_data.get("uptime_seconds"),
            is_healthy=is_healthy,
            fault_state=fault_state,
            probe_success=probe_success,
            probe_latency_ms=effective_latency_ms,
            error_message=probe_error,
        )

        self.last_observation = observation
        if self.exp_logger:
            self.exp_logger.log_progress_observation({
                "timestamp": now_ts,
                "iso_timestamp": iso_ts,
                "completion_counter": observation.completion_counter,
                "pid": observation.pid,
                "uptime_seconds": observation.uptime_seconds,
                "is_healthy": observation.is_healthy,
                "fault_state": observation.fault_state,
                "probe_success": observation.probe_success,
                "probe_latency_ms": observation.probe_latency_ms,
                "error_message": observation.error_message,
            })

        return observation

    def trigger_restart(self, reason: str, current_time: float) -> Optional[Any]:
        """
        Executes a guarded restart if no restart is currently active.
        Thread-safe; prevents overlapping/concurrent restart attempts.
        """
        with self._restart_lock:
            if self._restart_in_progress:
                logger.warning("Restart already in progress; rejecting duplicate trigger request.")
                return None
            self._restart_in_progress = True

        try:
            logger.info("Executing restart via %s: %s", self.adapter.adapter_type, reason)
            restart_result = self.adapter.restart(reason=reason)
            self.total_restarts_triggered += 1
            self.policy.record_restart_executed(current_time)

            if self.exp_logger:
                self.exp_logger.log_restart_event({
                    "timestamp": current_time,
                    "iso_timestamp": datetime.now(timezone.utc).isoformat(),
                    "restart_id": restart_result.restart_id,
                    "adapter_type": restart_result.adapter_type,
                    "is_simulated": restart_result.is_simulated,
                    "success": restart_result.success,
                    "duration_seconds": restart_result.duration_seconds,
                    "reason": reason,
                    "error_message": restart_result.error_message,
                })
            return restart_result
        finally:
            with self._restart_lock:
                self._restart_in_progress = False

    def step(self, base_url: str) -> PolicyDecision:
        """
        Performs one single observation and policy evaluation cycle.
        """
        current_time = time.time()
        obs = self.probe_server(base_url)
        decision = self.policy.evaluate(obs, current_time)

        if self.exp_logger:
            self.exp_logger.log_policy_decision({
                "timestamp": current_time,
                "iso_timestamp": decision.iso_timestamp,
                "policy_name": decision.policy_name,
                "decision": decision.decision.value,
                "reason": decision.reason,
                "metrics": decision.metrics,
            })

        if decision.decision == DecisionType.INITIATE_RESTART:
            self.trigger_restart(reason=decision.reason, current_time=current_time)

        elif decision.decision == DecisionType.RECOVERY_DETECTED:
            if self.exp_logger:
                self.exp_logger.log_recovery_event({
                    "timestamp": current_time,
                    "iso_timestamp": decision.iso_timestamp,
                    "policy_name": decision.policy_name,
                    "reason": decision.reason,
                    "metrics": decision.metrics,
                })

        return decision

    def run_monitoring_loop(
        self,
        base_url: str,
        duration_seconds: float,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        """
        Runs bounded progress observation loop.
        Will never run beyond duration_seconds.
        """
        start_time = time.time()
        end_time = start_time + duration_seconds
        interval = self.policy.config.observation_interval_seconds

        logger.info(
            "Starting bounded monitoring loop for %s (max %s seconds)...",
            self.policy.policy_name,
            duration_seconds,
        )

        while time.time() < end_time:
            if stop_event and stop_event.is_set():
                break

            loop_step_start = time.time()
            self.step(base_url)

            # Sleep remainder of observation interval
            elapsed = time.time() - loop_step_start
            sleep_time = max(0.05, interval - elapsed)
            time.sleep(min(sleep_time, max(0.0, end_time - time.time())))

        logger.info("Monitoring loop finished.")
