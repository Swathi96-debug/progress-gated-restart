"""
Data structures, enums, and configuration models for the experiment framework.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class DecisionType(str, Enum):
    """Possible outcomes of a policy evaluation."""
    NO_ACTION = "NO_ACTION"
    OBSERVE_PROGRESS = "OBSERVE_PROGRESS"
    INITIATE_RESTART = "INITIATE_RESTART"
    COOLDOWN_ACTIVE = "COOLDOWN_ACTIVE"
    RECOVERY_DETECTED = "RECOVERY_DETECTED"


@dataclass(frozen=True)
class PolicyConfig:
    """
    Common parameter configuration across all restart policies.
    Guarantees experimental validity by ensuring policies share identical thresholds.
    """
    # Latency boundary in milliseconds above which a request or probe is considered degraded
    latency_threshold_ms: float = 300.0

    # Observation interval in seconds between periodic progress probes
    observation_interval_seconds: float = 1.0

    # Maximum duration in seconds that degradation or stall is tolerated before restart
    hard_deadline_seconds: float = 4.0

    # Minimum cooldown period in seconds between successive restarts to prevent reboot storms
    cooldown_seconds: float = 5.0

    # Number of consecutive healthy probes required to declare recovery
    recovery_consecutive_successes: int = 3

    # Maximum acceptable latency for a probe during recovery verification
    healthy_latency_threshold_ms: float = 150.0


@dataclass
class ProgressObservation:
    """
    Represents an atomic observation of server progress and liveness at a point in time.
    """
    timestamp: float
    iso_timestamp: str
    completion_counter: Optional[int]
    counter_delta: Optional[int] = None
    pid: Optional[int] = None
    uptime_seconds: Optional[float] = None
    is_healthy: bool = True
    fault_state: str = "normal"
    probe_success: bool = True
    probe_latency_ms: float = 0.0
    is_counter_reset: bool = False
    is_process_change: bool = False
    error_message: Optional[str] = None


@dataclass
class PolicyDecision:
    """
    Actionable outcome produced by a restart policy.
    """
    decision: DecisionType
    reason: str
    timestamp: float
    iso_timestamp: str
    policy_name: str
    metrics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RestartResult:
    """
    Result of an executed restart operation.
    Explicitly distinguishes simulated restarts from real AWS Elastic Beanstalk operations.
    """
    success: bool
    adapter_type: str  # 'simulated' or 'aws_elastic_beanstalk'
    is_simulated: bool
    started_at: str
    completed_at: str
    duration_seconds: float
    restart_id: str
    reason: str
    details: Dict[str, Any] = field(default_factory=dict)
    error_message: Optional[str] = None
