# Progress-Gated Application-Server Restart Under Transient Load
> Operating Systems case study comparing progress-gated and fixed-threshold application restart policies under transient load.
>
> **Course:** Operating Systems Case Study (BTech 3rd Year)  
> **Deployment Target:** AWS Elastic Beanstalk (Single-Instance Python Platform)  
> **Architecture:** Flask Foundation & Experiment Framework

---

## 1. Project Overview & Architectural Context

This case study investigates **progress-gated restart policies** for application servers subjected to transient overload or deadlock-like degradation. 

In traditional cloud operations, health checks often rely on binary liveness endpoints (`/health`). However, under transient load spikes, resource exhaustion, or lock contention, an application server might still respond to lightweight health probes while its primary worker threads are stalled and making zero forward progress on real client work. Conversely, an aggressive timeout-based reboot policy risks restarting servers that are simply experiencing temporary delay, exacerbating queue backpressure and creating a cascading reboot storm.

To study and evaluate restart policies under controlled conditions:
- The application tracks a **monotonically increasing completion counter** reflecting actual completed units of work.
- Worker threads record detailed latency and throughput metrics.
- Controlled **fault injection** primitives simulate temporary overload, stalled progress, and hard failure without modifying OS kernels or running untrusted container runtimes.
- An independent **experiment framework** evaluates three comparative policies sharing identical latency thresholds and cooldowns:
  1. **No-Restart Control (`NoRestartPolicy`)**: Observes natural system recovery under transient load without intervention.
  2. **Fixed-Threshold Policy (`FixedThresholdPolicy`)**: Requests a restart immediately when the common latency threshold is met, subject to shared cooldown and duplicate-restart protection.
  3. **Progress-Gated Policy (`ProgressGatedPolicy`)**: When the common latency trigger is met, observes progress for a bounded interval; continues waiting while progress occurs (`counter_delta > 0`) and recovery remains possible, but restarts on confirmed stalled progress (`counter_delta == 0`) or when the hard deadline expires.
- Restart operations are decoupled through a pluggable **adapter interface**:
  - `SimulatedRestartAdapter`: For zero-cost local automated testing and development.
  - `ElasticBeanstalkRestartAdapter`: For future AWS Elastic Beanstalk environments via `boto3`.

> [!NOTE]
> **Scientific Integrity & Scope Note:**  
> We do not claim that the project or algorithm is novel based on implementation alone. It serves as an empirical BTech operating systems case study. Mocked restart events are strictly labeled as simulated and never combined with real AWS measurements.

---

## 2. Project Directory Structure

```
progress-gated-restart/
├── application.py          # Flask WSGI application, endpoints, thread-safe state & logging
├── requirements.txt        # Pinned production, testing, and AWS SDK dependencies
├── simulate_local.py       # Self-contained CLI runner for local benchmark simulations
├── .env.example            # Sample development environment configuration
├── .gitignore              # Ignores virtual environments, caches, secrets, and test logs
├── README.md               # Architecture documentation, runbooks, and endpoint reference
├── results/                # Output directory for immutable JSONL and CSV event logs
├── experiment/             # Modular experiment and control framework
│   ├── __init__.py         # Package exports
│   ├── models.py           # Dataclasses (PolicyConfig, ProgressObservation, PolicyDecision, etc.)
│   ├── adapters.py         # RestartAdapter base, SimulatedRestartAdapter, ElasticBeanstalkRestartAdapter
│   ├── policies.py         # NoRestartPolicy, FixedThresholdPolicy, ProgressGatedPolicy
│   ├── controller.py       # RestartController, guarded restarts, progress polling loops
│   ├── workload.py         # WorkloadGenerator (normal, transient overload, stalled progress)
│   ├── runner.py           # ExperimentRunner orchestrator with bounded run times
│   └── analysis.py         # Immutable log analyzer, recovery time, downtime, and reporting
└── tests/
    ├── __init__.py         # Test package marker
    ├── test_application.py # Application and endpoint tests (20 tests)
    ├── test_policies.py    # Policy evaluation, counter resets, cooldowns, adapter tests (13 tests)
    └── test_workload_and_analysis.py # Logger, trial IDs, analyzer accuracy, and scenario tests (4 tests)
```

