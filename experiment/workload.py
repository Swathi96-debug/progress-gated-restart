"""
Workload generator and fault injection scheduler for controlled experiment scenarios.
Supports deterministic request arrival schedules, transient latency degradation,
and genuine stalled progress faults.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import random
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from experiment.logger import ExperimentLogger

logger = logging.getLogger("progress_gated_restart.workload")


class WorkloadScenario(str, Enum):
    """Experiment benchmark scenarios."""
    NORMAL = "normal"
    TRANSIENT_LATENCY = "transient_latency"
    STALLED_PROGRESS = "stalled_progress"
    TRANSIENT_OVERLOAD = "transient_overload"  # HTTP 503 load-shedding
    FLAPPING_OVERLOAD = "flapping_overload"  # Repeated intermittent overload bursts


@dataclass
class FaultSchedule:
    """Defines when and what fault state to inject via POST /experiment/fault."""
    offset_seconds: float
    fault_state: str  # 'temporary_overload', 'simulated_stalled_progress', 'hard_failure'
    duration_seconds: Optional[float] = 3.0
    stall_timeout_seconds: Optional[float] = 10.0


@dataclass
class LatencySpikeSchedule:
    """
    Defines a deterministic transient latency degradation window.
    Simulates high latency while work requests continue completing successfully.
    """
    offset_seconds: float
    duration_seconds: float
    degraded_delay_ms: float = 250.0


class WorkloadGenerator:
    """
    Sends concurrent HTTP work requests according to a deterministic or seeded
    stochastic schedule, and executes scheduled fault injections.
    Thread-safe and strictly time-bounded.
    """

    def __init__(
        self,
        base_url: str,
        request_interval_seconds: float = 0.1,
        default_work_delay_ms: float = 20.0,
        max_workers: int = 25,
        fault_token: Optional[str] = None,
        logger: Optional[ExperimentLogger] = None,
        random_seed: Optional[int] = None,
        arrival_jitter_ratio: float = 0.0,
        latency_jitter_ratio: float = 0.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.request_interval_seconds = request_interval_seconds
        self.default_work_delay_ms = default_work_delay_ms
        self.max_workers = max_workers
        self.fault_token = fault_token
        self.exp_logger = logger
        self.random_seed = random_seed
        self.arrival_jitter_ratio = max(0.0, min(1.0, float(arrival_jitter_ratio)))
        self.latency_jitter_ratio = max(0.0, min(1.0, float(latency_jitter_ratio)))

        self._stop_event = threading.Event()
        self.total_requests_sent = 0
        self.total_requests_succeeded = 0
        self.total_requests_failed = 0

    def stop(self) -> None:
        self._stop_event.set()

    def _send_work_request(self, delay_ms: float) -> Dict[str, Any]:
        req_id = f"req-{uuid.uuid4().hex[:8]}"
        url = f"{self.base_url}/work?delay_ms={delay_ms}"
        start_perf = time.perf_counter()
        status_code = 0
        success = False
        error_msg: Optional[str] = None

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "WorkloadGenerator/1.0"})
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                status_code = resp.status
                success = (status_code == 200)
        except urllib.error.HTTPError as http_err:
            status_code = http_err.code
            success = False
            error_msg = f"HTTP {http_err.code}"
        except Exception as exc:
            status_code = 0
            success = False
            error_msg = str(exc)

        latency_ms = (time.perf_counter() - start_perf) * 1000.0

        if self.exp_logger:
            self.exp_logger.log_request(
                request_id=req_id,
                endpoint="/work",
                status_code=status_code,
                latency_ms=latency_ms,
                delay_ms=delay_ms,
                success=success,
                error_message=error_msg,
            )

        if success:
            self.total_requests_succeeded += 1
        else:
            self.total_requests_failed += 1
        self.total_requests_sent += 1

        return {
            "request_id": req_id,
            "status_code": status_code,
            "latency_ms": latency_ms,
            "success": success,
        }

    def inject_fault(
        self,
        fault_state: str,
        duration_seconds: Optional[float] = None,
        stall_timeout_seconds: Optional[float] = None,
    ) -> bool:
        """
        Sends authenticated fault injection command to POST /experiment/fault.
        """
        url = f"{self.base_url}/experiment/fault"
        payload: Dict[str, Any] = {"state": fault_state}
        if duration_seconds is not None:
            payload["duration_seconds"] = duration_seconds
        if stall_timeout_seconds is not None:
            payload["stall_timeout_seconds"] = stall_timeout_seconds

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "WorkloadGenerator/1.0",
        }
        if self.fault_token:
            headers["X-Fault-Token"] = self.fault_token

        try:
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                success = (resp.status == 200)
                if self.exp_logger:
                    self.exp_logger.log_fault_event({
                        "timestamp": time.time(),
                        "fault_state": fault_state,
                        "success": success,
                        "payload": payload,
                    })
                return success
        except Exception as exc:
            logger.error("Fault injection failed: %s", exc)
            if self.exp_logger:
                self.exp_logger.log_fault_event({
                    "timestamp": time.time(),
                    "fault_state": fault_state,
                    "success": False,
                    "error": str(exc),
                })
            return False

    def generate_request_plan(
        self,
        duration_seconds: float,
        latency_schedules: Optional[List[LatencySpikeSchedule]] = None,
    ) -> List[Tuple[float, float]]:
        """
        Generates a deterministic or seeded pseudo-random request plan.
        Returns a list of (target_offset_seconds, effective_delay_ms).
        Guarantees that identical seeds yield 100% identical request plans across policies.
        """
        total_requests = int(round(duration_seconds / self.request_interval_seconds))
        spikes = latency_schedules or []
        rng = random.Random(self.random_seed) if self.random_seed is not None else None

        plan: List[Tuple[float, float]] = []
        for i in range(total_requests):
            base_offset = round(i * self.request_interval_seconds, 4)

            if rng is not None and self.arrival_jitter_ratio > 0.0:
                jitter = rng.uniform(-self.arrival_jitter_ratio, self.arrival_jitter_ratio) * self.request_interval_seconds
                offset = max(0.0, min(duration_seconds - 0.01, round(base_offset + jitter, 4)))
            else:
                offset = base_offset

            # Determine base delay from spike schedule
            base_delay = self.default_work_delay_ms
            for spike in spikes:
                if spike.offset_seconds <= offset < (spike.offset_seconds + spike.duration_seconds):
                    base_delay = spike.degraded_delay_ms
                    break

            if rng is not None and self.latency_jitter_ratio > 0.0:
                jitter_delay = rng.uniform(-self.latency_jitter_ratio, self.latency_jitter_ratio) * base_delay
                delay = max(1.0, round(base_delay + jitter_delay, 1))
            else:
                delay = base_delay

            plan.append((offset, delay))

        # Sort plan chronologically by target offset
        plan.sort(key=lambda x: x[0])
        return plan

    def run_workload(
        self,
        duration_seconds: float,
        fault_schedules: Optional[List[FaultSchedule]] = None,
        latency_schedules: Optional[List[LatencySpikeSchedule]] = None,
    ) -> None:
        """
        Executes a deterministic or seeded request schedule.
        Guarantees that identical numbers of requests are dispatched at identical scheduled
        offsets across all policies sharing the same seed, making trials directly comparable.
        """
        start_time = time.time()
        executed_faults = set()
        schedules = fault_schedules or []
        spikes = latency_schedules or []

        plan = self.generate_request_plan(duration_seconds, latency_schedules)
        total_requests = len(plan)

        logger.info(
            "Starting workload generator: %s requests scheduled over %.2fs (interval=%.2fs, seed=%s)...",
            total_requests,
            duration_seconds,
            self.request_interval_seconds,
            self.random_seed,
        )

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = []

            for target_offset, effective_delay in plan:
                if self._stop_event.is_set():
                    break

                target_time = start_time + target_offset

                # Pace dispatch loop precisely according to schedule
                sleep_needed = target_time - time.time()
                if sleep_needed > 0:
                    time.sleep(sleep_needed)

                elapsed = time.time() - start_time

                # Check scheduled fault injections
                for idx, schedule in enumerate(schedules):
                    if idx not in executed_faults and elapsed >= schedule.offset_seconds:
                        logger.info("Executing scheduled fault: %s at offset %.2fs", schedule.fault_state, elapsed)
                        self.inject_fault(
                            fault_state=schedule.fault_state,
                            duration_seconds=schedule.duration_seconds,
                            stall_timeout_seconds=schedule.stall_timeout_seconds,
                        )
                        executed_faults.add(idx)

                # Check scheduled latency spikes for logging
                for idx, spike in enumerate(spikes):
                    spike_key = f"spike_{idx}"
                    if spike_key not in executed_faults and elapsed >= spike.offset_seconds:
                        executed_faults.add(spike_key)
                        logger.info("Starting scheduled latency spike: %.1fms for %.2fs", spike.degraded_delay_ms, spike.duration_seconds)
                        if self.exp_logger:
                            self.exp_logger.log_fault_event({
                                "timestamp": time.time(),
                                "fault_state": "transient_latency_spike",
                                "success": True,
                                "degraded_delay_ms": spike.degraded_delay_ms,
                                "duration_seconds": spike.duration_seconds,
                            })

                f = executor.submit(self._send_work_request, effective_delay)
                futures.append(f)

            # Bounded wait for all in-flight requests to complete
            concurrent.futures.wait(futures, timeout=6.0)

        logger.info("Workload generator finished. Attempted requests dispatched: %s", self.total_requests_sent)
