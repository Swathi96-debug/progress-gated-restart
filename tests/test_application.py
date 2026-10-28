"""
Unit and integration tests for the Progress-Gated Application Server.
Uses pytest and Flask's test client with deterministic and thread-safe validations.
"""

from __future__ import annotations

import concurrent.futures
import time
from typing import Generator

import pytest
from flask import Flask
from flask.testing import FlaskClient

from application import create_app, state


@pytest.fixture(autouse=True)
def reset_application_state() -> Generator[None, None, None]:
    """Ensures a clean application state before and after each test."""
    state.reset_for_testing()
    yield
    state.reset_for_testing()


@pytest.fixture
def default_app() -> Flask:
    """Creates app with default configuration (experiment mode disabled)."""
    return create_app({
        "TESTING": True,
        "EXPERIMENT_MODE": False,
        "FAULT_INJECTION_TOKEN": "test-secret-token",
    })


@pytest.fixture
def experiment_app() -> Flask:
    """Creates app with experiment mode enabled."""
    return create_app({
        "TESTING": True,
        "EXPERIMENT_MODE": True,
        "FAULT_INJECTION_TOKEN": "test-secret-token",
    })


@pytest.fixture
def client(default_app: Flask) -> FlaskClient:
    return default_app.test_client()


@pytest.fixture
def exp_client(experiment_app: Flask) -> FlaskClient:
    return experiment_app.test_client()


# ==============================================================================
# 1. Basic Endpoint Tests
# ==============================================================================