---

## 3. Setup & Execution (Windows PowerShell)

Follow these exact commands in Windows PowerShell:

### 3.1. Clone / Navigate to Project Directory
```powershell
Set-Location -Path "C:\Users\Swathi\.gemini\antigravity-ide\scratch\progress-gated-restart"
```

### 3.2. Create and Activate Virtual Environment
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```
*(If PowerShell script execution is restricted, run `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process` prior to activating).*

### 3.3. Install Dependencies
```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 3.4. Configure Environment Variables for Development
```powershell
Copy-Item .env.example .env
```
For local testing with fault injection enabled:
```powershell
$env:EXPERIMENT_MODE="true"
$env:FAULT_INJECTION_TOKEN="my-local-secret-token"
```

### 3.5. Run Automated Tests
```powershell
pytest -v
```
*(Runs 41 automated regression tests covering endpoints, thread safety, policies, cooldowns, adapter mutations, analyzer deduplication, and batch summary aggregation).*

### 3.6. Run Matched Simulation Benchmarks (Single Trials)
Execute matched trials across policies with identical workload and fault schedules:
```powershell
# Scenario A: Transient Latency Degradation (work succeeds with elevated latency, then recovers naturally)
python simulate_local.py --policy control --scenario transient_latency --duration 5.0
python simulate_local.py --policy progress_gated --scenario transient_latency --duration 5.0
python simulate_local.py --policy fixed_threshold --scenario transient_latency --duration 5.0

# Scenario B: Stalled Progress Fault (work deadlocks until restart intervention clears fault)
python simulate_local.py --policy control --scenario stalled_progress --duration 5.0
python simulate_local.py --policy progress_gated --scenario stalled_progress --duration 5.0
python simulate_local.py --policy fixed_threshold --scenario stalled_progress --duration 5.0
```

### 3.7. Run Reproducible Batch Experiment (30 Matched Trials)
Execute all three policies against both scenarios with 5 trials per combination (30 trials total):
```powershell
# Run the complete 30-trial batch
python simulate_local.py --batch --trials 5 --duration 3.5

# Inspect the generated per-trial and aggregate summary CSV in PowerShell:
Get-ChildItem results/batch_summary_*.csv | Select-Object -Last 1 | Import-Csv | Format-Table -AutoSize

# Inspect only the aggregate statistical means and standard deviations:
Get-ChildItem results/batch_aggregated_*.csv | Select-Object -Last 1 | Import-Csv | Format-Table -AutoSize
```

### 3.8. Launch Development Server (Standalone)
```powershell
python application.py
```
The server starts on `http://127.0.0.1:5000` with multi-threading enabled (`threaded=True`).

---

## 4. Policy Architectures & Correctness Guarantees

Both comparative policies (`FixedThresholdPolicy` and `ProgressGatedPolicy`) and the control policy (`NoRestartPolicy`) share identical parameters via `PolicyConfig`:
- **`latency_threshold_ms`**: Common latency threshold T beyond which a probe/request is flagged as degraded.
- **`observation_interval_seconds`**: Bounded interval between consecutive progress observations.
- **`hard_deadline_seconds`**: Maximum allowed continuous degradation/stall before restart.
- **`cooldown_seconds`**: Minimum quiet period enforced after a restart before another can be initiated.
- **`recovery_consecutive_successes`**: Number of consecutive healthy probes required to certify recovery.

### Policy Trigger Differences:
1. **`FixedThresholdPolicy`**:
   - Triggers a restart **immediately** upon meeting the latency threshold T (`probe_latency_ms > latency_threshold_ms` or health failure), subject to shared cooldown and duplicate-restart protection.
   - It does **not** wait for `hard_deadline_seconds` or evaluate progress counter deltas.
