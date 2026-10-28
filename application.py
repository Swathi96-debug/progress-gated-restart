"""
Progress-Gated Application-Server Restart Under Transient Load
Operating Systems Case Study — BTech 3rd Year

AWS Elastic Beanstalk Compatible Flask Foundation Application.
This module exposes 'application' as the WSGI callable.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import csv
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, Response, jsonify, redirect, render_template, request, send_file, send_from_directory

# ==============================================================================
# Structured Logging Setup
# ==============================================================================

class StructuredJsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects with UTC timestamps."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "pid": os.getpid(),
            "thread_id": threading.get_ident(),
        }
        if hasattr(record, "structured_data") and isinstance(record.structured_data, dict):
            log_entry.update(record.structured_data)
        return json.dumps(log_entry)


logger = logging.getLogger("progress_gated_restart")
logger.setLevel(logging.INFO)

# Avoid duplicate handlers if reloaded
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredJsonFormatter())
    logger.addHandler(handler)


def log_event(event_name: str, level: int = logging.INFO, **kwargs: Any) -> None:
    """Helper to emit structured log records."""
    extra = {"structured_data": {"event": event_name, **kwargs}}
    logger.log(level, f"Event: {event_name}", extra=extra)


# ==============================================================================
# Thread-Safe Application State Management
# ==============================================================================

class ApplicationState:
    """
    Manages in-memory counters, uptime, and experiment fault injection states
    in a thread-safe manner using standard library synchronization primitives.
    """

    ALLOWED_FAULT_STATES = {
        "normal",
        "temporary_overload",
        "simulated_stalled_progress",
        "hard_failure",
    }

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.process_start_timestamp: float = time.time()
        self.process_start_iso: str = datetime.now(timezone.utc).isoformat()

        # Metrics counters
        self.completed_work_requests: int = 0
        self.failed_work_requests: int = 0
        self.completion_counter: int = 0  # Monotonically increasing counter of successful work
        self.in_flight_requests: int = 0
        self.last_work_latency_ms: float = 0.0

        # Fault state control
        self.fault_state: str = "normal"
        self.fault_metadata: Dict[str, Any] = {}

        # Stall synchronization event: set (unblocked) in normal operation;
        # cleared (blocked) when simulated_stalled_progress is active.
        self.stall_event = threading.Event()
        self.stall_event.set()

    def get_effective_fault_state(self) -> Tuple[str, Dict[str, Any]]:
        """
        Retrieves the current fault state. If a temporary overload has expired,
        it automatically transitions back to 'normal' in a thread-safe manner.
        """
        with self._lock:
            if self.fault_state == "temporary_overload":
                expires_at = self.fault_metadata.get("expires_at", 0.0)
                if time.time() >= expires_at:
                    previous_state = self.fault_state
                    self.fault_state = "normal"
                    self.fault_metadata = {}
                    log_event(
                        "fault_state_transition",
                        previous_state=previous_state,
                        new_state="normal",
                        reason="temporary_overload_expired",
                    )
            return self.fault_state, dict(self.fault_metadata)

    def set_fault_state(
        self, new_state: str, duration_seconds: Optional[float] = None, metadata: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Updates the fault state. Reversible and deterministic.
        """
        if new_state not in self.ALLOWED_FAULT_STATES:
            return False, f"Invalid fault state '{new_state}'. Allowed: {sorted(self.ALLOWED_FAULT_STATES)}", {}

        meta = dict(metadata or {})
        with self._lock:
            previous_state = self.fault_state

            if new_state == "temporary_overload":
                dur = duration_seconds if duration_seconds is not None else 5.0
                expires_at = time.time() + dur
                meta["duration_seconds"] = dur
                meta["expires_at"] = expires_at

            elif new_state == "simulated_stalled_progress":
                self.stall_event.clear()  # Block simulated work threads

            # If leaving simulated_stalled_progress, unblock any waiting worker threads
            if previous_state == "simulated_stalled_progress" and new_state != "simulated_stalled_progress":
                self.stall_event.set()

            self.fault_state = new_state
            self.fault_metadata = meta

            log_event(
                "fault_state_transition",
                previous_state=previous_state,
                new_state=new_state,
                metadata=meta,
            )
            return True, f"State transitioned to {new_state}", meta

    def record_work_start(self) -> None:
        with self._lock:
            self.in_flight_requests += 1

    def record_work_success(self, latency_ms: float = 0.0) -> int:
        with self._lock:
            self.in_flight_requests = max(0, self.in_flight_requests - 1)
            self.completed_work_requests += 1
            self.completion_counter += 1
            self.last_work_latency_ms = round(latency_ms, 2)
            return self.completion_counter

    def record_work_failure(self) -> None:
        with self._lock:
            self.in_flight_requests = max(0, self.in_flight_requests - 1)
            self.failed_work_requests += 1

    def get_metrics_snapshot(self) -> Dict[str, Any]:
        """Returns an atomic snapshot of current metrics."""
        current_state, metadata = self.get_effective_fault_state()
        with self._lock:
            uptime = max(0.0, time.time() - self.process_start_timestamp)
            return {
                "process_start_time": self.process_start_iso,
                "uptime_seconds": round(uptime, 2),
                "completed_work_requests": self.completed_work_requests,
                "failed_work_requests": self.failed_work_requests,
                "completion_counter": self.completion_counter,
                "last_work_latency_ms": self.last_work_latency_ms,
                "current_fault_state": current_state,
                "fault_metadata": metadata,
                "in_flight_requests": self.in_flight_requests,
                "pid": os.getpid(),
            }

    def reset_for_testing(self) -> None:
        """Helper for test suites to reset metrics and state."""
        with self._lock:
            self.completed_work_requests = 0
            self.failed_work_requests = 0
            self.completion_counter = 0
            self.in_flight_requests = 0
            self.last_work_latency_ms = 0.0
            self.fault_state = "normal"
            self.fault_metadata = {}
            self.stall_event.set()


