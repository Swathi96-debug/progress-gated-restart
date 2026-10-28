"""
Restart policy implementations:
1. NoRestartPolicy (Control baseline measuring natural recovery)
2. FixedThresholdPolicy (Restarts immediately when latency trigger is met)
3. ProgressGatedPolicy (Withholds restart while progress occurs; restarts on confirmed stall or hard deadline)
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from experiment.models import DecisionType, PolicyConfig, PolicyDecision, ProgressObservation

logger = logging.getLogger("progress_gated_restart.policies")


class BasePolicy(ABC):
    """
    Abstract base restart policy enforcing common configuration, cooldown checks,
    counter reset handling, and recovery criteria detection.
    """

    def __init__(self, config: PolicyConfig, policy_name: str) -> None:
        self.config = config
        self.policy_name = policy_name

        # State tracking
        self.last_restart_timestamp: float = -1e9
        self.degradation_started_timestamp: Optional[float] = None
        self.is_currently_degraded: bool = False
        self.consecutive_healthy_probes: int = 0
        self.has_recovered: bool = False
        self.degraded_observation_count: int = 0

        # Progress tracking baselines
        self.last_counter: Optional[int] = None
        self.last_pid: Optional[int] = None
        self.last_observation_time: Optional[float] = None

    def process_observation_deltas(self, obs: ProgressObservation) -> ProgressObservation:
        """
        Updates observation with counter delta and detects process changes / counter resets.
        CRITICAL: Counter resets, process changes, missing metrics, and probe failures
        are never automatically interpreted as proof of stalled progress.
        """
        if not obs.probe_success or obs.completion_counter is None:
            # Metrics probe failed or returned no counter
            obs.counter_delta = None
            return obs

        current_counter = obs.completion_counter
        current_pid = obs.pid

        # Check for process identifier change (e.g. process restart)
        if self.last_pid is not None and current_pid is not None and current_pid != self.last_pid:
            obs.is_process_change = True
            logger.info("Process change detected from PID %s to %s", self.last_pid, current_pid)
            self.last_counter = current_counter
            self.last_pid = current_pid
            obs.counter_delta = 0
            return obs

        self.last_pid = current_pid

        # Check for completion counter reset (e.g. server reloaded or restarted)
        if self.last_counter is not None and current_counter < self.last_counter:
            obs.is_counter_reset = True
            logger.info("Counter reset detected (from %s to %s)", self.last_counter, current_counter)
            self.last_counter = current_counter
            obs.counter_delta = 0
            return obs

        # Normal delta computation
        if self.last_counter is not None:
            obs.counter_delta = max(0, current_counter - self.last_counter)
        else:
            # First observation: establish baseline
            obs.counter_delta = 0

        self.last_counter = current_counter
        self.last_observation_time = obs.timestamp
        return obs

    def check_cooldown(self, current_time: float) -> Optional[PolicyDecision]:
        """Ensures that restarts are not triggered during the cooldown period."""
        time_since_restart = current_time - self.last_restart_timestamp
        if time_since_restart < self.config.cooldown_seconds:
            remaining = round(self.config.cooldown_seconds - time_since_restart, 2)
            return PolicyDecision(
                decision=DecisionType.COOLDOWN_ACTIVE,
                reason=f"Restart cooldown active ({remaining}s remaining)",
                timestamp=current_time,
                iso_timestamp=datetime.now(timezone.utc).isoformat(),
                policy_name=self.policy_name,
                metrics={"cooldown_remaining_seconds": remaining},
            )
        return None

    def check_recovery(self, obs: ProgressObservation, current_time: float) -> Optional[PolicyDecision]:
        """
        Monitors whether the application has satisfied the recovery criteria:
        N consecutive healthy probes with low latency.
        Evaluated before cooldown to ensure recovery is not suppressed during post-restart stabilization.
        """
        is_probe_healthy = (
            obs.probe_success
            and obs.is_healthy
            and obs.probe_latency_ms <= self.config.healthy_latency_threshold_ms
            and obs.fault_state == "normal"
        )

        if is_probe_healthy:
            self.consecutive_healthy_probes += 1
            if self.is_currently_degraded and self.consecutive_healthy_probes >= self.config.recovery_consecutive_successes:
                self.is_currently_degraded = False
                self.degradation_started_timestamp = None
                self.degraded_observation_count = 0
                self.has_recovered = True
                return PolicyDecision(
                    decision=DecisionType.RECOVERY_DETECTED,
                    reason=f"Recovery criteria satisfied ({self.consecutive_healthy_probes} consecutive healthy probes)",
                    timestamp=current_time,
                    iso_timestamp=datetime.now(timezone.utc).isoformat(),
                    policy_name=self.policy_name,
                    metrics={"consecutive_healthy": self.consecutive_healthy_probes},
                )
        else:
            self.consecutive_healthy_probes = 0

        return None

    def record_restart_executed(self, current_time: float) -> None:
        """Records that a restart operation was initiated."""
        self.last_restart_timestamp = current_time
        self.degradation_started_timestamp = None
        self.degraded_observation_count = 0
        self.consecutive_healthy_probes = 0
        # When a restart executes, the system was degraded and is now in post-restart recovery
        self.is_currently_degraded = True
        self.has_recovered = False
        # Reset counter baselines on restart
        self.last_counter = None
        self.last_pid = None

    @abstractmethod
    def evaluate(self, obs: ProgressObservation, current_time: float) -> PolicyDecision:
        """Evaluates progress observation and returns policy decision."""
        pass


class NoRestartPolicy(BasePolicy):
    """
    Control baseline policy.
    Never triggers a restart under any circumstance.
    Measures natural recovery behavior under identical workloads and fault schedules.
    """

    def __init__(self, config: PolicyConfig) -> None:
        super().__init__(config, policy_name="NoRestartPolicy")

    def evaluate(self, obs: ProgressObservation, current_time: float) -> PolicyDecision:
        obs = self.process_observation_deltas(obs)

        # 1. Check recovery criteria first (consistent with FixedThreshold and ProgressGated)
        recovery_decision = self.check_recovery(obs, current_time)
        if recovery_decision:
            return recovery_decision

        # 2. Update degradation tracking
        is_degraded = (
            not obs.probe_success
            or not obs.is_healthy
            or obs.probe_latency_ms > self.config.latency_threshold_ms
            or obs.fault_state != "normal"
        )

        if is_degraded:
            if not self.is_currently_degraded:
                self.is_currently_degraded = True
                self.degradation_started_timestamp = current_time
            self.consecutive_healthy_probes = 0

        return PolicyDecision(
            decision=DecisionType.NO_ACTION,
            reason="NoRestart control policy: observing natural behavior without intervention",
            timestamp=current_time,
            iso_timestamp=datetime.now(timezone.utc).isoformat(),
            policy_name=self.policy_name,
            metrics={
                "counter_delta": obs.counter_delta,
                "is_degraded": self.is_currently_degraded,
            },
        )


class FixedThresholdPolicy(BasePolicy):
    """
    Fixed-Threshold restart policy.
    When the latency trigger is met (latency > latency_threshold_ms or health probe failure),
    requests a restart immediately, subject to shared cooldown and duplicate-restart protection.
    Does not evaluate progress counter deltas or wait for a progress observation deadline.
    """

    def __init__(self, config: PolicyConfig) -> None:
        super().__init__(config, policy_name="FixedThresholdPolicy")

    def evaluate(self, obs: ProgressObservation, current_time: float) -> PolicyDecision:
        obs = self.process_observation_deltas(obs)

        # 1. Check recovery criteria first (so recovery during post-restart stabilization is detected)
        recovery_decision = self.check_recovery(obs, current_time)
        if recovery_decision:
            return recovery_decision

        # 2. Check cooldown second (prevent triggering another restart during cooldown)
        cooldown_decision = self.check_cooldown(current_time)
        if cooldown_decision:
            return cooldown_decision

        # 3. Check trigger condition: latency exceeds threshold or probe fails
        is_trigger_met = (
            not obs.probe_success
            or not obs.is_healthy
            or obs.probe_latency_ms > self.config.latency_threshold_ms
            or obs.fault_state != "normal"
        )

        if is_trigger_met:
            self.is_currently_degraded = True
            if self.degradation_started_timestamp is None:
                self.degradation_started_timestamp = current_time

            return PolicyDecision(
                decision=DecisionType.INITIATE_RESTART,
                reason=(
                    f"Fixed threshold triggered immediately: latency {obs.probe_latency_ms:.1f}ms "
                    f"> threshold {self.config.latency_threshold_ms:.1f}ms (or health failure)"
                ),
                timestamp=current_time,
                iso_timestamp=datetime.now(timezone.utc).isoformat(),
                policy_name=self.policy_name,
                metrics={
                    "probe_latency_ms": obs.probe_latency_ms,
                    "latency_threshold_ms": self.config.latency_threshold_ms,
                    "fault_state": obs.fault_state,
                    "is_healthy": obs.is_healthy,
                },
            )

        return PolicyDecision(
            decision=DecisionType.NO_ACTION,
            reason="Performance within normal operational thresholds",
            timestamp=current_time,
            iso_timestamp=datetime.now(timezone.utc).isoformat(),
            policy_name=self.policy_name,
        )


class ProgressGatedPolicy(BasePolicy):
    """
    Progress-Gated restart policy.
    When the common latency trigger is met, observes progress for a bounded interval;
    continues waiting while progress occurs (counter delta > 0) and recovery remains possible,
    but restarts on confirmed stalled progress (counter delta == 0) or when the hard deadline expires.
    """

    def __init__(self, config: PolicyConfig) -> None:
        super().__init__(config, policy_name="ProgressGatedPolicy")

    def evaluate(self, obs: ProgressObservation, current_time: float) -> PolicyDecision:
        obs = self.process_observation_deltas(obs)

        # 1. Check recovery criteria first (so natural recovery or post-restart recovery is detected)
        recovery_decision = self.check_recovery(obs, current_time)
        if recovery_decision:
            return recovery_decision

        # 2. Check cooldown second (prevent duplicate restart triggers during cooldown)
        cooldown_decision = self.check_cooldown(current_time)
        if cooldown_decision:
            return cooldown_decision

        # 3. Check trigger condition (identical latency threshold and degradation conditions)
        is_trigger_met = (
            not obs.probe_success
            or not obs.is_healthy
            or obs.probe_latency_ms > self.config.latency_threshold_ms
            or obs.fault_state != "normal"
        )

        if not is_trigger_met:
            return PolicyDecision(
                decision=DecisionType.NO_ACTION,
                reason="Performance within normal operational thresholds",
                timestamp=current_time,
                iso_timestamp=datetime.now(timezone.utc).isoformat(),
                policy_name=self.policy_name,
            )

        # Degradation trigger is met
        if not self.is_currently_degraded or self.degradation_started_timestamp is None:
            self.is_currently_degraded = True
            self.degradation_started_timestamp = current_time
            self.degraded_observation_count = 0

        degraded_duration = current_time - self.degradation_started_timestamp
        self.degraded_observation_count += 1

        # 4. Check hard deadline upper bound:
        if degraded_duration >= self.config.hard_deadline_seconds:
            return PolicyDecision(
                decision=DecisionType.INITIATE_RESTART,
                reason=(
                    f"Progress-gated hard deadline expired: degradation sustained for {degraded_duration:.2f}s "
                    f">= hard deadline {self.config.hard_deadline_seconds:.2f}s"
                ),
                timestamp=current_time,
                iso_timestamp=datetime.now(timezone.utc).isoformat(),
                policy_name=self.policy_name,
                metrics={
                    "degraded_duration_seconds": round(degraded_duration, 2),
                    "hard_deadline_seconds": self.config.hard_deadline_seconds,
                    "counter_delta": obs.counter_delta,
                },
            )

        # 5. Handle probe failures or missing metrics safely:
        if not obs.probe_success or obs.completion_counter is None:
            return PolicyDecision(
                decision=DecisionType.OBSERVE_PROGRESS,
                reason="Probe failure or missing metrics; withholding stall conclusion",
                timestamp=current_time,
                iso_timestamp=datetime.now(timezone.utc).isoformat(),
                policy_name=self.policy_name,
                metrics={"degraded_duration_seconds": round(degraded_duration, 2)},
            )

        # 6. Handle counter resets or process changes:
        if obs.is_counter_reset or obs.is_process_change:
            return PolicyDecision(
                decision=DecisionType.OBSERVE_PROGRESS,
                reason="Counter reset or process change detected; re-establishing baseline",
                timestamp=current_time,
                iso_timestamp=datetime.now(timezone.utc).isoformat(),
                policy_name=self.policy_name,
                metrics={"is_counter_reset": obs.is_counter_reset, "is_process_change": obs.is_process_change},
            )

        # 7. Evaluate progress deltas:
        # If work is actively completing (delta > 0), withhold restart while recovery remains possible
        if obs.counter_delta is not None and obs.counter_delta > 0:
            return PolicyDecision(
                decision=DecisionType.OBSERVE_PROGRESS,
                reason=(
                    f"Forward progress observed (delta={obs.counter_delta} > 0); "
                    f"continuing wait while progress occurs and recovery remains possible"
                ),
                timestamp=current_time,
                iso_timestamp=datetime.now(timezone.utc).isoformat(),
                policy_name=self.policy_name,
                metrics={
                    "counter_delta": obs.counter_delta,
                    "completion_counter": obs.completion_counter,
                    "degraded_duration_seconds": round(degraded_duration, 2),
                },
            )

        # 8. Check confirmed stalled progress (delta == 0):
        if degraded_duration < self.config.observation_interval_seconds and self.degraded_observation_count <= 1:
            return PolicyDecision(
                decision=DecisionType.OBSERVE_PROGRESS,
                reason=(
                    f"Observing progress over initial bounded interval "
                    f"({degraded_duration:.2f}s < {self.config.observation_interval_seconds:.2f}s)"
                ),
                timestamp=current_time,
                iso_timestamp=datetime.now(timezone.utc).isoformat(),
                policy_name=self.policy_name,
                metrics={"degraded_duration_seconds": round(degraded_duration, 2)},
            )

        # Confirmed stall after observation interval
        return PolicyDecision(
            decision=DecisionType.INITIATE_RESTART,
            reason=(
                f"Confirmed stalled progress: delta=0 observed after "
                f"{degraded_duration:.2f}s (observation interval {self.config.observation_interval_seconds:.2f}s)"
            ),
            timestamp=current_time,
            iso_timestamp=datetime.now(timezone.utc).isoformat(),
            policy_name=self.policy_name,
            metrics={
                "counter_delta": 0,
                "degraded_duration_seconds": round(degraded_duration, 2),
                "observation_interval_seconds": self.config.observation_interval_seconds,
            },
        )