2. **`ProgressGatedPolicy`**:
   - When the same latency threshold T is crossed, it enters an observation phase for a bounded interval (`observation_interval_seconds`).
   - If work is progressing (`counter_delta > 0`), it continues waiting while recovery remains possible.
   - If progress is confirmed stalled (`counter_delta == 0` over the observation interval), it initiates a restart.
   - If degradation persists past `hard_deadline_seconds`, it initiates a restart as a safety upper bound.
3. **`NoRestartPolicy`**:
   - Observes and records natural recovery behavior without ever initiating a restart.

### Anomaly Handling:
1. **Counter Resets**: When `completion_counter < last_counter` (e.g. after a process restart or container reload), the baseline is re-established immediately. The drop is never mistaken for new completed work nor interpreted as a stall.
2. **Process Identifier Changes**: When `pid != last_pid`, the baseline is updated and the delta reset to 0 without misinterpreting as a stall.
3. **Probe Failures / Missing Metrics**: If an HTTP health or metrics probe fails due to network timeout or connection reset, the policy withholds stall conclusions. Missing telemetry is never treated as proof of stalled progress (`delta == 0`).
4. **Duplicate / Overlapping Restarts**: `RestartController` uses a strict `threading.Lock` and status flag. If a restart is already executing, all concurrent trigger attempts are rejected.
5. **Bounded Polling & Execution**: All loops in `RestartController` and `WorkloadGenerator` enforce explicit timeouts (`duration_seconds`) to guarantee that no test or run can hang indefinitely.

---

## 5. Restart Adapters & AWS Elastic Beanstalk Scope

### 5.1. `SimulatedRestartAdapter`
- Strictly designated with `is_simulated = True` and `adapter_type = "simulated"`.
- **State Mutation Semantics**:
  - If `on_restart_callback` is provided (e.g. in test suites or `simulate_local.py`), this adapter executes the callback to reset local server fault state or counters.
  - If `on_restart_callback` is `None`, this adapter ONLY records the restart event and simulates execution latency without altering the running server process state.
  - Every restart record includes `details["state_mutated"]` for auditing.

### 5.2. `ElasticBeanstalkRestartAdapter`
- Designated with `is_simulated = False` and `adapter_type = "aws_elastic_beanstalk"`.
- Uses `boto3.client('elasticbeanstalk').restart_app_server(EnvironmentName=...)`.

> [!WARNING]
> **Important Elastic Beanstalk Operational Scope:**
> - `RestartAppServer` restarts the **application container server** (Gunicorn, Nginx, Apache) across **every EC2 instance** in the target environment.
> - It does **not** reboot the underlying EC2 instance or OS kernel.
> - We do **not** claim that it necessarily restarts a particular Python process until that behavior has been empirically verified and benchmarked on the specific AWS platform.
> - Actual AWS restart invocations are never executed during local testing.

---

## 6. Telemetry & Log Analysis Definitions

Every benchmark run produces immutable event logs in `results/`:
- `<run_id>.jsonl`: Complete, atomic JSONL stream of requests, progress observations, policy decisions, restart events, and recoveries. Every entry contains a timestamp, ISO timestamp, and `trial_id`.
- `<run_id>_requests.csv`: High-throughput tabular log of every individual request with `trial_id`, timestamps, latency, and status.

### Metric Definitions:
1. **Recovery Time**: The elapsed duration (in seconds) from the initial scheduled fault injection timestamp until the first subsequent validated recovery event (requiring N consecutive healthy, low-latency probes). If no valid recovery event occurred before trial termination, recovery time is reported as `None` / `N/A`.
2. **Estimated Downtime (Request-Inferred)**: The empirical duration (in seconds) between the first failed work request and the first subsequent successful work request. Because client request arrival is discrete rather than continuous, this metric is strictly labeled as an estimated request-inferred downtime, not a continuous kernel-measured outage.
3. **Restart Deduplication**: Restarts are tracked by unique UUIDs (`restart_id`). Duplicate log entries are never counted twice. Simulated restarts are strictly segregated from real AWS Elastic Beanstalk API requests.

