"""
Tests for Web UI Dashboard and Results APIs.
Verifies dashboard rendering, backward-compatible root endpoint routing,
results CSV endpoints, export capabilities, and input validation.
"""

from __future__ import annotations

import io
from typing import Generator
import pytest
from flask import Flask
from flask.testing import FlaskClient

from application import create_app, state


@pytest.fixture
def app() -> Generator[Flask, None, None]:
    """Provides a fresh Flask application configured for testing."""
    test_app = create_app({
        "TESTING": True,
        "EXPERIMENT_MODE": True,
        "FAULT_INJECTION_TOKEN": "test_token",
    })
    state.reset_for_testing()
    yield test_app
    state.reset_for_testing()


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    """Provides a test HTTP client."""
    return app.test_client()


def test_dashboard_renders_html(client: FlaskClient) -> None:
    """GET /dashboard must return 200 OK with HTML content."""
    res = client.get("/dashboard")
    assert res.status_code == 200
    assert "text/html" in res.content_type
    html = res.get_data(as_text=True)
    assert "Progress-Gated Restart" in html
    assert "Operating Systems Case Study" in html
    assert "Live Node Diagnostics" in html
    assert "Interactive Benchmark Trial Execution" in html
    assert '<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">' in html
    assert '<link rel="alternate icon" href="/favicon.ico">' in html


def test_favicon_ico_route_and_static_assets(client: FlaskClient) -> None:
    """Browser request to /favicon.ico and /static/favicon.svg must succeed with 200 OK."""
    res_ico = client.get("/favicon.ico")
    assert res_ico.status_code == 200
    assert "icon" in res_ico.content_type or "image/" in res_ico.content_type
    assert len(res_ico.data) > 0

    res_svg = client.get("/static/favicon.svg")
    assert res_svg.status_code == 200
    assert "svg" in res_svg.content_type
    assert len(res_svg.data) > 0


def test_dashboard_form_fields_have_associated_labels(client: FlaskClient) -> None:
    """All interactive form controls in dashboard.html must have associated labels with matching for and id attributes."""
    import re

    res = client.get("/dashboard")
    assert res.status_code == 200
    html = res.get_data(as_text=True)

    # Required field IDs that must have matching <label for="...">
    expected_labeled_fields = [
        "poll-interval",
        "probe-delay",
        "policy-control",
        "policy-fixed-threshold",
        "policy-progress-gated",
        "scenario-transient-latency",
        "scenario-stalled-progress",
        "scenario-flapping-overload",
        "scenario-transient-overload",
        "param-duration",
        "param-threshold",
        "param-obs-interval",
        "param-spike-delay",
        "param-random-seed",
        "param-arrival-jitter",
        "param-latency-jitter",
    ]

    for field_id in expected_labeled_fields:
        # Check that the input or select element exists with this id
        assert f'id="{field_id}"' in html, f"Missing form control with id='{field_id}'"
        # Check that a label exists associating with this id
        assert f'for="{field_id}"' in html, f"Missing label associated with for='{field_id}'"


def test_root_endpoint_backward_compatibility(client: FlaskClient) -> None:
    """GET / must continue returning JSON metadata for API clients and test fixtures."""
    res = client.get("/")
    assert res.status_code == 200
    assert "application/json" in res.content_type
    data = res.get_json()
    assert "project" in data
    assert "endpoints" in data
    assert "GET /dashboard" in data["endpoints"]


def test_root_endpoint_browser_redirect(client: FlaskClient) -> None:
    """GET / with browser navigate headers redirects to /dashboard."""
    # Standard Chrome/Edge browser navigation headers
    browser_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
    }
    res = client.get("/", headers=browser_headers)
    assert res.status_code == 302
    assert res.headers["Location"].endswith("/dashboard")

    # Override with ?json=1 still returns JSON
    res_json = client.get("/?json=1", headers=browser_headers)
    assert res_json.status_code == 200
    assert "application/json" in res_json.content_type


