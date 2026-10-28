"""
Thread-safe, append-only structured logger for experiment events.
Outputs immutable JSONL and CSV event records with timestamps and trial identifiers.
"""

from __future__ import annotations

import csv
import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional


class ExperimentLogger:
    """
    Manages structured, immutable event logging for experiment runs.
    Ensures every restart, request failure, and recovery event has a timestamp
    and unique trial identifier.
    """

    def __init__(self, log_dir: str = "results", run_id: Optional[str] = None) -> None:
        self.log_dir = log_dir
        os.makedirs(self.log_dir, exist_ok=True)

        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.run_id = run_id or f"run_{timestamp_str}"
        self.trial_id = self.run_id
        self.jsonl_path = os.path.join(self.log_dir, f"{self.run_id}.jsonl")
        self.requests_csv_path = os.path.join(self.log_dir, f"{self.run_id}_requests.csv")

        self._lock = threading.Lock()
        self._init_csv()

    def _init_csv(self) -> None:
        """Initializes the CSV header for high-volume request telemetry."""
        with self._lock:
            if not os.path.exists(self.requests_csv_path):
                with open(self.requests_csv_path, mode="w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        "trial_id",
                        "timestamp",
                        "iso_timestamp",
                        "request_id",
                        "endpoint",
                        "status_code",
                        "latency_ms",
                        "delay_ms",
                        "success",
                        "error_message",
                    ])

    def _append_jsonl(self, record: Dict[str, Any]) -> None:
        with self._lock:
            with open(self.jsonl_path, mode="a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")

    def _prepare_record(self, event_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        now_ts = datetime.now(timezone.utc)
        record = {
            "trial_id": self.trial_id,
            "event_type": event_type,
            "timestamp": now_ts.timestamp(),
            "iso_timestamp": now_ts.isoformat(),
        }
        record.update(data)
        return record

    def log_request(
        self,
        request_id: str,
        endpoint: str,
        status_code: int,
        latency_ms: float,
        delay_ms: float,
        success: bool,
        error_message: Optional[str] = None,
    ) -> None:
        now_ts = datetime.now(timezone.utc)
        record = {
            "trial_id": self.trial_id,
            "event_type": "request",
            "timestamp": now_ts.timestamp(),
            "iso_timestamp": now_ts.isoformat(),
            "request_id": request_id,
            "endpoint": endpoint,
            "status_code": status_code,
            "latency_ms": round(latency_ms, 2),
            "delay_ms": delay_ms,
            "success": success,
            "error_message": error_message,
        }
        self._append_jsonl(record)

        with self._lock:
            with open(self.requests_csv_path, mode="a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    self.trial_id,
                    record["timestamp"],
                    record["iso_timestamp"],
                    request_id,
                    endpoint,
                    status_code,
                    record["latency_ms"],
                    delay_ms,
                    1 if success else 0,
                    error_message or "",
                ])

    def log_progress_observation(self, observation_data: Dict[str, Any]) -> None:
        record = self._prepare_record("progress_observation", observation_data)
        self._append_jsonl(record)

    def log_policy_decision(self, decision_data: Dict[str, Any]) -> None:
        record = self._prepare_record("policy_decision", decision_data)
        self._append_jsonl(record)

    def log_restart_event(self, restart_data: Dict[str, Any]) -> None:
        record = self._prepare_record("restart_event", restart_data)
        self._append_jsonl(record)

    def log_fault_event(self, fault_data: Dict[str, Any]) -> None:
        record = self._prepare_record("fault_event", fault_data)
        self._append_jsonl(record)

    def log_recovery_event(self, recovery_data: Dict[str, Any]) -> None:
        record = self._prepare_record("recovery_event", recovery_data)
        self._append_jsonl(record)

    def log_trial_metadata(self, metadata: Dict[str, Any]) -> None:
        """Logs experiment run configuration, parameters, and random seed."""
        record = self._prepare_record("trial_metadata", metadata)
        self._append_jsonl(record)
