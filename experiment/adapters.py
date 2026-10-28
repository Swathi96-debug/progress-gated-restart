"""
Restart adapter interface and concrete implementations for simulated local testing
and AWS Elastic Beanstalk environment operations.
"""

from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from experiment.models import RestartResult


class RestartAdapter(ABC):
    """Abstract base class defining the contract for restarting an application server."""

    @abstractmethod
    def restart(self, reason: str = "") -> RestartResult:
        """
        Executes an application server restart.
        Must return a structured RestartResult detailing success, duration, and simulation status.
        """
        pass

    @property
    @abstractmethod
    def is_simulated(self) -> bool:
        """Boolean flag indicating whether the restart is simulated or real infrastructure."""
        pass

    @property
    @abstractmethod
    def adapter_type(self) -> str:
        """String identifier for the adapter implementation."""
        pass


class SimulatedRestartAdapter(RestartAdapter):
    """
    Mock restart adapter for deterministic, zero-cost local development and automated testing.
    Explicitly labeled as simulated. Never combined with real AWS measurements.

    BEHAVIOR & MUTATION SEMANTICS:
    - If `on_restart_callback` is provided (e.g. in test suites or simulate_local.py),
      this adapter executes the callback to actually mutate local server state (such as
      resetting application fault state or clearing counters).
    - If `on_restart_callback` is None, this adapter ONLY records the restart event and
      simulates execution latency without altering the running server process state.
    """

    def __init__(
        self,
        on_restart_callback: Optional[Callable[[], None]] = None,
        simulated_delay_seconds: float = 0.05,
    ) -> None:
        self._on_restart_callback = on_restart_callback
        self.simulated_delay_seconds = simulated_delay_seconds
        self.restart_history: list[RestartResult] = []

    @property
    def is_simulated(self) -> bool:
        return True

    @property
    def adapter_type(self) -> str:
        return "simulated"

    def restart(self, reason: str = "") -> RestartResult:
        restart_id = f"sim-{uuid.uuid4().hex[:8]}"
        started_iso = datetime.now(timezone.utc).isoformat()
        start_perf = time.perf_counter()

        # Simulate restart latency / restart downtime
        if self.simulated_delay_seconds > 0:
            time.sleep(self.simulated_delay_seconds)

        # Trigger hook if provided to mutate state
        callback_error: Optional[str] = None
        state_mutated = False
        if self._on_restart_callback:
            try:
                self._on_restart_callback()
                state_mutated = True
            except Exception as exc:
                callback_error = str(exc)

        duration = time.perf_counter() - start_perf
        completed_iso = datetime.now(timezone.utc).isoformat()

        result = RestartResult(
            success=(callback_error is None),
            adapter_type=self.adapter_type,
            is_simulated=self.is_simulated,
            started_at=started_iso,
            completed_at=completed_iso,
            duration_seconds=round(duration, 4),
            restart_id=restart_id,
            reason=reason,
            details={
                "simulated_delay_seconds": self.simulated_delay_seconds,
                "state_mutated": state_mutated,
                "callback_configured": self._on_restart_callback is not None,
            },
            error_message=callback_error,
        )
        self.restart_history.append(result)
        return result


class ElasticBeanstalkRestartAdapter(RestartAdapter):
    """
    AWS Elastic Beanstalk restart adapter utilizing the official Boto3 SDK.

    ARCHITECTURE & PLATFORM NOTES:
    - This adapter calls AWS Elastic Beanstalk `RestartAppServer`.
    - This restarts the application container server (e.g. Gunicorn, Nginx, Apache)
      on EVERY EC2 instance within the target environment.
    - It does NOT reboot the underlying EC2 host, nor does it perform kernel modifications.
    - We DO NOT claim that it necessarily restarts a particular Python process until that
      behavior has been empirically verified and benchmarked on the chosen AWS platform.
    """

    def __init__(
        self,
        environment_name: str,
        region_name: str = "us-east-1",
        boto3_client: Optional[Any] = None,
    ) -> None:
        self.environment_name = environment_name
        self.region_name = region_name
        self._client = boto3_client
        self.restart_history: list[RestartResult] = []

    @property
    def is_simulated(self) -> bool:
        return False

    @property
    def adapter_type(self) -> str:
        return "aws_elastic_beanstalk"

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        import boto3
        self._client = boto3.client("elasticbeanstalk", region_name=self.region_name)
        return self._client

    def restart(self, reason: str = "") -> RestartResult:
        restart_id = f"aws-eb-{uuid.uuid4().hex[:8]}"
        started_iso = datetime.now(timezone.utc).isoformat()
        start_perf = time.perf_counter()

        success = False
        error_msg: Optional[str] = None
        details: Dict[str, Any] = {
            "environment_name": self.environment_name,
            "region": self.region_name,
            "api_action": "RestartAppServer",
            "scope": "all_instances_in_environment",
        }

        try:
            client = self._get_client()
            response = client.restart_app_server(EnvironmentName=self.environment_name)
            success = True
            details["aws_response"] = {
                "http_status": response.get("ResponseMetadata", {}).get("HTTPStatusCode"),
                "request_id": response.get("ResponseMetadata", {}).get("RequestId"),
            }
        except Exception as exc:
            success = False
            error_msg = f"AWS Elastic Beanstalk RestartAppServer failed: {exc}"

        duration = time.perf_counter() - start_perf
        completed_iso = datetime.now(timezone.utc).isoformat()

        result = RestartResult(
            success=success,
            adapter_type=self.adapter_type,
            is_simulated=self.is_simulated,
            started_at=started_iso,
            completed_at=completed_iso,
            duration_seconds=round(duration, 4),
            restart_id=restart_id,
            reason=reason,
            details=details,
            error_message=error_msg,
        )
        self.restart_history.append(result)
        return result