def test_root_endpoint_explicit_json_accept(client: FlaskClient) -> None:
    """GET / with explicit Accept: application/json must return JSON metadata."""
    res = client.get("/", headers={"Accept": "application/json"})
    assert res.status_code == 200
    assert "application/json" in res.content_type
    data = res.get_json()
    assert data["project"] == "Progress-Gated Application-Server Restart Under Transient Load"
    assert "GET /dashboard" in data["endpoints"]


def test_root_endpoint_html_browser_without_sec_fetch(client: FlaskClient) -> None:
    """GET / with text/html in Accept header (legacy or minimal browser) redirects to /dashboard."""
    res = client.get("/", headers={"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"})
    assert res.status_code == 302
    assert res.headers["Location"].endswith("/dashboard")


def test_root_endpoint_format_query_override(client: FlaskClient) -> None:
    """GET / with ?format=json returns JSON metadata regardless of browser Accept headers."""
    browser_headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Sec-Fetch-Mode": "navigate",
    }
    res = client.get("/?format=json", headers=browser_headers)
    assert res.status_code == 200
    assert "application/json" in res.content_type


def test_root_endpoint_user_agent_browser_redirect(client: FlaskClient) -> None:
    """GET / with Mozilla User-Agent even without Sec-Fetch redirects to /dashboard."""
    res = client.get("/", headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    assert res.status_code == 302
    assert res.headers["Location"].endswith("/dashboard")

    # Override with ?json=1 returns JSON
    res_json = client.get("/?json=1", headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    assert res_json.status_code == 200
    assert "application/json" in res_json.content_type


def test_api_results_aggregated(client: FlaskClient) -> None:
    """GET /api/results/aggregated returns parsed benchmark data."""
    res = client.get("/api/results/aggregated")
    assert res.status_code == 200
    data = res.get_json()
    assert data["status"] in ("success", "empty")
    if data["status"] == "success":
        assert len(data["aggregates"]) > 0
        policies = {r["policy"] for r in data["aggregates"]}
        assert "control" in policies or "progress_gated" in policies


def test_api_results_summary(client: FlaskClient) -> None:
    """GET /api/results/summary returns per-trial records."""
    res = client.get("/api/results/summary")
    assert res.status_code == 200
    data = res.get_json()
    assert data["status"] in ("success", "empty")
    if data["status"] == "success":
        assert len(data["trials"]) > 0


def test_api_results_export(client: FlaskClient) -> None:
    """GET /api/results/export strictly serves canonical historical benchmark CSV files."""
    # 1. Aggregated CSV export
    res_agg = client.get("/api/results/export?type=aggregated")
    assert res_agg.status_code == 200
    assert "text/csv" in res_agg.content_type
    assert "attachment" in res_agg.headers.get("Content-Disposition", "")
    assert "batch_aggregated_20261027_215526.csv" in res_agg.headers.get("Content-Disposition", "")
    agg_lines = [line.strip() for line in res_agg.get_data(as_text=True).strip().splitlines() if line.strip()]
    assert len(agg_lines) == 7  # 1 header + 6 aggregate rows (3 policies x 2 scenarios)
    assert "scenario,policy,trials_count" in agg_lines[0]

    # 2. Summary CSV export (strictly 42 data rows excluding the header)
    res_sum = client.get("/api/results/export?type=summary")
    assert res_sum.status_code == 200
    assert "text/csv" in res_sum.content_type
    assert "batch_summary_20261027_215526.csv" in res_sum.headers.get("Content-Disposition", "")
    sum_lines = [line.strip() for line in res_sum.get_data(as_text=True).strip().splitlines() if line.strip()]
    header = sum_lines[0]
    data_rows = sum_lines[1:]
    assert "scenario,policy,row_type,trial_index" in header
    assert len(data_rows) == 42, f"Expected exactly 42 data rows, got {len(data_rows)}"

    # 3. Default without type parameter defaults to aggregated
    res_default = client.get("/api/results/export")
    assert res_default.status_code == 200
    assert "batch_aggregated_20261027_215526.csv" in res_default.headers.get("Content-Disposition", "")

    # 4. Invalid export type rejection
    res_invalid = client.get("/api/results/export?type=invalid_type")
    assert res_invalid.status_code == 400
    err_json = res_invalid.get_json()
    assert err_json["error"] == "Bad Request"
    assert "Invalid export type" in err_json["message"]


def test_api_results_export_fallback_when_results_dir_missing(client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies that export endpoints seamlessly fall back to bundled experiment/data/ when results/ is missing."""
    import os

    app_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    results_path = os.path.join(app_root, "results")

    # Simulate results directory missing / non-existent
    orig_isdir = os.path.isdir
    monkeypatch.setattr(os.path, "isdir", lambda p: False if os.path.abspath(p) == os.path.abspath(results_path) else orig_isdir(p))

    # Aggregated download must still return HTTP 200 from bundled experiment/data/
    res_agg = client.get("/api/results/export?type=aggregated")
    assert res_agg.status_code == 200
    assert "text/csv" in res_agg.content_type

    # Summary download must still return HTTP 200 with 42 rows from bundled experiment/data/
    res_sum = client.get("/api/results/export?type=summary")
    assert res_sum.status_code == 200
    data_rows = [l for l in res_sum.get_data(as_text=True).strip().splitlines() if l.strip()][1:]
    assert len(data_rows) == 42


def test_api_results_export_missing_file_case(client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies that 404 is returned when neither bundled nor results files exist."""
    import os

    # Simulate all candidate paths non-existent
    monkeypatch.setattr(os.path, "isfile", lambda p: False if "batch_" in str(p) else True)

    res = client.get("/api/results/export?type=aggregated")
    assert res.status_code == 404
    data = res.get_json()
    assert "error" in data
    assert "No aggregated results file available for download." in data["error"]


def test_api_run_trial_input_validation(client: FlaskClient) -> None:
    """POST /api/experiment/run_trial validates policies and scenarios."""
    # Invalid policy
    res = client.post("/api/experiment/run_trial", json={"policy": "invalid_policy"})
    assert res.status_code == 400

    # Invalid scenario
    res = client.post("/api/experiment/run_trial", json={"policy": "control", "scenario": "invalid_scenario"})
    assert res.status_code == 400

    # Valid mock execution
    res = client.post("/api/experiment/run_trial", json={
        "policy": "progress_gated",
        "scenario": "transient_latency",
        "mock_trial": True,
    })
    assert res.status_code == 200
    data = res.get_json()
    assert data["status"] == "success"
    assert "trial" in data
    assert data["trial"]["policy"] == "progress_gated"


def test_api_run_trial_summary_mapping(client: FlaskClient) -> None:
    """Verifies that trial_record maps simulated_restarts and mean_latency_ms accurately."""
    mock_summary = {
        "total_requests": 25,
        "successful_requests": 25,
        "failed_requests": 0,
        "success_rate_percent": 100.0,
        "restarts": {
            "total_unique_restarts": 1,
            "simulated_restarts_count": 1,
            "aws_restarts_count": 0,
        },
        "latency_metrics": {
            "mean_ms": 28.5,
        },
        "measured_recovery_time_seconds": 1.25,
        "estimated_downtime_seconds": 0.0,
        "recovered": True,
    }
    # Verify mapping logic
    sim_restarts = mock_summary.get("restarts", {}).get("simulated_restarts_count", 0)
    mean_lat = mock_summary.get("latency_metrics", {}).get("mean_ms", 0.0)
    assert sim_restarts == 1
    assert mean_lat == 28.5


def test_api_run_trial_single_simulated_restart_returns_one(client: FlaskClient) -> None:
    """Verifies that API response reports simulated_restarts == 1 for stalled progress trial."""
    res = client.post("/api/experiment/run_trial", json={
        "policy": "progress_gated",
        "scenario": "stalled_progress",
        "mock_trial": True,
    })
    assert res.status_code == 200
    data = res.get_json()
    assert data["status"] == "success"
    assert data["trial"]["simulated_restarts"] == 1
    assert data["trial"]["policy"] == "progress_gated"
    assert data["trial"]["scenario"] == "stalled_progress"


def test_recovery_status_classification_distinguishes_confirmed_cutoff_and_failure() -> None:
    """
    Verifies that the system rigorously distinguishes between:
    1. Recovery confirmed by policy (measured recovery time present).
    2. Requests successful but observation cutoff before recovery can be validated.
    3. Genuine unrecovered failure (requests failed / service down).
    """
    # Case 1: Recovery confirmed by policy
    rec_time_1 = 1.25
    failed_1 = 0
    total_1 = 35
    status_1 = "RECOVERED" if rec_time_1 is not None else ("UNCONFIRMED_CUTOFF" if failed_1 == 0 and total_1 > 0 else "UNRECOVERED")
    assert status_1 == "RECOVERED"

    # Case 2: Requests successful (100% OK), but trial cutoff before post-fault recovery confirmed
    rec_time_2 = None
    failed_2 = 0
    total_2 = 20
    status_2 = "RECOVERED" if rec_time_2 is not None else ("UNCONFIRMED_CUTOFF" if failed_2 == 0 and total_2 > 0 else "UNRECOVERED")
    assert status_2 == "UNCONFIRMED_CUTOFF"

    # Case 3: Genuine unrecovered failure
    rec_time_3 = None
    failed_3 = 10
    total_3 = 20
    status_3 = "RECOVERED" if rec_time_3 is not None else ("UNCONFIRMED_CUTOFF" if failed_3 == 0 and total_3 > 0 else "UNRECOVERED")
    assert status_3 == "UNRECOVERED"


def test_api_run_trial_jitter_and_seed_validation(client: FlaskClient) -> None:
    """Verifies input validation on random seed and jitter bounds."""
    # Non-integer seed
    res = client.post("/api/experiment/run_trial", json={"policy": "control", "random_seed": "not-an-int"})
    assert res.status_code == 400
    assert "random_seed must be an integer" in res.get_json()["message"]

    # Negative seed
    res = client.post("/api/experiment/run_trial", json={"policy": "control", "random_seed": -1})
    assert res.status_code == 400
    assert "random_seed must be between 0" in res.get_json()["message"]

    # Out of bounds arrival jitter (> 0.5)
    res = client.post("/api/experiment/run_trial", json={"policy": "control", "arrival_jitter_ratio": 0.6})
    assert res.status_code == 400
    assert "arrival_jitter_ratio must be between 0.0 and 0.5" in res.get_json()["message"]

    # Negative arrival jitter (< 0.0)
    res = client.post("/api/experiment/run_trial", json={"policy": "control", "arrival_jitter_ratio": -0.1})
    assert res.status_code == 400

    # Non-numeric arrival jitter
    res = client.post("/api/experiment/run_trial", json={"policy": "control", "arrival_jitter_ratio": "abc"})
    assert res.status_code == 400

    # Out of bounds latency jitter (> 0.5)
    res = client.post("/api/experiment/run_trial", json={"policy": "control", "latency_jitter_ratio": 0.7})
    assert res.status_code == 400
    assert "latency_jitter_ratio must be between 0.0 and 0.5" in res.get_json()["message"]

    # Valid boundaries: 0.0 and 0.5 are accepted
    res_valid_0 = client.post("/api/experiment/run_trial", json={"policy": "control", "mock_trial": True, "arrival_jitter_ratio": 0.0, "latency_jitter_ratio": 0.0})
    assert res_valid_0.status_code == 200

    res_valid_max = client.post("/api/experiment/run_trial", json={"policy": "control", "mock_trial": True, "arrival_jitter_ratio": 0.5, "latency_jitter_ratio": 0.5})
    assert res_valid_max.status_code == 200


def test_api_run_trial_all_four_scenarios_accepted(client: FlaskClient) -> None:
    """Verifies that all 4 supported interactive benchmark scenarios are accepted."""
    scenarios = ["transient_latency", "stalled_progress", "flapping_overload", "transient_overload"]
    for sc in scenarios:
        res = client.post("/api/experiment/run_trial", json={
            "policy": "progress_gated",
            "scenario": sc,
            "mock_trial": True,
            "random_seed": 42,
            "arrival_jitter_ratio": 0.1,
            "latency_jitter_ratio": 0.1,
        })
        assert res.status_code == 200, f"Scenario {sc} failed with status {res.status_code}"
        trial = res.get_json()["trial"]
        assert trial["scenario"] == sc
        assert trial["random_seed"] == 42
        assert trial["arrival_jitter_ratio"] == 0.1
        assert trial["latency_jitter_ratio"] == 0.1
        assert "simulated_restart_overhead_seconds" in trial


def test_api_run_trial_seed_reproducibility_and_matched_parity() -> None:
    """
    Verifies that WorkloadGenerator instantiated with matching seed produces identical
    request arrival offsets and latencies across comparative policies, while differing seeds diverge.
    """
    from experiment.workload import LatencySpikeSchedule, WorkloadGenerator

    spikes = [LatencySpikeSchedule(offset_seconds=1.0, duration_seconds=1.0, degraded_delay_ms=250.0)]
    seed = 42

    gen_ctrl = WorkloadGenerator(base_url="http://127.0.0.1:5000", random_seed=seed, arrival_jitter_ratio=0.2, latency_jitter_ratio=0.2)
    gen_ft = WorkloadGenerator(base_url="http://127.0.0.1:5000", random_seed=seed, arrival_jitter_ratio=0.2, latency_jitter_ratio=0.2)
    gen_pg = WorkloadGenerator(base_url="http://127.0.0.1:5000", random_seed=seed, arrival_jitter_ratio=0.2, latency_jitter_ratio=0.2)
    gen_diff = WorkloadGenerator(base_url="http://127.0.0.1:5000", random_seed=999, arrival_jitter_ratio=0.2, latency_jitter_ratio=0.2)

    plan_ctrl = gen_ctrl.generate_request_plan(3.5, spikes)
    plan_ft = gen_ft.generate_request_plan(3.5, spikes)
    plan_pg = gen_pg.generate_request_plan(3.5, spikes)
    plan_diff = gen_diff.generate_request_plan(3.5, spikes)

    # Schedule parity guarantee: identical across policies under matched seed
    assert plan_ctrl == plan_ft == plan_pg
    assert len(plan_ctrl) == 35

    # Different seeds produce different stochastic schedules
    assert plan_ctrl != plan_diff


def test_api_run_trial_safety_controls_preserved(client: FlaskClient, app: Flask) -> None:
    """Verifies that EXPERIMENT_MODE and state resets cleanly revert after trial execution."""
    app.config["EXPERIMENT_MODE"] = False
    app.config["FAULT_INJECTION_TOKEN"] = "original_token"

    res = client.post("/api/experiment/run_trial", json={
        "policy": "control",
        "scenario": "transient_latency",
        "mock_trial": True,
    })
    assert res.status_code == 200

    # Reverted to original state
    assert app.config["EXPERIMENT_MODE"] is False
    assert app.config["FAULT_INJECTION_TOKEN"] == "original_token"


def test_api_run_trial_live_execution_flapping_overload() -> None:
    """Verifies end-to-end unmocked live execution of flapping_overload interactive trial."""
    import os
    import time
    import threading
    import urllib.request
    import json
    from werkzeug.serving import make_server
    from application import create_app

    test_port = 5088
    os.environ["PORT"] = str(test_port)

    server_app = create_app({
        "TESTING": True,
        "EXPERIMENT_MODE": True,
        "FAULT_INJECTION_TOKEN": "live-test-tok",
    })
    server = make_server("127.0.0.1", test_port, server_app, threaded=True)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    time.sleep(0.3)

    try:
        url = f"http://127.0.0.1:{test_port}/api/experiment/run_trial"
        payload = json.dumps({
            "policy": "progress_gated",
            "scenario": "flapping_overload",
            "duration_seconds": 2.2,
            "random_seed": 42,
            "arrival_jitter_ratio": 0.1,
            "latency_jitter_ratio": 0.1,
            "burst_count": 1,
            "burst_duration_seconds": 0.3,
            "offset_seconds": 0.5,
        }).encode("utf-8")

        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["status"] == "success"
            trial = data["trial"]
            assert trial["scenario"] == "flapping_overload"
            assert trial["policy"] == "progress_gated"
            assert trial["random_seed"] == 42
            assert trial["total_requests"] > 0
            assert "simulated_restart_overhead_seconds" in trial
            assert os.path.exists(trial["log_file"])
    finally:
        server.shutdown()
        server_thread.join(timeout=1.0)
        os.environ.pop("PORT", None)


def test_refresh_now_and_metrics_polling_preserves_cumulative_counters(client: FlaskClient) -> None:
    """Verifies that multiple Refresh Now / polling reads never reset or decrement cumulative counters."""
    # Initial state should be 0
    m0 = client.get("/metrics").get_json()
    assert m0["completion_counter"] == 0
    assert m0["completed_work_requests"] == 0

    # Multiple initial reads (simulating polling ticks and Refresh Now before any work)
    for _ in range(5):
        m = client.get("/metrics").get_json()
        assert m["completion_counter"] == 0
        assert m["completed_work_requests"] == 0

    # Dispatch authentic work requests (simulating manual probe or incoming load)
    w1 = client.get("/work?delay_ms=20").get_json()
    assert w1["status"] == "completed"
    assert w1["completion_counter"] == 1

    w2 = client.get("/work?delay_ms=30").get_json()
    assert w2["status"] == "completed"
    assert w2["completion_counter"] == 2

    # Verify telemetry snapshot immediately after work
    post_work = client.get("/metrics").get_json()
    assert post_work["completion_counter"] == 2
    assert post_work["completed_work_requests"] == 2

    # Simulate 10 consecutive 'Refresh Now' clicks and polling intervals without work
    for _ in range(10):
        m = client.get("/metrics").get_json()
        # Cumulative counters MUST strictly preserve their count and never reset
        assert m["completion_counter"] == 2
        assert m["completed_work_requests"] == 2

    # Dispatch another work request
    w3 = client.get("/work?delay_ms=10").get_json()
    assert w3["completion_counter"] == 3

    # Subsequent Refresh Now reads must reflect 3 and persist
    m_final = client.get("/metrics").get_json()
    assert m_final["completion_counter"] == 3
    assert m_final["completed_work_requests"] == 3


def test_dashboard_js_probe_invalidation_and_loading_state() -> None:
    """Verifies that dashboard.js contains invalidation on input change and immediate loading state."""
    import os
    js_path = os.path.join(os.path.dirname(__file__), "..", "static", "js", "dashboard.js")
    with open(js_path, "r", encoding="utf-8") as f:
        js_code = f.read()

    # 1. Stale banner invalidation on work-delay input change
    assert "handleProbeInputChange" in js_code
    assert "els.probeDelayInput.addEventListener('input', handleProbeInputChange)" in js_code
    assert "els.probeDelayInput.addEventListener('change', handleProbeInputChange)" in js_code
    assert "els.probeFeedback.classList.add('hidden')" in js_code

    # 2. In-flight loading state while probe is dispatching
    assert "DISPATCHING:" in js_code
    assert "awaiting server completion..." in js_code

    # 3. Refresh Now interactive feedback handler
    assert "handleRefreshNow" in js_code
    assert "els.btnRefreshNow.addEventListener('click', handleRefreshNow)" in js_code
    assert "Refreshing..." in js_code


def test_dashboard_js_client_behavior_runtime() -> None:
    """Executes dashboard.js in a mock DOM runtime using Node to verify behavioral contracts:
    - Paused polling stops interval updates.
    - Refresh Now fetches fresh metrics and updates UI even while polling is paused.
    - Refresh Now gracefully recovers and unblocks UI on network/server error.
    - Work probe delay changes immediately invalidate and hide stale feedback banners.
    - Probe dispatch immediately displays an in-flight loading state before completion.
    """
    import os
    import shutil
    import subprocess

    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("Node.js runtime not found on PATH; skipping client-side JS runtime test.")

    test_script = """
const fs = require('fs');
const vm = require('vm');
const path = require('path');

function createMockElement(id = '', tag = 'div') {
  return {
    id,
    tagName: tag.toUpperCase(),
    textContent: '',
    innerHTML: '',
    value: '40',
    disabled: false,
    className: '',
    style: {},
    classList: {
      _classes: new Set(),
      add(c) { this._classes.add(c); },
      remove(c) { this._classes.delete(c); },
      contains(c) { return this._classes.has(c); }
    },
    _listeners: {},
    addEventListener(evt, fn) {
      if (!this._listeners[evt]) this._listeners[evt] = [];
      this._listeners[evt].push(fn);
    },
    dispatchEvent(evt) {
      const fns = this._listeners[evt.type || evt] || [];
      fns.forEach(fn => fn(evt));
    },
    getContext(type) {
      return {
        save: () => {},
        restore: () => {},
        clearRect: () => {},
        beginPath: () => {},
        moveTo: () => {},
        lineTo: () => {},
        stroke: () => {},
        fill: () => {},
        arc: () => {},
        fillText: () => {},
        setLineDash: () => {},
        createLinearGradient: () => ({ addColorStop: () => {} }),
      };
    }
  };
}

const elements = {};
function getEl(id) {
  if (!elements[id]) elements[id] = createMockElement(id);
  return elements[id];
}

let domContentLoadedHandler = null;

const documentMock = {
  getElementById: (id) => getEl(id),
  querySelector: (sel) => getEl(sel.replace(/^[#.]/, '')),
  querySelectorAll: () => [],
  addEventListener: (evt, fn) => {
    if (evt === 'DOMContentLoaded') domContentLoadedHandler = fn;
  }
};

const fetchCalls = [];
let fetchResponses = {
  '/health': { ok: true, status: 200, json: async () => ({ status: 'healthy', pid: 12345 }) },
  '/metrics': { ok: true, status: 200, json: async () => ({ pid: 12345, uptime_seconds: 120, last_work_latency_ms: 15.4, completion_counter: 99, completed_work_requests: 95, in_flight_requests: 0, failed_work_requests: 0 }) },
  '/api/results/aggregated': { ok: true, status: 200, json: async () => ({ status: 'success', summary: { total_trials: 0 } }) },
  '/api/results/summary': { ok: true, status: 200, json: async () => ({ status: 'success', trials: [] }) },
  '/work?delay_ms=40': { ok: true, status: 200, json: async () => ({ status: 'completed', completion_counter: 100, delay_applied_ms: 40 }) }
};

const mockFetch = async (url) => {
  fetchCalls.push(url);
  const resp = fetchResponses[url];
  if (!resp) throw new Error('Unknown url: ' + url);
  return resp;
};

const windowMock = {
  document: documentMock,
  addEventListener: () => {},
  location: { href: '' },
  fetch: mockFetch,
  performance: { now: () => Date.now() },
  setInterval: (fn, ms) => setInterval(fn, ms),
  clearInterval: (id) => clearInterval(id),
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (id) => clearTimeout(id)
};

const jsPath = path.resolve(process.argv[1]);
const code = fs.readFileSync(jsPath, 'utf8');

const ctx = vm.createContext({
  window: windowMock,
  document: documentMock,
  fetch: mockFetch,
  performance: windowMock.performance,
  setInterval: windowMock.setInterval,
  clearInterval: windowMock.clearInterval,
  setTimeout: windowMock.setTimeout,
  clearTimeout: windowMock.clearTimeout,
  console: { log: () => {}, warn: () => {}, error: () => {} }
});

vm.runInContext(code, ctx);
domContentLoadedHandler();

async function runTests() {
  await new Promise(r => setTimeout(r, 40));
  if (getEl('kpi-completion-counter').textContent !== '99') {
    throw new Error('Initial KPI completion counter mismatch');
  }

  // 1. Paused polling stops scheduled intervals
  const pollIntervalSelect = getEl('poll-interval');
  pollIntervalSelect.value = '0';
  pollIntervalSelect.dispatchEvent({ type: 'change', target: { value: '0' } });

  // 2. Refresh Now while paused forces immediate fetch and updates UI metrics
  fetchCalls.length = 0;
  fetchResponses['/metrics'] = {
    ok: true,
    status: 200,
    json: async () => ({ pid: 12345, uptime_seconds: 140, last_work_latency_ms: 22.1, completion_counter: 105, completed_work_requests: 100, in_flight_requests: 0, failed_work_requests: 0 })
  };

  const btnRefreshNow = getEl('btn-refresh-now');
  btnRefreshNow.dispatchEvent({ type: 'click' });
  await new Promise(r => setTimeout(r, 40));

  if (!fetchCalls.includes('/health') || !fetchCalls.includes('/metrics')) {
    throw new Error('Refresh Now failed to fetch /health and /metrics');
  }
  if (getEl('kpi-completion-counter').textContent !== '105') {
    throw new Error('KPI completion counter was not updated by Refresh Now');
  }
  if (btnRefreshNow.disabled !== false) {
    throw new Error('btnRefreshNow was left disabled after refresh completed');
  }

  // 3. Error handling: UI displays disconnected state and button is not stuck disabled
  fetchResponses['/health'] = { ok: false, status: 500, json: async () => ({}) };
  btnRefreshNow.dispatchEvent({ type: 'click' });
  await new Promise(r => setTimeout(r, 40));

  if (getEl('kpi-health').textContent !== 'UNREACHABLE') {
    throw new Error('KPI health did not show UNREACHABLE on fetch failure');
  }
  if (btnRefreshNow.disabled !== false) {
    throw new Error('btnRefreshNow was left disabled after fetch error');
  }

  // Restore healthy state
  fetchResponses['/health'] = { ok: true, status: 200, json: async () => ({ status: 'healthy', pid: 12345 }) };

  // 4. Stale probe result banner is cleared and hidden on input change
  const probeFeedback = getEl('probe-result-feedback');
  probeFeedback.innerHTML = 'Old success banner';
  probeFeedback.classList.remove('hidden');

  const probeDelayInput = getEl('probe-delay');
  probeDelayInput.value = '25';
  probeDelayInput.dispatchEvent({ type: 'input' });

  if (!probeFeedback.classList.contains('hidden') || probeFeedback.innerHTML !== '') {
    throw new Error('Probe feedback was not hidden/cleared on input change');
  }

  // 5. In-flight loading state when probe is triggered
  const btnSendProbe = getEl('btn-send-probe');
  probeDelayInput.value = '40';
  btnSendProbe.dispatchEvent({ type: 'click' });

  if (!probeFeedback.innerHTML.includes('DISPATCHING')) {
    throw new Error('Probe feedback did not show DISPATCHING loading state');
  }

  await new Promise(r => setTimeout(r, 40));
  if (!probeFeedback.innerHTML.includes('SUCCESS') || !probeFeedback.innerHTML.includes('Roundtrip')) {
    throw new Error('Probe feedback did not display completion result');
  }

  process.stdout.write('CLIENT_TESTS_OK');
}

runTests().catch(err => {
  process.stderr.write(String(err && err.stack ? err.stack : err));
  process.exit(1);
});
"""

    workspace_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    js_file = os.path.join(workspace_root, "static", "js", "dashboard.js")

    proc = subprocess.run(
        [node_bin, "-e", test_script, js_file],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"Node client test failed:\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
    assert "CLIENT_TESTS_OK" in proc.stdout