def test_root_endpoint(client: FlaskClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    data = response.get_json()
    assert "project" in data
    assert "endpoints" in data
    assert data["project"] == "Progress-Gated Application-Server Restart Under Transient Load"


def test_health_endpoint_healthy(client: FlaskClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "healthy"
    assert data["fault_state"] == "normal"
    assert "pid" in data
    assert "uptime_seconds" in data
    assert isinstance(data["uptime_seconds"], (int, float))


def test_metrics_initial_state(client: FlaskClient) -> None:
    response = client.get("/metrics")
    assert response.status_code == 200
    data = response.get_json()
    assert data["completed_work_requests"] == 0
    assert data["failed_work_requests"] == 0
    assert data["completion_counter"] == 0
    assert data["current_fault_state"] == "normal"
    assert "pid" in data
    assert "process_start_time" in data


# ==============================================================================
# 2. Work Request & Completion Counter Tests
# ==============================================================================

def test_work_successful_with_latency_measurement(client: FlaskClient) -> None:
    delay_ms = 30
    start = time.perf_counter()
    response = client.get(f"/work?delay_ms={delay_ms}")
    elapsed_ms = (time.perf_counter() - start) * 1000.0

    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "completed"
    assert data["delay_ms"] == delay_ms
    assert data["latency_ms"] >= 20.0  # Measured latency includes sleep and overhead
    assert data["completion_counter"] == 1
    assert "completed_at" in data

    # Verify metrics updated accordingly
    metrics = client.get("/metrics").get_json()
    assert metrics["completed_work_requests"] == 1
    assert metrics["completion_counter"] == 1
    assert metrics["failed_work_requests"] == 0


def test_work_default_delay(client: FlaskClient) -> None:
    response = client.get("/work")
    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "completed"
    assert data["delay_ms"] == 50
    assert data["completion_counter"] == 1


def test_completion_counter_strictly_monotonic(client: FlaskClient) -> None:
    for expected_count in range(1, 6):
        resp = client.get("/work?delay_ms=5")
        assert resp.status_code == 200
        assert resp.get_json()["completion_counter"] == expected_count

    metrics = client.get("/metrics").get_json()
    assert metrics["completion_counter"] == 5
    assert metrics["completed_work_requests"] == 5


# ==============================================================================
# 3. Invalid Delay Validation Tests
# ==============================================================================

def test_work_negative_delay_rejected(client: FlaskClient) -> None:
    response = client.get("/work?delay_ms=-10")
    assert response.status_code == 400
    data = response.get_json()
    assert "error" in data
    assert "Invalid delay_ms" in data["error"]

    # Failed work recorded, completion counter NOT incremented
    metrics = client.get("/metrics").get_json()
    assert metrics["failed_work_requests"] == 1
    assert metrics["completion_counter"] == 0


def test_work_excessive_delay_rejected(client: FlaskClient) -> None:
    response = client.get("/work?delay_ms=30001")
    assert response.status_code == 400
    data = response.get_json()
    assert "error" in data

    metrics = client.get("/metrics").get_json()
    assert metrics["failed_work_requests"] == 1
    assert metrics["completion_counter"] == 0


def test_work_non_numeric_delay_rejected(client: FlaskClient) -> None:
    response = client.get("/work?delay_ms=notanumber")
    assert response.status_code == 400
    metrics = client.get("/metrics").get_json()
    assert metrics["failed_work_requests"] == 1
    assert metrics["completion_counter"] == 0


# ==============================================================================
# 4. Fault Injection Authorization & Safety Tests
# ==============================================================================

def test_fault_injection_disabled_by_default(client: FlaskClient) -> None:
    # client has default_app where EXPERIMENT_MODE=False
    response = client.post(
        "/experiment/fault",
        json={"state": "temporary_overload"},
        headers={"X-Fault-Token": "test-secret-token"},
    )
    assert response.status_code == 403
    data = response.get_json()
    assert "disabled by default" in data["message"]


def test_fault_injection_unauthorized_missing_token(exp_client: FlaskClient) -> None:
    # exp_client has EXPERIMENT_MODE=True but no token provided
    response = exp_client.post(
        "/experiment/fault",
        json={"state": "temporary_overload"},
    )
    assert response.status_code == 401
    assert "Invalid or missing" in response.get_json()["message"]


def test_fault_injection_unauthorized_incorrect_token(exp_client: FlaskClient) -> None:
    response = exp_client.post(
        "/experiment/fault",
        json={"state": "temporary_overload"},
        headers={"X-Fault-Token": "wrong-secret-token"},
    )
    assert response.status_code == 401


def test_fault_injection_authorized_with_bearer_token(exp_client: FlaskClient) -> None:
    response = exp_client.post(
        "/experiment/fault",
        json={"state": "temporary_overload", "duration_seconds": 2.0},
        headers={"Authorization": "Bearer test-secret-token"},
    )
    assert response.status_code == 200
    assert response.get_json()["current_fault_state"] == "temporary_overload"


def test_fault_injection_invalid_payload(exp_client: FlaskClient) -> None:
    # Invalid JSON body
    response = exp_client.post(
        "/experiment/fault",
        data="not a json",
        content_type="text/plain",
        headers={"X-Fault-Token": "test-secret-token"},
    )
    assert response.status_code == 400

    # Missing state
    response2 = exp_client.post(
        "/experiment/fault",
        json={"other_field": 123},
        headers={"X-Fault-Token": "test-secret-token"},
    )
    assert response2.status_code == 400

    # Unknown state
    response3 = exp_client.post(
        "/experiment/fault",
        json={"state": "catastrophic_meltdown"},
        headers={"X-Fault-Token": "test-secret-token"},
    )
    assert response3.status_code == 400


# ==============================================================================
# 5. Fault Behavior: Temporary Overload & Automatic Recovery
# ==============================================================================

def test_temporary_overload_and_automatic_recovery(exp_client: FlaskClient) -> None:
    # Inject short overload duration (250 milliseconds)
    inject_resp = exp_client.post(
        "/experiment/fault",
        json={"state": "temporary_overload", "duration_seconds": 0.25},
        headers={"X-Fault-Token": "test-secret-token"},
    )
    assert inject_resp.status_code == 200

    # During overload: /work should fail immediately with 503
    work_resp = exp_client.get("/work?delay_ms=10")
    assert work_resp.status_code == 503
    assert work_resp.get_json()["fault_state"] == "temporary_overload"

    metrics_mid = exp_client.get("/metrics").get_json()
    assert metrics_mid["failed_work_requests"] == 1
    assert metrics_mid["completion_counter"] == 0

    # Wait for the duration to elapse (0.35 seconds > 0.25 seconds)
    time.sleep(0.35)

    # After expiration: /work should automatically succeed and revert to normal
    work_resp_recovered = exp_client.get("/work?delay_ms=10")
    assert work_resp_recovered.status_code == 200
    assert work_resp_recovered.get_json()["status"] == "completed"

    metrics_post = exp_client.get("/metrics").get_json()
    assert metrics_post["current_fault_state"] == "normal"
    assert metrics_post["completed_work_requests"] == 1
    assert metrics_post["completion_counter"] == 1
    assert metrics_post["failed_work_requests"] == 1


# ==============================================================================
# 6. Fault Behavior: Simulated Stalled Progress & Release
# ==============================================================================

def test_simulated_stalled_progress_unblocked_by_normal_transition(exp_client: FlaskClient) -> None:
    # Put into stalled progress state with max wait timeout of 2 seconds
    inject_resp = exp_client.post(
        "/experiment/fault",
        json={"state": "simulated_stalled_progress", "stall_timeout_seconds": 2.0},
        headers={"X-Fault-Token": "test-secret-token"},
    )
    assert inject_resp.status_code == 200

    results = []

    def make_work_call() -> None:
        resp = exp_client.get("/work?delay_ms=10")
        results.append(resp)

    # Launch work on a background thread (it should block waiting on stall release)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(make_work_call)

        # Allow worker thread to enter the wait state
        time.sleep(0.1)
        assert len(results) == 0  # Still waiting

        # Revert state back to normal from another thread/endpoint
        revert_resp = exp_client.post(
            "/experiment/fault",
            json={"state": "normal"},
            headers={"X-Fault-Token": "test-secret-token"},
        )
        assert revert_resp.status_code == 200

        # Wait for worker thread to unblock and finish
        future.result(timeout=2.0)

    assert len(results) == 1
    assert results[0].status_code == 200
    assert results[0].get_json()["status"] == "completed"

    metrics = exp_client.get("/metrics").get_json()
    assert metrics["completed_work_requests"] == 1
    assert metrics["completion_counter"] == 1


def test_simulated_stalled_progress_timeout(exp_client: FlaskClient) -> None:
    # Set short stall timeout of 0.15 seconds
    exp_client.post(
        "/experiment/fault",
        json={"state": "simulated_stalled_progress", "stall_timeout_seconds": 0.15},
        headers={"X-Fault-Token": "test-secret-token"},
    )

    # Worker request should time out and return 504 Gateway Timeout
    resp = exp_client.get("/work?delay_ms=5")
    assert resp.status_code == 504
    data = resp.get_json()
    assert data["fault_state"] == "simulated_stalled_progress"
    assert "timed out" in data["reason"]

    metrics = exp_client.get("/metrics").get_json()
    assert metrics["failed_work_requests"] == 1
    assert metrics["completion_counter"] == 0


def test_metrics_accessible_during_stalled_work(exp_client: FlaskClient) -> None:
    exp_client.post(
        "/experiment/fault",
        json={"state": "simulated_stalled_progress", "stall_timeout_seconds": 1.0},
        headers={"X-Fault-Token": "test-secret-token"},
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        # Start stalled worker thread
        future = executor.submit(lambda: exp_client.get("/work?delay_ms=5"))
        time.sleep(0.05)

        # /metrics must return immediately without blocking!
        start_metrics = time.perf_counter()
        metrics_resp = exp_client.get("/metrics")
        query_duration = time.perf_counter() - start_metrics

        assert metrics_resp.status_code == 200
        assert query_duration < 0.1  # Fast, non-blocking
        data = metrics_resp.get_json()
        assert data["current_fault_state"] == "simulated_stalled_progress"
        assert data["in_flight_requests"] == 1

        # Restore normal to unblock worker
        exp_client.post(
            "/experiment/fault",
            json={"state": "normal"},
            headers={"X-Fault-Token": "test-secret-token"},
        )
        future.result(timeout=2.0)


# ==============================================================================
# 7. Hard Failure Simulation State
# ==============================================================================

def test_hard_failure_state(exp_client: FlaskClient) -> None:
    # Inject hard_failure state
    exp_client.post(
        "/experiment/fault",
        json={"state": "hard_failure"},
        headers={"X-Fault-Token": "test-secret-token"},
    )

    # /health returns HTTP 500
    health_resp = exp_client.get("/health")
    assert health_resp.status_code == 500
    health_data = health_resp.get_json()
    assert health_data["status"] == "unhealthy"
    assert health_data["simulated_crash"] is True

    # /work returns HTTP 500
    work_resp = exp_client.get("/work?delay_ms=10")
    assert work_resp.status_code == 500

    # Reset back to normal
    exp_client.post(
        "/experiment/fault",
        json={"state": "normal"},
        headers={"X-Fault-Token": "test-secret-token"},
    )
    assert exp_client.get("/health").status_code == 200


# ==============================================================================
# 8. Concurrent Work Requests & Thread-Safe Counter Updates
# ==============================================================================

def test_concurrent_work_requests_and_thread_safe_counters(client: FlaskClient) -> None:
    concurrent_requests = 10

    def make_work_call(idx: int) -> int:
        resp = client.get("/work?delay_ms=15")
        return resp.status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrent_requests) as executor:
        status_codes = list(executor.map(make_work_call, range(concurrent_requests)))

    assert all(code == 200 for code in status_codes)

    metrics = client.get("/metrics").get_json()
    assert metrics["completed_work_requests"] == concurrent_requests
    assert metrics["completion_counter"] == concurrent_requests
    assert metrics["failed_work_requests"] == 0
    assert metrics["in_flight_requests"] == 0