### Analyzing Log Files Directly:
```powershell
python -m experiment.analysis results/local_sim_fixed_threshold_transient_overload.jsonl
```

---

## 7. Application Endpoints & Verification

### 7.1. `GET /` — Project Metadata
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:5000/" | ConvertTo-Json -Depth 4
```

### 7.2. `GET /health` — Liveness & Crash State Probe
Returns HTTP 200 during normal operation, temporary overload, or stalled progress. Returns HTTP 500 when simulated hard failure is active.
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:5000/health"
```

### 7.3. `GET /metrics` — Telemetry Snapshot
Returns non-blocking JSON containing PID, uptime, completion counter, completed/failed requests, and fault state.
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:5000/metrics" | ConvertTo-Json -Depth 4
```

### 7.4. `GET /work?delay_ms=N` — Simulated Work Execution
Simulates CPU/IO work with duration specified in milliseconds (allowed bounds: `0` to `30000`). Increments `completion_counter` only on successful completion.
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:5000/work?delay_ms=25"
```

### 7.5. `POST /experiment/fault` — Controlled Fault Injection
Requires `EXPERIMENT_MODE=true` and `X-Fault-Token` header.
- **`normal`**: Clears active faults.
- **`temporary_overload`**: Rejects work requests with HTTP 503 for `duration_seconds`, then automatically recovers to `normal`.
- **`simulated_stalled_progress`**: Causes worker threads to wait on a synchronization event without blocking `/metrics` or `/health`.
- **`hard_failure`**: Causes `/health` and `/work` to return HTTP 500.

---

## 8. Cautious AWS Elastic Beanstalk Deployment Checklist (NOT YET EXECUTED)

> [!IMPORTANT]
> **Safety & Scope Boundary:**  
> The project is currently configured and benchmarked for local execution only. **No AWS resources have been provisioned and no AWS APIs have been invoked.** The following checklist outlines the necessary pre-flight steps and architecture safeguards required prior to any future AWS deployment.