# Global application state instance
state = ApplicationState()


# ==============================================================================
# Flask Application Factory
# ==============================================================================

def create_app(test_config: Optional[Dict[str, Any]] = None) -> Flask:
    """Creates and configures the Flask application."""
    app = Flask(__name__)

    # Default configuration
    app.config["EXPERIMENT_MODE"] = os.environ.get("EXPERIMENT_MODE", "false").strip().lower() in ("true", "1", "yes")
    app.config["FAULT_INJECTION_TOKEN"] = os.environ.get("FAULT_INJECTION_TOKEN", "")

    # Upper bound for work delay in milliseconds (30 seconds)
    app.config["MAX_WORK_DELAY_MS"] = 30000
    app.config["DEFAULT_WORK_DELAY_MS"] = 50

    if test_config:
        app.config.update(test_config)

    # --------------------------------------------------------------------------
    # 1. GET / — Project description
    # --------------------------------------------------------------------------
    @app.route("/", methods=["GET"])
    def root() -> Any:
        log_event("request_root", path="/")
        accept_header = request.headers.get("Accept", "")
        user_agent = request.headers.get("User-Agent", "")
        # Check if client explicitly requested JSON (via query parameter or Accept header)
        wants_json = (
            request.args.get("json") is not None
            or request.args.get("format") == "json"
            or ("application/json" in accept_header and "text/html" not in accept_header)
        )

        # If not explicitly requesting JSON, redirect browser navigations (HTML clients) to /dashboard
        if not wants_json:
            is_browser = (
                "text/html" in accept_header
                or request.headers.get("Sec-Fetch-Mode") == "navigate"
                or request.headers.get("Sec-Fetch-Dest") == "document"
                or "Mozilla" in user_agent
            )
            if is_browser:
                return redirect("/dashboard")

        return jsonify({
            "project": "Progress-Gated Application-Server Restart Under Transient Load",
            "course": "Operating Systems Case Study (BTech 3rd Year)",
            "version": "1.0.0",
            "architecture": "AWS Elastic Beanstalk Single-Instance Python",
            "endpoints": {
                "GET /": "Project metadata and endpoint directory",
                "GET /dashboard": "Interactive research presentation dashboard and real-time monitor",
                "GET /health": "Application health check (HTTP 200 normal, HTTP 500 in hard_failure)",
                "GET /metrics": "Process uptime, completion counter, failed/completed counters, and fault state",
                "GET /work?delay_ms=N": "Simulate work execution with configurable delay (0 to 30000 ms)",
                "POST /experiment/fault": "Inject controlled fault state (requires EXPERIMENT_MODE=true and valid token)",
                "GET /api/results/aggregated": "Aggregated statistical benchmark summary across policies and scenarios",
                "GET /api/results/summary": "Detailed per-trial experiment records",
                "GET /api/results/export": "Download benchmark summary CSV",
                "POST /api/experiment/run_trial": "Execute a bounded local simulated experiment trial",
            },
        })

    # --------------------------------------------------------------------------
    # 1b. GET /dashboard — Web presentation dashboard
    # --------------------------------------------------------------------------
    @app.route("/dashboard", methods=["GET"])
    def dashboard() -> Any:
        log_event("request_dashboard", path="/dashboard")
        return render_template("dashboard.html")

    # --------------------------------------------------------------------------
    # 1c. GET /favicon.ico — Browser favicon
    # --------------------------------------------------------------------------
    @app.route("/favicon.ico", methods=["GET"])
    def favicon() -> Any:
        return send_from_directory(
            os.path.join(app.root_path, "static"),
            "favicon.ico",
            mimetype="image/vnd.microsoft.icon",
        )

    # --------------------------------------------------------------------------
    # 2. GET /health — Health status
    # --------------------------------------------------------------------------
    @app.route("/health", methods=["GET"])
    def health() -> Response:
        current_state, _ = state.get_effective_fault_state()
        uptime = round(max(0.0, time.time() - state.process_start_timestamp), 2)

        if current_state == "hard_failure":
            log_event("health_check_failed", fault_state=current_state, uptime_seconds=uptime)
            response = jsonify({
                "status": "unhealthy",
                "fault_state": "hard_failure",
                "simulated_crash": True,
                "note": "Simulated unrecoverable failure state for restart controller experiments.",
                "uptime_seconds": uptime,
                "pid": os.getpid(),
            })
            response.status_code = 500
            return response

        log_event("health_check_passed", fault_state=current_state, uptime_seconds=uptime)
        return jsonify({
            "status": "healthy",
            "fault_state": current_state,
            "uptime_seconds": uptime,
            "pid": os.getpid(),
        })

    # --------------------------------------------------------------------------
    # 3. GET /metrics — Execution metrics
    # --------------------------------------------------------------------------
    @app.route("/metrics", methods=["GET"])
    def metrics() -> Response:
        snapshot = state.get_metrics_snapshot()
        log_event("metrics_queried", **snapshot)
        return jsonify(snapshot)

    # --------------------------------------------------------------------------
    # 4. GET /work?delay_ms=N — Work simulation
    # --------------------------------------------------------------------------
    @app.route("/work", methods=["GET"])
    def work() -> Response:
        raw_delay = request.args.get("delay_ms", default=str(app.config["DEFAULT_WORK_DELAY_MS"]))

        # Validate delay_ms input
        try:
            delay_ms = float(raw_delay)
            if delay_ms < 0 or delay_ms > app.config["MAX_WORK_DELAY_MS"]:
                raise ValueError("Out of bounds")
        except (ValueError, TypeError):
            state.record_work_failure()
            log_event(
                "work_request_rejected",
                level=logging.WARNING,
                reason="invalid_delay_parameter",
                raw_delay=raw_delay,
            )
            response = jsonify({
                "error": "Invalid delay_ms parameter",
                "message": f"delay_ms must be a non-negative number <= {app.config['MAX_WORK_DELAY_MS']} ms",
                "provided": raw_delay,
            })
            response.status_code = 400
            return response

        req_start_perf = time.perf_counter()
        req_start_iso = datetime.now(timezone.utc).isoformat()
        state.record_work_start()
        log_event("work_request_start", requested_delay_ms=delay_ms, started_at=req_start_iso)

        current_fault, metadata = state.get_effective_fault_state()

        # Check for simulated hard failure
        if current_fault == "hard_failure":
            state.record_work_failure()
            log_event("work_request_failed", reason="simulated_hard_failure")
            response = jsonify({
                "error": "Internal Server Error",
                "reason": "Simulated hard failure active",
                "fault_state": "hard_failure",
            })
            response.status_code = 500
            return response

        # Check for temporary overload
        if current_fault == "temporary_overload":
            state.record_work_failure()
            log_event(
                "work_request_failed",
                reason="temporary_overload",
                expires_at=metadata.get("expires_at"),
            )
            response = jsonify({
                "error": "Service Temporarily Overloaded",
                "reason": "Simulated transient load rejection active",
                "fault_state": "temporary_overload",
                "retry_after_seconds": max(0.0, round(metadata.get("expires_at", 0) - time.time(), 2)),
            })
            response.status_code = 503
            return response

        # Check for simulated stalled progress
        if current_fault == "simulated_stalled_progress":
            # Worker thread waits on stall_event or max stall timeout (default 10s or custom)
            stall_wait_timeout = float(metadata.get("stall_timeout_seconds", 10.0))
            log_event("work_request_stalled", stall_wait_timeout=stall_wait_timeout)
            
            unblocked = state.stall_event.wait(timeout=stall_wait_timeout)
            if not unblocked:
                # Stall was not released before timeout
                state.record_work_failure()
                log_event("work_request_stalled_timeout", stall_wait_timeout=stall_wait_timeout)
                response = jsonify({
                    "error": "Gateway Timeout",
                    "reason": "Simulated stalled progress: work thread timed out waiting for progress release",
                    "fault_state": "simulated_stalled_progress",
                })
                response.status_code = 504
                return response
            # If unblocked, re-evaluate and fall through to perform work

        # Perform simulated work computation / I/O wait
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)

        latency_ms = round((time.perf_counter() - req_start_perf) * 1000.0, 2)
        completion_counter = state.record_work_success(latency_ms)
        req_completed_iso = datetime.now(timezone.utc).isoformat()

        log_event(
            "work_request_completed",
            delay_ms=delay_ms,
            latency_ms=latency_ms,
            completion_counter=completion_counter,
            completed_at=req_completed_iso,
        )

        return jsonify({
            "status": "completed",
            "delay_ms": delay_ms,
            "latency_ms": latency_ms,
            "completion_counter": completion_counter,
            "started_at": req_start_iso,
            "completed_at": req_completed_iso,
        })

    # --------------------------------------------------------------------------
    # 5. POST /experiment/fault — Controlled fault injection
    # --------------------------------------------------------------------------
    @app.route("/experiment/fault", methods=["POST"])
    def inject_fault() -> Response:
        # 1. Verify EXPERIMENT_MODE is enabled
        if not app.config.get("EXPERIMENT_MODE", False):
            log_event("fault_injection_rejected", reason="experiment_mode_disabled")
            response = jsonify({
                "error": "Forbidden",
                "message": "Fault injection is disabled by default. Set EXPERIMENT_MODE=true in environment to enable.",
            })
            response.status_code = 403
            return response

        # 2. Verify authorization token via header
        configured_token = app.config.get("FAULT_INJECTION_TOKEN", "")
        if not configured_token:
            log_event("fault_injection_rejected", reason="fault_token_not_configured_on_server")
            response = jsonify({
                "error": "Forbidden",
                "message": "FAULT_INJECTION_TOKEN is not configured on the server.",
            })
            response.status_code = 403
            return response

        auth_header = request.headers.get("X-Fault-Token", "")
        if not auth_header:
            # Also check Authorization: Bearer <token>
            auth_val = request.headers.get("Authorization", "")
            if auth_val.startswith("Bearer "):
                auth_header = auth_val[7:].strip()

        # Constant-time comparison to prevent timing attacks
        if not auth_header or not hmac.compare_digest(auth_header.encode("utf-8"), configured_token.encode("utf-8")):
            log_event("fault_injection_rejected", reason="invalid_or_missing_token")
            response = jsonify({
                "error": "Unauthorized",
                "message": "Invalid or missing fault injection token. Provide valid 'X-Fault-Token' header.",
            })
            response.status_code = 401
            return response

        # 3. Validate request payload
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            response = jsonify({
                "error": "Bad Request",
                "message": "JSON body is required with at least a 'state' field.",
            })
            response.status_code = 400
            return response

        target_state = payload.get("state")
        if not target_state or not isinstance(target_state, str):
            response = jsonify({
                "error": "Bad Request",
                "message": f"Field 'state' is required. Allowed states: {sorted(ApplicationState.ALLOWED_FAULT_STATES)}",
            })
            response.status_code = 400
            return response

        target_state = target_state.strip().lower()
        if target_state not in ApplicationState.ALLOWED_FAULT_STATES:
            response = jsonify({
                "error": "Bad Request",
                "message": f"Unknown fault state '{target_state}'. Allowed states: {sorted(ApplicationState.ALLOWED_FAULT_STATES)}",
            })
            response.status_code = 400
            return response

        duration_seconds = None
        if target_state == "temporary_overload":
            raw_dur = payload.get("duration_seconds", 5.0)
            try:
                duration_seconds = float(raw_dur)
                if duration_seconds <= 0 or duration_seconds > 600:
                    raise ValueError()
            except (ValueError, TypeError):
                response = jsonify({
                    "error": "Bad Request",
                    "message": "duration_seconds must be a positive number <= 600",
                })
                response.status_code = 400
                return response

        custom_metadata: Dict[str, Any] = {}
        if target_state == "simulated_stalled_progress":
            stall_timeout = payload.get("stall_timeout_seconds", 10.0)
            try:
                stall_timeout_val = float(stall_timeout)
                if stall_timeout_val <= 0 or stall_timeout_val > 60:
                    raise ValueError()
                custom_metadata["stall_timeout_seconds"] = stall_timeout_val
            except (ValueError, TypeError):
                response = jsonify({
                    "error": "Bad Request",
                    "message": "stall_timeout_seconds must be a positive number <= 60",
                })
                response.status_code = 400
                return response

        previous_state, _ = state.get_effective_fault_state()
        success, message, meta = state.set_fault_state(
            target_state, duration_seconds=duration_seconds, metadata=custom_metadata
        )

        return jsonify({
            "status": "success",
            "message": message,
            "previous_fault_state": previous_state,
            "current_fault_state": target_state,
            "metadata": meta,
        })

    # --------------------------------------------------------------------------
    # 6. Dashboard & Results APIs
    # --------------------------------------------------------------------------
    CANONICAL_BENCHMARK_BATCH = "20261027_215526"

    def _resolve_benchmark_csv(prefix: str) -> Optional[str]:
        """
        Resolves the canonical historical benchmark CSV file.
        Priority:
        1. Bundled experiment/data/ directory (preserves canonical batch 20261027_215526 in deployments).
        2. Runtime results/ directory for canonical batch 20261027_215526.
        3. Fallback: latest matching CSV in results/ if canonical batch is absent.
        """
        app_root = os.path.dirname(os.path.abspath(__file__))
        bundled_dir = os.path.join(app_root, "experiment", "data")
        results_dir = os.path.join(app_root, "results")

        canonical_filename = f"{prefix}{CANONICAL_BENCHMARK_BATCH}.csv"

        # 1. Primary: Bundled benchmark data in experiment/data/
        bundled_path = os.path.join(bundled_dir, canonical_filename)
        if os.path.isfile(bundled_path):
            return bundled_path

        # 2. Workspace: Canonical batch in results/
        canonical_results_path = os.path.join(results_dir, canonical_filename)
        if os.path.isfile(canonical_results_path):
            return canonical_results_path

        # 3. Fallback: Latest matching file in results/
        if os.path.isdir(results_dir):
            matches = [
                os.path.join(results_dir, f)
                for f in os.listdir(results_dir)
                if f.startswith(prefix) and f.endswith(".csv")
            ]
            if matches:
                matches.sort(key=os.path.getmtime, reverse=True)
                return matches[0]

        return None

    _find_latest_csv = _resolve_benchmark_csv

    @app.route("/api/results/aggregated", methods=["GET"])
    def get_aggregated_results() -> Response:
        csv_path = _resolve_benchmark_csv("batch_aggregated_")
        if not csv_path or not os.path.isfile(csv_path):
            return jsonify({"status": "empty", "aggregates": [], "message": "No aggregated results found."})

        rows: List[Dict[str, Any]] = []
        try:
            with open(csv_path, mode="r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    rows.append(row)
            return jsonify({
                "status": "success",
                "file": os.path.basename(csv_path),
                "aggregates": rows,
            })
        except Exception as exc:
            return jsonify({"status": "error", "message": str(exc)}), 500

    @app.route("/api/results/summary", methods=["GET"])
    def get_summary_results() -> Response:
        csv_path = _resolve_benchmark_csv("batch_summary_")
        if not csv_path or not os.path.isfile(csv_path):
            return jsonify({"status": "empty", "trials": [], "message": "No summary results found."})

        rows: List[Dict[str, Any]] = []
        try:
            with open(csv_path, mode="r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get("row_type") == "TRIAL":
                        rows.append(row)
            return jsonify({
                "status": "success",
                "file": os.path.basename(csv_path),
                "trials": rows,
            })
        except Exception as exc:
            return jsonify({"status": "error", "message": str(exc)}), 500

    @app.route("/api/results/export", methods=["GET"])
    def export_results_csv() -> Any:
        export_type = request.args.get("type", "aggregated").lower().strip()
        if export_type not in ("aggregated", "summary"):
            return jsonify({
                "error": "Bad Request",
                "message": f"Invalid export type '{export_type}'. Allowed types: ['aggregated', 'summary']",
            }), 400

        prefix = "batch_summary_" if export_type == "summary" else "batch_aggregated_"
        csv_path = _resolve_benchmark_csv(prefix)
        if not csv_path or not os.path.isfile(csv_path):
            return jsonify({"error": f"No {export_type} results file available for download."}), 404

        return send_file(
            csv_path,
            as_attachment=True,
            download_name=os.path.basename(csv_path),
            mimetype="text/csv",
        )

    # Concurrency lock for experiment runner
    _experiment_execution_lock = threading.Lock()

    @app.route("/api/experiment/run_trial", methods=["POST"])
    def run_experiment_trial() -> Response:
        if not _experiment_execution_lock.acquire(blocking=False):
            return jsonify({
                "error": "Conflict",
                "message": "Another experiment trial is currently executing. Please wait for completion.",
            }), 409

        try:
            payload = request.get_json(silent=True) or {}
            policy_name = str(payload.get("policy", "control")).strip().lower()
            scenario = str(payload.get("scenario", "transient_latency")).strip().lower()

            if policy_name not in ("control", "no_restart", "fixed_threshold", "progress_gated"):
                return jsonify({"error": "Bad Request", "message": f"Invalid policy '{policy_name}'"}), 400

            allowed_scenarios = ("transient_latency", "stalled_progress", "flapping_overload", "transient_overload")
            if scenario not in allowed_scenarios:
                return jsonify({
                    "error": "Bad Request",
                    "message": f"Invalid scenario '{scenario}'. Allowed scenarios: {list(allowed_scenarios)}",
                }), 400

            duration = max(2.0, min(10.0, float(payload.get("duration_seconds", 3.5))))
            threshold = max(50.0, min(1000.0, float(payload.get("latency_threshold_ms", 150.0))))
            obs_interval = max(0.1, min(1.5, float(payload.get("observation_interval_seconds", 0.3))))
            degraded_delay = max(100.0, min(2000.0, float(payload.get("degraded_delay_ms", 250.0))))

            # Parse and validate seeded randomness and jitter
            raw_seed = payload.get("random_seed")
            random_seed: Optional[int] = None
            if raw_seed is not None and str(raw_seed).strip() != "":
                try:
                    random_seed = int(raw_seed)
                    if random_seed < 0 or random_seed > 2147483647:
                        return jsonify({"error": "Bad Request", "message": "random_seed must be between 0 and 2147483647"}), 400
                except (ValueError, TypeError):
                    return jsonify({"error": "Bad Request", "message": "random_seed must be an integer"}), 400

            try:
                arrival_jitter = float(payload.get("arrival_jitter_ratio", 0.0))
                if arrival_jitter < 0.0 or arrival_jitter > 0.5:
                    return jsonify({"error": "Bad Request", "message": "arrival_jitter_ratio must be between 0.0 and 0.5"}), 400
            except (ValueError, TypeError):
                return jsonify({"error": "Bad Request", "message": "arrival_jitter_ratio must be a numeric value"}), 400

            try:
                latency_jitter = float(payload.get("latency_jitter_ratio", 0.0))
                if latency_jitter < 0.0 or latency_jitter > 0.5:
                    return jsonify({"error": "Bad Request", "message": "latency_jitter_ratio must be between 0.0 and 0.5"}), 400
            except (ValueError, TypeError):
                return jsonify({"error": "Bad Request", "message": "latency_jitter_ratio must be a numeric value"}), 400

            # In testing mode with mock flag, return deterministic response without spawning subprocess
            if app.config.get("TESTING") and payload.get("mock_trial"):
                sim_restarts = 0
                if scenario == "stalled_progress" and policy_name in ("fixed_threshold", "progress_gated"):
                    sim_restarts = 1
                elif scenario == "flapping_overload" and policy_name == "fixed_threshold":
                    sim_restarts = 1
                elif scenario == "transient_overload" and policy_name == "fixed_threshold":
                    sim_restarts = 1

                failed_reqs = 0
                succ_reqs = 35
                if scenario in ("flapping_overload", "transient_overload"):
                    failed_reqs = 8 if policy_name == "control" else (6 if policy_name == "fixed_threshold" else 7)
                    succ_reqs = 35 - failed_reqs

                succ_rate = round((succ_reqs / 35.0) * 100.0, 1)

                return jsonify({
                    "status": "success",
                    "trial": {
                        "trial_id": "trial_mock_unit_test",
                        "policy": policy_name,
                        "scenario": scenario,
                        "total_requests": 35,
                        "successful_requests": succ_reqs,
                        "failed_requests": failed_reqs,
                        "success_rate_percent": succ_rate,
                        "simulated_restarts": sim_restarts,
                        "real_aws_restarts": 0,
                        "recovery_time_seconds": "1.45" if (scenario != "stalled_progress" or policy_name != "control") else None,
                        "estimated_downtime_seconds": 0.0 if failed_reqs == 0 else 0.85,
                        "simulated_restart_overhead_seconds": 0.05 if sim_restarts > 0 else 0.0,
                        "mean_latency_ms": 25.4,
                        "random_seed": random_seed,
                        "arrival_jitter_ratio": arrival_jitter,
                        "latency_jitter_ratio": latency_jitter,
                        "recovered": (scenario != "stalled_progress" or policy_name != "control"),
                        "log_file": "results/test.jsonl",
                    },
                    "message": "Mock trial completed successfully.",
                })

            # Save original config to preserve external authorization constraints outside trial
            orig_experiment_mode = app.config.get("EXPERIMENT_MODE", False)
            orig_fault_token = app.config.get("FAULT_INJECTION_TOKEN", "")
            trial_fault_token = orig_fault_token or "web_demo_token"

            # Authorize local simulation fault injection during this locked trial
            app.config["EXPERIMENT_MODE"] = True
            app.config["FAULT_INJECTION_TOKEN"] = trial_fault_token

            # Reset local application state before running matched trial
            state.reset_for_testing()
            time.sleep(0.05)

            # Lazy import experiment modules to keep application module lightweight
            from experiment.adapters import SimulatedRestartAdapter
            from experiment.models import PolicyConfig
            from experiment.runner import ExperimentRunner
            from experiment.workload import FaultSchedule, LatencySpikeSchedule

            def on_restart_action() -> None:
                state.set_fault_state("normal")

            adapter = SimulatedRestartAdapter(
                on_restart_callback=on_restart_action,
                simulated_delay_seconds=0.05,
            )

            p_config = PolicyConfig(
                latency_threshold_ms=threshold,
                observation_interval_seconds=obs_interval,
                hard_deadline_seconds=1.5,
                cooldown_seconds=1.2,
                recovery_consecutive_successes=2,
                healthy_latency_threshold_ms=80.0,
            )

            trial_id = f"trial_web_{policy_name}_{scenario}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

            port = int(os.environ.get("PORT", 5000))
            base_url = f"http://127.0.0.1:{port}"

            runner = ExperimentRunner(
                base_url=base_url,
                policy_name=policy_name,
                config=p_config,
                adapter=adapter,
                fault_token=trial_fault_token,
                log_dir="results",
                run_id=trial_id,
                probe_timeout_seconds=1.0,
                request_interval_seconds=0.1,
                default_work_delay_ms=20.0,
                random_seed=random_seed,
                arrival_jitter_ratio=arrival_jitter,
                latency_jitter_ratio=latency_jitter,
                scenario_name=scenario,
                scenario_params=payload,
            )

            fault_schedules: List[FaultSchedule] = []
            latency_schedules: List[LatencySpikeSchedule] = []

            if scenario == "transient_latency":
                latency_schedules.append(
                    LatencySpikeSchedule(offset_seconds=1.0, duration_seconds=1.0, degraded_delay_ms=degraded_delay)
                )
            elif scenario == "stalled_progress":
                stall_timeout = max(1.0, min(10.0, float(payload.get("stall_timeout_seconds", 2.5))))
                fault_schedules.append(
                    FaultSchedule(offset_seconds=1.0, fault_state="simulated_stalled_progress", stall_timeout_seconds=stall_timeout)
                )
            elif scenario == "flapping_overload":
                bursts = max(1, min(5, int(payload.get("burst_count", 2))))
                burst_dur = max(0.1, min(2.0, float(payload.get("burst_duration_seconds", 0.4))))
                gap = max(0.1, min(2.0, float(payload.get("recovery_gap_seconds", 0.3))))
                base_off = max(0.2, min(5.0, float(payload.get("offset_seconds", 1.0))))
                for b in range(bursts):
                    b_off = base_off + b * (burst_dur + gap)
                    fault_schedules.append(
                        FaultSchedule(offset_seconds=b_off, fault_state="temporary_overload", duration_seconds=burst_dur)
                    )
            elif scenario == "transient_overload":
                off = max(0.2, min(5.0, float(payload.get("offset_seconds", 1.0))))
                dur = max(0.1, min(5.0, float(payload.get("fault_duration_seconds", payload.get("duration_seconds_fault", 1.0)))))
                fault_schedules.append(
                    FaultSchedule(offset_seconds=off, fault_state="temporary_overload", duration_seconds=dur)
                )

            summary = runner.run(
                duration_seconds=duration,
                fault_schedules=fault_schedules,
                latency_schedules=latency_schedules,
            )

            rec_time = summary.get("measured_recovery_time_seconds")
            rec_str = f"{rec_time:.2f}" if rec_time is not None else None

            restarts_meta = summary.get("restarts", {})
            latency_meta = summary.get("latency_metrics", {})
            sim_restarts = restarts_meta.get("simulated_restarts_count", 0)
            aws_restarts = restarts_meta.get("aws_restarts_count", 0)
            mean_lat = latency_meta.get("mean_ms", 0.0)

            total_reqs = summary.get("total_requests", 0)
            failed_reqs = summary.get("failed_requests", 0)

            # Calculate total simulated restart duration overhead
            restart_details = restarts_meta.get("restart_details", [])
            sim_restart_overhead = round(
                sum(float(r.get("duration_seconds", 0.0)) for r in restart_details if r.get("is_simulated")),
                3,
            )

            if rec_time is not None:
                recovery_status = "RECOVERED"
                recovery_detail = "Recovery confirmed by policy via consecutive healthy probes post-degradation."
            elif failed_reqs == 0 and total_reqs > 0:
                recovery_status = "UNCONFIRMED_CUTOFF"
                recovery_detail = "Requests succeeded (100% OK), but trial duration terminated before post-fault recovery criteria could be validated."
            else:
                recovery_status = "UNRECOVERED"
                recovery_detail = "Genuine unrecovered state: requests failed or timed out during unmitigated outage."

            trial_record = {
                "trial_id": trial_id,
                "policy": policy_name,
                "scenario": scenario,
                "total_requests": total_reqs,
                "successful_requests": summary.get("successful_requests", 0),
                "failed_requests": failed_reqs,
                "success_rate_percent": summary.get("success_rate_percent", 0.0),
                "simulated_restarts": sim_restarts,
                "real_aws_restarts": aws_restarts,
                "restarts": restarts_meta,
                "recovery_time_seconds": rec_str,
                "recovery_status": recovery_status,
                "recovery_detail": recovery_detail,
                "estimated_downtime_seconds": summary.get("estimated_downtime_seconds", 0.0),
                "simulated_restart_overhead_seconds": sim_restart_overhead,
                "mean_latency_ms": mean_lat,
                "random_seed": random_seed,
                "arrival_jitter_ratio": arrival_jitter,
                "latency_jitter_ratio": latency_jitter,
                "recovered": (rec_time is not None),
                "log_file": runner.exp_logger.jsonl_path,
            }

            return jsonify({
                "status": "success",
                "trial": trial_record,
                "message": f"Trial {trial_id} completed successfully.",
            })

        except Exception as exc:
            logger.error("Trial run error: %s", exc, exc_info=True)
            return jsonify({"status": "error", "error": str(exc)}), 500
        finally:
            if "orig_experiment_mode" in locals():
                app.config["EXPERIMENT_MODE"] = orig_experiment_mode
            if "orig_fault_token" in locals():
                app.config["FAULT_INJECTION_TOKEN"] = orig_fault_token
            state.reset_for_testing()
            _experiment_execution_lock.release()

    return app


# ==============================================================================
# WSGI Entry Point (for AWS Elastic Beanstalk & Local Server)
# ==============================================================================

# AWS Elastic Beanstalk looks for 'application' in 'application.py'
application = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"Starting development server on http://{host}:{port} (threaded=True)...")
    application.run(host=host, port=port, threaded=True)
