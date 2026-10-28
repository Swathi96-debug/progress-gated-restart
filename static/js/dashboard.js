/**
 * Progress-Gated Application-Server Restart Dashboard Client Logic
 * Modular Vanilla JS: Telemetry Polling, Canvas Charting, Interactive Probe,
 * Experiment Lab Orchestration, and Verified Results Rendering.
 */

(function () {
  'use strict';

  // State Management
  const state = {
    pollingIntervalMs: 2000,
    pollTimer: null,
    isTrialRunning: false,
    lastCompletionCounter: null,
    telemetryHistory: [], // Max 30 points: { time, latency, delta }
    chartThresholdMs: 150,
  };

  // DOM Elements
  const els = {
    // Nav
    navStatusText: document.getElementById('nav-status-text'),
    navStatusDot: document.querySelector('.pulse-dot'),
    navPid: document.getElementById('nav-pid'),
    navUptime: document.getElementById('nav-uptime'),
    navLatency: document.getElementById('nav-latency'),

    // KPIs
    kpiHealth: document.getElementById('kpi-health'),
    kpiHealthSub: document.getElementById('kpi-health-sub'),
    kpiFaultState: document.getElementById('kpi-fault-state'),
    kpiCompletionCounter: document.getElementById('kpi-completion-counter'),
    kpiCompletedRequests: document.getElementById('kpi-completed-requests'),
    kpiInFlight: document.getElementById('kpi-in-flight'),
    kpiFailed: document.getElementById('kpi-failed'),
    kpiLatency: document.getElementById('kpi-latency'),

    // Controls
    pollIntervalSelect: document.getElementById('poll-interval'),
    btnRefreshNow: document.getElementById('btn-refresh-now'),

    // Probe
    probeDelayInput: document.getElementById('probe-delay'),
    btnSendProbe: document.getElementById('btn-send-probe'),
    probeFeedback: document.getElementById('probe-result-feedback'),

    // Canvas Chart
    canvas: document.getElementById('liveTelemetryChart'),
    chartPointCount: document.getElementById('chart-point-count'),

    // Experiment Studio
    paramDuration: document.getElementById('param-duration'),
    paramThreshold: document.getElementById('param-threshold'),
    paramObsInterval: document.getElementById('param-obs-interval'),
    paramSpikeDelay: document.getElementById('param-spike-delay'),
    paramRandomSeed: document.getElementById('param-random-seed'),
    paramArrivalJitter: document.getElementById('param-arrival-jitter'),
    paramLatencyJitter: document.getElementById('param-latency-jitter'),
    btnLaunchTrial: document.getElementById('btn-launch-trial'),
    progressBar: document.getElementById('trial-progress-bar'),
    stageLabel: document.getElementById('trial-stage-label'),
    timerLabel: document.getElementById('trial-timer'),
    terminal: document.getElementById('trial-terminal'),

    // Result Banner
    resultBanner: document.getElementById('trial-result-banner'),
    resultBadgeStatus: document.getElementById('result-badge-status'),
    resultTrialId: document.getElementById('result-trial-id'),
    resSuccessRate: document.getElementById('res-success-rate'),
    resRestarts: document.getElementById('res-restarts'),
    resRecoveryTime: document.getElementById('res-recovery-time'),
    resDowntime: document.getElementById('res-downtime'),
    resRestartOverhead: document.getElementById('res-restart-overhead'),

    // Modal
    modal: document.getElementById('safety-modal'),
    modalPolicyVal: document.getElementById('modal-policy-val'),
    modalScenarioVal: document.getElementById('modal-scenario-val'),
    modalDurationVal: document.getElementById('modal-duration-val'),
    modalSeedVal: document.getElementById('modal-seed-val'),
    btnModalCancel: document.getElementById('btn-modal-cancel'),
    btnModalConfirm: document.getElementById('btn-modal-confirm'),

    // Results Tables & Exports
    tableAggregatesBody: document.getElementById('table-aggregates-body'),
    tableTrialsBody: document.getElementById('table-trials-body'),
    trialHistoryCount: document.getElementById('trial-history-count'),
    btnExportAgg: document.getElementById('btn-export-agg'),
    btnExportSummary: document.getElementById('btn-export-summary'),
  };

  // ============================================================================
  // 1. Live Telemetry Polling
  // ============================================================================

  async function fetchTelemetry() {
    try {
      const [healthRes, metricsRes] = await Promise.all([
        fetch('/health', { cache: 'no-store' }),
        fetch('/metrics', { cache: 'no-store' }),
      ]);

      if (!healthRes.ok || !metricsRes.ok) {
        throw new Error(`HTTP Error: Health ${healthRes.status}, Metrics ${metricsRes.status}`);
      }

      const health = await healthRes.json();
      const metrics = await metricsRes.json();

      updateTelemetryUI(health, metrics);
    } catch (err) {
      console.warn('Telemetry poll error:', err);
      els.navStatusText.textContent = 'LOCAL NODE DISCONNECTED';
      if (els.navStatusDot) {
        els.navStatusDot.style.background = '#f43f5e';
        els.navStatusDot.style.boxShadow = '0 0 8px #f43f5e';
      }
      els.kpiHealth.textContent = 'UNREACHABLE';
      els.kpiHealth.className = 'kpi-value text-rose';
    }
  }

  function updateTelemetryUI(health, metrics) {
    // Navigation updates
    const isHealthy = health.status === 'healthy';
    els.navStatusText.textContent = isHealthy ? 'LOCAL NODE ONLINE (HEALTHY)' : `LOCAL NODE DEGRADED (${health.status})`;
    if (els.navStatusDot) {
      els.navStatusDot.style.background = isHealthy ? '#10b981' : '#f59e0b';
      els.navStatusDot.style.boxShadow = isHealthy ? '0 0 8px #10b981' : '0 0 8px #f59e0b';
    }

    const pid = metrics.pid || health.pid || '--';
    els.navPid.textContent = pid;

    const uptime = metrics.uptime_seconds != null ? `${metrics.uptime_seconds.toFixed(0)}s` : '--';
    els.navUptime.textContent = uptime;

    const lastLatency = metrics.last_work_latency_ms != null ? metrics.last_work_latency_ms : 0;
    els.navLatency.textContent = `${lastLatency.toFixed(1)} ms`;

    // KPI updates
    els.kpiHealth.textContent = String(health.status || 'unknown').toUpperCase();
    els.kpiHealth.className = isHealthy ? 'kpi-value text-emerald' : 'kpi-value text-amber';
    els.kpiHealthSub.textContent = `PID: ${pid} • Uptime: ${uptime}`;

    const faultState = metrics.current_fault_state || health.fault_state || 'normal';
    els.kpiFaultState.textContent = faultState.toUpperCase();
    els.kpiFaultState.className = faultState === 'normal' ? 'kpi-value text-cyan' : 'kpi-value text-amber';

    const currentCounter = metrics.completion_counter != null ? metrics.completion_counter : 0;
    els.kpiCompletionCounter.textContent = currentCounter.toLocaleString();

    els.kpiCompletedRequests.textContent = (metrics.completed_work_requests || 0).toLocaleString();
    els.kpiInFlight.textContent = metrics.in_flight_requests || 0;
    els.kpiFailed.textContent = metrics.failed_work_requests || 0;
    els.kpiLatency.textContent = `${lastLatency.toFixed(1)} ms`;

    // Calculate completion delta
    let delta = 0;
    if (state.lastCompletionCounter !== null) {
      delta = Math.max(0, currentCounter - state.lastCompletionCounter);
    }
    state.lastCompletionCounter = currentCounter;

    // Record telemetry point for live chart
    recordChartPoint(lastLatency, delta);
  }

  function setupPolling() {
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }

    if (state.pollingIntervalMs > 0) {
      fetchTelemetry(); // initial trigger
      state.pollTimer = setInterval(fetchTelemetry, state.pollingIntervalMs);
    }
  }

  // ============================================================================
  // 2. Interactive Workload Probe & Telemetry Refresh
  // ============================================================================

  function handleProbeInputChange() {
    // Invalidate stale probe result banner when user edits the simulated work delay input
    if (els.probeFeedback) {
      els.probeFeedback.classList.add('hidden');
      els.probeFeedback.innerHTML = '';
    }
  }

  async function handleRefreshNow() {
    if (els.btnRefreshNow) {
      els.btnRefreshNow.disabled = true;
      const origHtml = els.btnRefreshNow.innerHTML;
      els.btnRefreshNow.innerHTML = `<span class="pulse-dot" style="display:inline-block; vertical-align:middle; margin-right:4px;"></span> Refreshing...`;
      try {
        await fetchTelemetry();
      } finally {
        els.btnRefreshNow.disabled = false;
        els.btnRefreshNow.innerHTML = origHtml;
      }
    } else {
      await fetchTelemetry();
    }
  }

  async function handleSendProbe() {
    const delayVal = parseFloat(els.probeDelayInput.value);
    if (isNaN(delayVal) || delayVal < 0 || delayVal > 30000) {
      alert('Delay must be between 0 and 30,000 ms');
      return;
    }

    els.btnSendProbe.disabled = true;
    els.btnSendProbe.innerHTML = `<span class="pulse-dot"></span> Sending...`;

    // Immediately show in-flight loading state in probe feedback banner
    els.probeFeedback.classList.remove('hidden');
    els.probeFeedback.innerHTML = `<span class="pulse-dot" style="display:inline-block; vertical-align:middle; margin-right:6px; background:#06b6d4; box-shadow:0 0 8px #06b6d4;"></span><strong>DISPATCHING:</strong> Sending probe to <code>/work?delay_ms=${delayVal}</code> &bull; awaiting server completion...`;
    els.probeFeedback.style.borderColor = 'rgba(6, 182, 212, 0.4)';
    els.probeFeedback.style.background = 'rgba(6, 182, 212, 0.08)';

    const startPerf = performance.now();
    try {
      const res = await fetch(`/work?delay_ms=${delayVal}`);
      const durationMs = performance.now() - startPerf;
      const data = await res.json();

      els.probeFeedback.classList.remove('hidden');
      if (res.ok) {
        els.probeFeedback.innerHTML = `<strong>SUCCESS:</strong> HTTP 200 OK &bull; Roundtrip: <strong>${durationMs.toFixed(1)} ms</strong> &bull; Server Work Delay: <strong>${data.delay_applied_ms || delayVal} ms</strong> &bull; New Counter: <strong>${data.completion_counter || '--'}</strong>`;
        els.probeFeedback.style.borderColor = 'rgba(16, 185, 129, 0.4)';
        els.probeFeedback.style.background = 'rgba(16, 185, 129, 0.1)';
      } else {
        els.probeFeedback.innerHTML = `<strong>REJECTED:</strong> HTTP ${res.status} &bull; ${data.error || 'Request Failed'} &bull; ${data.reason || ''}`;
        els.probeFeedback.style.borderColor = 'rgba(244, 63, 94, 0.4)';
        els.probeFeedback.style.background = 'rgba(244, 63, 94, 0.1)';
      }

      // Immediate metrics refresh to show updated counter
      await fetchTelemetry();
    } catch (err) {
      els.probeFeedback.classList.remove('hidden');
      els.probeFeedback.innerHTML = `<strong>ERROR:</strong> Network connection failed (${err.message})`;
      els.probeFeedback.style.borderColor = 'rgba(244, 63, 94, 0.4)';
      els.probeFeedback.style.background = 'rgba(244, 63, 94, 0.1)';
    } finally {
      els.btnSendProbe.disabled = false;
      els.btnSendProbe.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg> Send Work Probe`;
    }
  }

  // ============================================================================
  // 3. Real-Time Canvas Charting
  // ============================================================================

  function recordChartPoint(latencyMs, delta) {
    const now = new Date();
    const timeStr = `${now.getMinutes().toString().padStart(2, '0')}:${now.getSeconds().toString().padStart(2, '0')}`;

    state.telemetryHistory.push({
      time: timeStr,
      latency: latencyMs,
      delta: delta,
    });

    if (state.telemetryHistory.length > 30) {
      state.telemetryHistory.shift();
    }

    if (els.chartPointCount) {
      els.chartPointCount.textContent = `${state.telemetryHistory.length} points recorded`;
    }

    drawChart();
  }

  function drawChart() {
    const canvas = els.canvas;
    if (!canvas || !canvas.getContext) return;

    const ctx = canvas.getContext('2d');
    const width = canvas.width;
    const height = canvas.height;

    // Clear canvas
    ctx.clearRect(0, 0, width, height);

    const padding = { top: 25, right: 35, bottom: 30, left: 50 };
    const chartWidth = width - padding.left - padding.right;
    const chartHeight = height - padding.top - padding.bottom;

    // Draw background subtle grid
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.05)';
    ctx.lineWidth = 1;

    for (let y = padding.top; y <= height - padding.bottom; y += chartHeight / 4) {
      ctx.beginPath();
      ctx.moveTo(padding.left, y);
      ctx.lineTo(width - padding.right, y);
      ctx.stroke();
    }

    if (state.telemetryHistory.length < 2) {
      ctx.fillStyle = '#64748b';
      ctx.font = '12px Inter, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('Awaiting real telemetry streams from /metrics...', width / 2, height / 2);
      return;
    }

    // Determine max latency scale (minimum 300ms)
    const maxDataLatency = Math.max(...state.telemetryHistory.map(d => d.latency), state.chartThresholdMs);
    const maxLatency = Math.max(300, Math.ceil(maxDataLatency * 1.2));
    const maxDelta = Math.max(5, ...state.telemetryHistory.map(d => d.delta));

    // Draw threshold line (amber dashed)
    const thresholdY = padding.top + chartHeight - (state.chartThresholdMs / maxLatency) * chartHeight;
    ctx.save();
    ctx.strokeStyle = 'rgba(245, 158, 11, 0.6)';
    ctx.lineWidth = 1.5;
    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    ctx.moveTo(padding.left, thresholdY);
    ctx.lineTo(width - padding.right, thresholdY);
    ctx.stroke();

    ctx.fillStyle = '#f59e0b';
    ctx.font = '10px JetBrains Mono, monospace';
    ctx.textAlign = 'right';
    ctx.fillText(`T = ${state.chartThresholdMs}ms`, width - padding.right - 5, thresholdY - 4);
    ctx.restore();

    // Coordinates mapping
    const stepX = chartWidth / (state.telemetryHistory.length - 1);

    // 1. Draw Latency Line (Cyan)
    ctx.beginPath();
    ctx.strokeStyle = '#06b6d4';
    ctx.lineWidth = 2.5;

    state.telemetryHistory.forEach((pt, i) => {
      const x = padding.left + i * stepX;
      const y = padding.top + chartHeight - (pt.latency / maxLatency) * chartHeight;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();

    // Fill under latency line
    ctx.lineTo(padding.left + (state.telemetryHistory.length - 1) * stepX, padding.top + chartHeight);
    ctx.lineTo(padding.left, padding.top + chartHeight);
    ctx.fillStyle = 'rgba(6, 182, 212, 0.08)';
    ctx.fill();

    // 2. Draw Completion Delta Line (Violet)
    ctx.beginPath();
    ctx.strokeStyle = '#8b5cf6';
    ctx.lineWidth = 2;

    state.telemetryHistory.forEach((pt, i) => {
      const x = padding.left + i * stepX;
      const y = padding.top + chartHeight - (pt.delta / maxDelta) * chartHeight;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();

    // Draw data points for latest reading
    const lastIdx = state.telemetryHistory.length - 1;
    const lastPt = state.telemetryHistory[lastIdx];
    const lastX = padding.left + lastIdx * stepX;
    const lastY = padding.top + chartHeight - (lastPt.latency / maxLatency) * chartHeight;

    ctx.fillStyle = '#06b6d4';
    ctx.beginPath();
    ctx.arc(lastX, lastY, 4, 0, Math.PI * 2);
    ctx.fill();

    // Draw Y-Axis Labels
    ctx.fillStyle = '#64748b';
    ctx.font = '10px JetBrains Mono, monospace';
    ctx.textAlign = 'right';
    ctx.fillText(`${maxLatency}ms`, padding.left - 8, padding.top + 10);
    ctx.fillText(`${Math.round(maxLatency / 2)}ms`, padding.left - 8, padding.top + chartHeight / 2);
    ctx.fillText(`0ms`, padding.left - 8, padding.top + chartHeight);
  }

  // ============================================================================
  // 4. Experiment Lab Execution
  // ============================================================================

  function logTerminal(message, type = 'info') {
    const line = document.createElement('div');
    line.className = `term-line term-${type}`;
    const timestamp = new Date().toLocaleTimeString();
    line.textContent = `[${timestamp}] ${message}`;
    els.terminal.appendChild(line);
    els.terminal.scrollTop = els.terminal.scrollHeight;
  }

  function handleOpenConfirmModal() {
    if (state.isTrialRunning) return;

    const policy = document.querySelector('input[name="exp-policy"]:checked').value;
    const scenario = document.querySelector('input[name="exp-scenario"]:checked').value;
    const duration = parseFloat(els.paramDuration.value) || 3.5;
    const rawSeed = els.paramRandomSeed ? els.paramRandomSeed.value.trim() : '';

    els.modalPolicyVal.textContent = policy.toUpperCase();
    els.modalScenarioVal.textContent = scenario.toUpperCase();
    els.modalDurationVal.textContent = `${duration}s`;
    if (els.modalSeedVal) {
      els.modalSeedVal.textContent = rawSeed !== '' ? rawSeed : 'None (Deterministic baseline)';
    }

    els.modal.classList.remove('hidden');
  }

  function handleCloseModal() {
    els.modal.classList.add('hidden');
  }

  async function handleExecuteTrial() {
    handleCloseModal();

    if (state.isTrialRunning) return;
    state.isTrialRunning = true;
    els.btnLaunchTrial.disabled = true;
    els.resultBanner.classList.add('hidden');

    const policy = document.querySelector('input[name="exp-policy"]:checked').value;
    const scenario = document.querySelector('input[name="exp-scenario"]:checked').value;
    const duration = parseFloat(els.paramDuration.value) || 3.5;
    const threshold = parseFloat(els.paramThreshold.value) || 150.0;
    const obsInterval = parseFloat(els.paramObsInterval.value) || 0.3;
    const degradedDelay = parseFloat(els.paramSpikeDelay.value) || 250.0;

    const rawSeed = els.paramRandomSeed ? els.paramRandomSeed.value.trim() : '';
    let randomSeed = null;
    if (rawSeed !== '') {
      const parsedSeed = parseInt(rawSeed, 10);
      if (isNaN(parsedSeed) || parsedSeed < 0 || parsedSeed > 2147483647) {
        logTerminal('Validation Error: Random seed must be an integer between 0 and 2147483647', 'error');
        state.isTrialRunning = false;
        els.btnLaunchTrial.disabled = false;
        return;
      }
      randomSeed = parsedSeed;
    }

    const rawArrival = els.paramArrivalJitter ? parseFloat(els.paramArrivalJitter.value) : 0.0;
    if (isNaN(rawArrival) || rawArrival < 0.0 || rawArrival > 0.5) {
      logTerminal('Validation Error: Arrival jitter ratio must be between 0.0 and 0.5', 'error');
      state.isTrialRunning = false;
      els.btnLaunchTrial.disabled = false;
      return;
    }
    const arrivalJitter = rawArrival;

    const rawLatency = els.paramLatencyJitter ? parseFloat(els.paramLatencyJitter.value) : 0.0;
    if (isNaN(rawLatency) || rawLatency < 0.0 || rawLatency > 0.5) {
      logTerminal('Validation Error: Latency jitter ratio must be between 0.0 and 0.5', 'error');
      state.isTrialRunning = false;
      els.btnLaunchTrial.disabled = false;
      return;
    }
    const latencyJitter = rawLatency;

    // Update chart reference threshold
    state.chartThresholdMs = threshold;

    logTerminal(`Launching trial: Policy=${policy}, Scenario=${scenario}, Duration=${duration}s...`, 'info');
    els.stageLabel.textContent = `RUNNING • Scenario: ${scenario} (${policy})`;
    els.progressBar.style.width = '15%';

    // Visual timer countdown
    const startTime = performance.now();
    const durationMs = duration * 1000;
    const timerInterval = setInterval(() => {
      const elapsed = performance.now() - startTime;
      const progress = Math.min(92, (elapsed / durationMs) * 90);
      els.progressBar.style.width = `${progress}%`;
      els.timerLabel.textContent = `${(elapsed / 1000).toFixed(1)}s / ${duration.toFixed(1)}s`;
    }, 100);

    try {
      logTerminal(`Dispatched request schedule: pacing 10 req/sec over ${duration}s`, 'info');
      if (scenario === 'transient_latency') {
        logTerminal(`Configured latency spike: ${degradedDelay}ms delay from t=1.0s to t=2.0s (&Delta;C > 0)`, 'info');
      } else if (scenario === 'stalled_progress') {
        logTerminal(`Configured stall fault: worker threads stall from t=1.0s (&Delta;C == 0)`, 'warn');
      } else if (scenario === 'flapping_overload') {
        logTerminal(`Configured flapping overload: 2 bursts of 400ms HTTP 503 load-shedding with 300ms recovery gap`, 'warn');
      } else if (scenario === 'transient_overload') {
        logTerminal(`Configured transient overload: 1.0s sustained HTTP 503 load-shedding window`, 'warn');
      }

      if (randomSeed !== null) {
        logTerminal(`Seeded execution: seed=${randomSeed}, arrival_jitter=${arrivalJitter}, latency_jitter=${latencyJitter}`, 'info');
      } else {
        logTerminal(`Deterministic baseline execution: unseeded, zero jitter (schedule parity guaranteed)`, 'info');
      }

      const res = await fetch('/api/experiment/run_trial', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          policy: policy,
          scenario: scenario,
          duration_seconds: duration,
          latency_threshold_ms: threshold,
          observation_interval_seconds: obsInterval,
          degraded_delay_ms: degradedDelay,
          random_seed: randomSeed,
          arrival_jitter_ratio: arrivalJitter,
          latency_jitter_ratio: latencyJitter,
        }),
      });

      clearInterval(timerInterval);
      els.progressBar.style.width = '100%';

      if (!res.ok) {
        const errData = await res.json();
        throw new Error(errData.error || `HTTP ${res.status}`);
      }

      const data = await res.json();
      const trial = data.trial;

      logTerminal(`Trial completed! Trial ID: ${trial.trial_id}`, 'success');
      logTerminal(`Requests: ${trial.successful_requests}/${trial.total_requests} (${trial.success_rate_percent}%) • Restarts: ${trial.simulated_restarts}`, 'success');
      const overheadStr = trial.simulated_restart_overhead_seconds != null ? `${trial.simulated_restart_overhead_seconds}s` : '0.00s';
      logTerminal(`Recovery Time: ${trial.recovery_time_seconds != null ? trial.recovery_time_seconds + 's' : 'N/A (Unrecovered)'} • Downtime: ${trial.estimated_downtime_seconds}s • Restart Overhead: ${overheadStr}`, 'info');

      // Display completed result banner
      renderResultBanner(trial);

      // Refresh benchmark table and trial records
      loadTrialHistory();
      loadAggregatedResults();
      fetchTelemetry();
    } catch (err) {
      clearInterval(timerInterval);
      logTerminal(`Execution Failed: ${err.message}`, 'error');
      els.stageLabel.textContent = `ERROR • ${err.message}`;
      els.progressBar.style.width = '0%';
    } finally {
      state.isTrialRunning = false;
      els.btnLaunchTrial.disabled = false;
      els.stageLabel.textContent = `IDLE • Ready for execution`;
    }
  }

  function renderResultBanner(trial) {
    els.resultBanner.classList.remove('hidden');
    els.resultTrialId.textContent = trial.trial_id;

    if (trial.recovery_time_seconds != null) {
      els.resultBadgeStatus.textContent = 'RECOVERED';
      els.resultBadgeStatus.style.background = 'rgba(16, 185, 129, 0.2)';
      els.resultBadgeStatus.style.color = '#10b981';
    } else if (trial.recovery_status === 'UNCONFIRMED_CUTOFF' || (trial.failed_requests === 0 && trial.total_requests > 0)) {
      els.resultBadgeStatus.textContent = 'UNCONFIRMED (TRIAL CUTOFF)';
      els.resultBadgeStatus.style.background = 'rgba(245, 158, 11, 0.2)';
      els.resultBadgeStatus.style.color = '#f59e0b';
    } else {
      els.resultBadgeStatus.textContent = 'UNRECOVERED';
      els.resultBadgeStatus.style.background = 'rgba(244, 63, 94, 0.2)';
      els.resultBadgeStatus.style.color = '#f43f5e';
    }

    els.resSuccessRate.textContent = `${trial.success_rate_percent}%`;
    const simRestarts = (trial.simulated_restarts != null) ? trial.simulated_restarts : (trial.restarts && trial.restarts.simulated_restarts_count != null ? trial.restarts.simulated_restarts_count : 0);
    els.resRestarts.textContent = `${simRestarts} (Simulated)`;
    els.resRecoveryTime.textContent = trial.recovery_time_seconds != null ? `${trial.recovery_time_seconds}s` : 'N/A';
    els.resDowntime.textContent = `${trial.estimated_downtime_seconds}s`;
    if (els.resRestartOverhead) {
      const overhead = trial.simulated_restart_overhead_seconds != null ? trial.simulated_restart_overhead_seconds : 0.0;
      els.resRestartOverhead.textContent = `${overhead.toFixed(2)}s`;
    }
  }

  // ============================================================================
  // 5. Benchmark Data Loading & CSV Rendering
  // ============================================================================

  async function loadAggregatedResults() {
    try {
      const res = await fetch('/api/results/aggregated');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);

      const data = await res.json();
      if (!data.aggregates || data.aggregates.length === 0) {
        els.tableAggregatesBody.innerHTML = `<tr><td colspan="9" class="text-center">No aggregated results available yet. Run a batch to generate.</td></tr>`;
        return;
      }

      els.tableAggregatesBody.innerHTML = '';
      data.aggregates.forEach(agg => {
        const tr = document.createElement('tr');
        const isProgressGated = agg.policy === 'progress_gated';
        const isFixed = agg.policy === 'fixed_threshold';

        tr.innerHTML = `
          <td><strong>${agg.scenario}</strong></td>
          <td><span class="${isProgressGated ? 'text-emerald' : (isFixed ? 'text-amber' : '')}">${agg.policy}</span></td>
          <td>${agg.trials_count}</td>
          <td><span class="${agg.recovered_trials_count === agg.trials_count ? 'text-emerald' : 'text-rose'}">${agg.recovered_trials_count}/${agg.trials_count}</span></td>
          <td><strong>${agg.success_rate_mean_percent}%</strong> <span style="color:#64748b">&plusmn;${agg.success_rate_std_percent}%</span></td>
          <td><span class="${agg.simulated_restarts_mean > 0 && agg.scenario === 'transient_latency' ? 'text-rose' : ''}">${agg.simulated_restarts_mean}</span></td>
          <td>${agg.real_aws_restarts_mean}</td>
          <td>${agg.recovery_time_mean_seconds !== 'N/A' ? `${agg.recovery_time_mean_seconds}s <span style="color:#64748b">&plusmn;${agg.recovery_time_std_seconds}s</span>` : '<span class="text-rose">N/A</span>'}</td>
          <td>${agg.estimated_downtime_mean_seconds}s</td>
        `;
        els.tableAggregatesBody.appendChild(tr);
      });
    } catch (err) {
      console.warn('Failed to load aggregated results:', err);
      els.tableAggregatesBody.innerHTML = `<tr><td colspan="9" class="text-center text-rose">Failed to load aggregated results (${err.message})</td></tr>`;
    }
  }

  async function loadTrialHistory() {
    try {
      const res = await fetch('/api/results/summary');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);

      const data = await res.json();
      if (!data.trials || data.trials.length === 0) {
        els.tableTrialsBody.innerHTML = `<tr><td colspan="10" class="text-center">No individual trial records found in results/.</td></tr>`;
        return;
      }

      els.trialHistoryCount.textContent = `${data.trials.length} trials recorded`;
      els.tableTrialsBody.innerHTML = '';

      // Display most recent 12 trials
      const recentTrials = data.trials.slice(0, 15);
      recentTrials.forEach(t => {
        const tr = document.createElement('tr');
        const recovered = t.recovery_time_seconds !== 'N/A';
        tr.innerHTML = `
          <td>${t.trial_index}</td>
          <td>${t.scenario}</td>
          <td><strong>${t.policy}</strong></td>
          <td title="${t.trial_id}">${t.trial_id.length > 24 ? t.trial_id.substring(0, 24) + '...' : t.trial_id}</td>
          <td>${t.successful_requests}/${t.total_requests}</td>
          <td><span class="${t.success_rate_percent >= 99 ? 'text-emerald' : 'text-rose'}">${t.success_rate_percent}%</span></td>
          <td>${t.simulated_restarts}</td>
          <td><span class="${recovered ? 'text-emerald' : 'text-rose'}">${t.recovery_time_seconds}</span></td>
          <td>${t.estimated_downtime_seconds}s</td>
          <td>${t.mean_latency_ms} ms</td>
        `;
        els.tableTrialsBody.appendChild(tr);
      });
    } catch (err) {
      console.warn('Failed to load trial history:', err);
      els.tableTrialsBody.innerHTML = `<tr><td colspan="10" class="text-center text-rose">Failed to load trial history (${err.message})</td></tr>`;
    }
  }

  // ============================================================================
  // 6. CSV Exports
  // ============================================================================

  function handleExportAggregated() {
    window.location.href = '/api/results/export?type=aggregated';
  }

  function handleExportSummary() {
    window.location.href = '/api/results/export?type=summary';
  }

  // ============================================================================
  // 7. Initialization & Event Binding
  // ============================================================================

  function init() {
    // Polling controls
    els.pollIntervalSelect.addEventListener('change', (e) => {
      state.pollingIntervalMs = parseInt(e.target.value, 10);
      setupPolling();
    });

    els.btnRefreshNow.addEventListener('click', handleRefreshNow);
    els.btnSendProbe.addEventListener('click', handleSendProbe);
    if (els.probeDelayInput) {
      els.probeDelayInput.addEventListener('input', handleProbeInputChange);
      els.probeDelayInput.addEventListener('change', handleProbeInputChange);
    }

    // Modal & Trial Launch
    els.btnLaunchTrial.addEventListener('click', handleOpenConfirmModal);
    els.btnModalCancel.addEventListener('click', handleCloseModal);
    els.btnModalConfirm.addEventListener('click', handleExecuteTrial);

    // CSV Exports
    els.btnExportAgg.addEventListener('click', handleExportAggregated);
    els.btnExportSummary.addEventListener('click', handleExportSummary);

    // Initial triggers
    setupPolling();
    loadAggregatedResults();
    loadTrialHistory();

    // Resize handler for chart
    window.addEventListener('resize', drawChart);
  }

  document.addEventListener('DOMContentLoaded', init);
})();