### 8.1. WSGI Concurrency & Procfile
- [x] **Procfile Configured:** [Procfile](file:///C:/Users/Swathi/.gemini/antigravity-ide/scratch/progress-gated-restart/Procfile) is created with single-worker multi-threaded Gunicorn:
  ```
  web: gunicorn --workers=1 --threads=8 application:application
  ```
- [x] **In-Memory Counter Integrity:** Because `completion_counter`, `stall_event`, and telemetry reside in Python memory, Gunicorn must NEVER be run with `--workers > 1` on AWS unless state is migrated to Redis, Memcached, or an OS-level shared memory segment (`mmap` / POSIX IPC). Multi-worker Gunicorn would fragment the completion counter across worker PIDs.
- [x] **Dependencies:** `gunicorn>=21.2.0,<23.0.0` is pinned in `requirements.txt`.

### 8.2. Secrets Management & Environment Configuration
- [x] **Zero Hardcoded Secrets:** No tokens, API keys, or AWS credentials are committed. `.env` is ignored by `.gitignore`.
- [ ] **Provision Environment Properties:** Prior to running live experiments, inject configuration via AWS Elastic Beanstalk Environment Properties or AWS Systems Manager (SSM) Parameter Store:
  ```powershell
  # Configure via AWS CLI (execute only when provisioning environment)
  aws elasticbeanstalk update-environment `
      --environment-name "progress-gated-env" `
      --option-settings `
          Namespace=aws:elasticbeanstalk:application:environment,OptionName=EXPERIMENT_MODE,Value="true" `
          Namespace=aws:elasticbeanstalk:application:environment,OptionName=FAULT_INJECTION_TOKEN,Value="<cryptographic-secret-token>"
  ```
- [ ] **SSM Parameter Store Alternative (Recommended for Production):** Store encrypted token in `/progress-gated/fault-token` with `SecureString` KMS encryption.

### 8.3. IAM Role & Controller Permissions
- [ ] **Controller IAM Policy:** When running `ElasticBeanstalkRestartAdapter` against live AWS infrastructure, the executing IAM identity requires the following least-privilege permission policy:
  ```json
  {
      "Version": "2012-10-17",
      "Statement": [
          {
              "Effect": "Allow",
              "Action": "elasticbeanstalk:RestartAppServer",
              "Resource": "arn:aws:elasticbeanstalk:<region>:<account-id>:environment/<app-name>/<environment-name>"
          }
      ]
  }
  ```
- [ ] **Instance Profile:** EC2 instances in the single-instance Elastic Beanstalk environment require standard `AWSElasticBeanstalkWebTier` permissions.

### 8.4. Asynchronous Cloud Restart Semantics & Timeout Handling
- [ ] **Asynchronous API Behavior:** `client.restart_app_server()` is an asynchronous API call. It returns HTTP 200 once the restart command is queued; it does NOT wait for container processes to reload.
- [ ] **Restart Latency Window:** While `SimulatedRestartAdapter` completes state mutation in ~50ms, AWS Elastic Beanstalk container restarts take between 15 and 45 seconds.
- [ ] **Controller Cooldown Calibration:** `PolicyConfig.cooldown_seconds` must be increased from 1.5s to at least 45.0s on AWS to prevent triggering cascading restarts while Gunicorn / Nginx are mid-reload.
- [ ] **Audit Trail Segregation:** All restart events emitted by `ElasticBeanstalkRestartAdapter` carry `is_simulated = False` and `adapter_type = "aws_elastic_beanstalk"` to ensure cloud actions are never conflated with simulated local trials.

### 8.5. Pre-Flight Staging Smoke Test Checklist
Before launching autonomous experiment workloads against AWS:
1. `GET /health` returns HTTP 200 and `"status": "healthy"`.
2. `GET /metrics` returns valid monotonic counter, PID, and uptime.
3. `GET /work?delay_ms=25` returns HTTP 200 and increments `completion_counter` by exactly 1.
4. `POST /experiment/fault` with invalid token returns HTTP 401 Unauthorized.
5. Verify that single-worker multi-threaded Gunicorn is actively serving requests without worker churn.

---

## 9. Live AWS Experiment Runner (`run_live_aws.py`)

A dedicated entry point, [run_live_aws.py](file:///C:/Users/Swathi/.gemini/antigravity-ide/scratch/progress-gated-restart/run_live_aws.py), orchestrates live cloud benchmark trials against deployed Elastic Beanstalk environments with strict safety gates.

### 9.1. Safety Features & Architectural Guarantees
1. **Explicit Restarts Authorization:** The `--allow-restarts` flag is mandatory for `fixed_threshold` and `progress_gated`. Without this flag or `--prohibit-restarts`, the runner aborts with a safety exception before initiating any action.
2. **Mechanically Enforced No-Restart Mode:** For transient-latency validation, the `--prohibit-restarts` flag mechanically blocks and intercepts any restart attempt without calling AWS APIs. If any policy requests a restart, the trial fails safely.
3. **Explicit Fault Authorization & Mandatory Token:** The `--allow-fault-injection` flag AND a configured, non-empty secret token (`--fault-token` or `FAULT_INJECTION_TOKEN` env var) are mandatory for `stalled_progress`.
4. **No Silent Fallback:** The live runner uses `ElasticBeanstalkRestartAdapter` and never silently falls back to local simulated restarts.
5. **Isolated Control Permissions:** The `control` policy never invokes AWS restart APIs and can run without AWS credentials or environment permissions.
6. **Honest Recovery Lifecycle:** An HTTP 200 from AWS API only marks API acceptance. Application recovery requires $N=3$ consecutive healthy probes post-restart. Interrupted or timed-out trials are reported as UNRECOVERED.
7. **Calibrated Cloud Defaults:** Defaults to 120s duration, 2.0s observation interval, 500ms latency trigger, 60s restart cooldown, and 5.0s probe timeouts to accommodate internet RTT and container reload cycles.

### 9.2. Pre-Flight Dry-Run Execution (Non-Mutating)
The `--dry-run` flag performs non-destructive health and metrics checks against the live environment, queries environment status via read-only AWS APIs, and prints the planned configuration without calling restart APIs or injecting faults:
```powershell
python run_live_aws.py `
    --base-url "http://progress-gated-env.eba-xyz.us-east-1.elasticbeanstalk.com" `
    --environment-name "progress-gated-env" `
    --policy progress_gated `
    --scenario transient_latency `
    --dry-run
```

### 9.3. Authorizing and Running Live Experiments
Once dry-run passes, execute live trials following the staged validation protocol:

```powershell
# 1. Phase 2: Transient Latency Validation (Mechanically Enforced No-Restart Mode)
# Ensures zero AWS restart API calls can be made, even if a policy requests restart
python run_live_aws.py `
    --base-url "http://progress-gated-env.eba-xyz.us-east-1.elasticbeanstalk.com" `
    --environment-name "progress-gated-env" `
    --policy progress_gated `
    --scenario transient_latency `
    --duration 60.0 `
    --prohibit-restarts

# 2. Phase 3: Stalled Progress Restart Test (Explicit opt-in and authenticated token required)
$env:FAULT_INJECTION_TOKEN = "your-live-cryptographic-token"
python run_live_aws.py `
    --base-url "http://progress-gated-env.eba-xyz.us-east-1.elasticbeanstalk.com" `
    --environment-name "progress-gated-env" `
    --policy progress_gated `
    --scenario stalled_progress `
    --duration 120.0 `
    --cooldown 60.0 `
    --allow-restarts `
    --allow-fault-injection

# 3. Emergency Fault Remediation (If an experiment is interrupted or aborted before recovery):
# To immediately clear stalled progress without claiming experiment recovery:
Invoke-RestMethod -Method POST -Uri "http://<ENV_URL>/experiment/fault" `
    -Headers @{"X-Fault-Token"="$env:FAULT_INJECTION_TOKEN"} `
    -ContentType "application/json" `
    -Body '{"fault_state": "normal"}'
```

### 9.4. Spending Limits & Manual Cost Checks
To protect against runaway costs during live testing:
1. **SingleInstance Environment:** Use single-instance environments (`eb create --single`) to eliminate the Application Load Balancer base charge ($16.20/month).
2. **AWS Free Tier / Micro Instances:** Use `t4g.micro` or `t3.micro` instances (~$0.0104/hour on-demand).
3. **AWS Budgets Alert:** Set a hard budget alert at **$5.00 USD** in the AWS Billing Console.
4. **Execution Time Limit:** Restrict total live testing to a 30-minute window (cost: <$0.02 USD).

### 9.5. Complete Resource Teardown & Cleanup
Immediately after testing, terminate all cloud resources to ensure zero ongoing charges:
```powershell
# 1. Terminate the Elastic Beanstalk environment and release EC2 instance and Elastic IP:
aws elasticbeanstalk terminate-environment --environment-name "progress-gated-env" --terminate-resources

# 2. Delete the application definition:
aws elasticbeanstalk delete-application --application-name "progress-gated-app"

# 3. Verify zero EC2 instances remain running:
aws ec2 describe-instances `
    --filters "Name=tag:elasticbeanstalk:environment-name,Values=progress-gated-env" "Name=instance-state-name,Values=running" `
    --query "Reservations[*].Instances[*].InstanceId"
```
