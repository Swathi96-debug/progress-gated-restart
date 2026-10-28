# Progress-Gated Application-Server Restart Under Transient Load

> **Operating Systems Case Study (BTech 3rd Year - Computer Science & Engineering)**
> **GitHub Repository:** [https://github.com/Swathi96-debug/progress-gated-restart](https://github.com/Swathi96-debug/progress-gated-restart)
> **Deployment Target:** AWS Elastic Beanstalk (Single-Instance Python Platform on Amazon Linux 2023)

---

## 1. What This Project Is About

When running web applications in cloud environments, services occasionally experience slow responses. This can happen because of a sudden spike in user requests, heavy background tasks, or database delays.

In real-world operations, systems often use automated health checks. A typical automated recovery rule looks like this: *if response latency exceeds a threshold (for example, 150 ms) or if a probe times out, restart the application server.*

The problem with this traditional **Fixed-Threshold** reboot policy is that restarting a server has a high cost. A restart drops in-flight connections, wipes out in-memory caches, and forces the runtime to re-initialize. If the server was simply working through a temporary traffic spike and would have caught up on its own a second later, restarting it actually makes the problem worse. In severe cases, rebooting under load triggers a reboot storm across multiple instances.

However, if worker threads are truly deadlocked or stuck in an unrecoverable state, the server will never fix itself. In that case, doing nothing is equally bad, and a reboot is necessary.

In this case study, we implemented and evaluated an alternative policy called **Progress-Gated Restart**. Instead of checking only whether requests are taking longer than a threshold, the policy also monitors whether the application is still making forward progress by tracking an atomic completion counter ($\Delta C$).

- If requests are slow but work is still getting finished ($\Delta C > 0$), restarts are held back to allow natural recovery.
- If requests are slow and zero work is completing over an observation window ($\Delta C = 0$), or if a hard deadline expires, a restart is triggered.

---

## 2. The Three Restart Policies

All three policies share the same configuration parameters (defined in `PolicyConfig`) to ensure tests are strictly comparable:
- **Latency Threshold ($T$):** $150\text{ ms}$ for local simulations (calibrated to $500\text{ ms}$ for live AWS trials to account for network transit).
- **Observation Window:** $0.3\text{ s}$ (or $0.4\text{ s}$ in CLI single trials; $2.0\text{ s}$ on AWS).
- **Hard Deadline:** $1.5\text{ s}$ locally (or $1.6\text{ s}$ in CLI; $60.0\text{ s}$ on AWS) as an upper bound on sustained degradation.
- **Cooldown Window:** $1.2\text{ s}$ locally (or $1.8\text{ s}$ in CLI; $60.0\text{ s}$ on AWS) to prevent repeated immediate reboots.

### Policy Details

1. **Control Policy (`NoRestartPolicy`)**
   Does not trigger any restarts. It acts as our baseline to observe how the application handles load naturally and to show what happens when a permanent deadlock occurs without any recovery mechanism.

2. **Fixed-Threshold Policy (`FixedThresholdPolicy`)**
   Reboots the application immediately as soon as observed request latency exceeds the threshold $T$ (or if a probe fails), subject to cooldown checks. It does not check whether work is still getting done.

3. **Progress-Gated Policy (`ProgressGatedPolicy`)**
   When observed latency crosses $T$, it enters an observation window ($0.3\text{ s}$ in the batch benchmark). During this window, it checks the change in completed requests:
   $$\Delta C = C_t - C_{t-1}$$
   - **If $\Delta C > 0$:** Work is still completing. The restart is withheld because the server is actively processing its queue and may recover on its own.
   - **If $\Delta C == 0$:** Zero work completed during the observation window. The policy confirms a stall and triggers a restart.
   - **Hard Deadline:** If latency stays degraded past the hard deadline ($1.5\text{ s}$ locally), it restarts anyway as a fail-safe, even if minor progress is occurring.

### Edge Case Handling in Code
- **Counter Resets & Reboots:** When a server restarts, `completion_counter` drops back to 0 and the process ID (`pid`) changes. `ProgressGatedPolicy` detects process changes and counter drops, immediately establishing a new baseline instead of misinterpreting the drop as negative progress or a stall.
- **Probe Failures:** If a health or metrics probe fails due to a temporary network timeout, the policy does not treat missing data as confirmed zero progress ($\Delta C = 0$). It withholds a restart until it can confirm a true stall.
- **Duplicate Restart Prevention:** `RestartController` uses a `threading.Lock` so concurrent worker threads cannot trigger overlapping restarts while one is already in progress.

---

## 3. Workload and Failure Scenarios

We tested all three policies across four workload and failure scenarios (implemented in `experiment/workload.py`):

1. **Transient Latency Spike (`transient_latency`)**
   From offset $t = 1.0\text{ s}$ to $t = 2.5\text{ s}$ ($1.5\text{ s}$ duration), request delay temporarily jumps from baseline $20\text{ ms}$ to $250\text{ ms}$ (exceeding the $150\text{ ms}$ threshold). All requests continue to succeed with HTTP 200, and forward progress continues ($\Delta C > 0$).
   - *Goal:* Verify whether a policy can distinguish a temporary slowdown from a real crash, avoiding false-positive reboots.

2. **Stalled Progress (`stalled_progress`)**
   At offset $t = 1.0\text{ s}$, a fault is injected that causes simulated worker threads to block on a synchronization event (`threading.Event`). Completed work stops completely ($\Delta C == 0$), while `/metrics` and `/health` endpoints remain responsive.
   - *Goal:* Verify whether a policy correctly detects the lack of progress and initiates a recovery reboot.

3. **Flapping Overload (`flapping_overload`)**
   The application enters an intermittent load-shedding state where requests alternate between HTTP 503 rejections and successful HTTP 200 responses.
   - *Goal:* Observe how policies handle bursty, intermittent load-shedding where partial progress is made.

4. **Transient Overload (`transient_overload`)**
   At offset $t = 1.0\text{ s}$, the server enters a temporary overload state for $1.5\text{ s}$, returning HTTP 503 (`Retry-After`) to shed load before automatically recovering back to normal.
   - *Goal:* Evaluate behavior when requests fail due to deliberate queue rejection rather than thread deadlocks.

---

## 4. How the System is Built

The project has three main components:

```
progress-gated-restart/
├── application.py          # Flask backend, in-memory state, and telemetry endpoints
├── requirements.txt        # Python dependencies (Flask, Gunicorn, Boto3, Pytest)
├── Procfile                # Elastic Beanstalk entry point (single-worker Gunicorn)
├── simulate_local.py       # CLI tool for local simulation runs and batch benchmarks
├── run_live_aws.py         # CLI tool for running tests against an AWS deployment
├── .ebignore               # AWS deployment bundle ignore rules
├── .gitignore              # Git ignore rules for virtual environments, caches, and logs
│
├── experiment/             # Experiment framework and policy logic
│   ├── models.py           # Dataclasses (PolicyConfig, PolicyDecision, etc.)
│   ├── policies.py         # Implementations of the three restart policies
│   ├── controller.py       # RestartController, guarded locks, and polling loops
│   ├── adapters.py         # SimulatedRestartAdapter and ElasticBeanstalkRestartAdapter
│   ├── workload.py         # Workload generator and fault injection schedules
│   ├── runner.py           # ExperimentRunner orchestrator
│   ├── analysis.py         # Log parser and summary metrics calculator
│   ├── batch.py            # Batch execution orchestrator for multi-trial experiments
│   └── data/               # Canonical bundled benchmark CSV datasets
│       ├── batch_aggregated_20261027_215526.csv
│       └── batch_summary_20261027_215526.csv
│
├── templates/
│   └── dashboard.html      # Interactive web dashboard
├── static/
│   ├── css/dashboard.css   # Dashboard styles
│   ├── js/dashboard.js     # Live telemetry charting and client logic
│   ├── favicon.svg         # Vector favicon matching cyan brand palette
│   └── favicon.ico         # Standard 32x32 browser favicon
│
└── tests/                  # 92 automated regression and unit tests
    ├── test_application.py
    ├── test_dashboard_ui.py
    ├── test_policies.py
    ├── test_workload_and_analysis.py
    └── test_live_aws_runner.py
```

### Key Technical Details

- **Atomic Completion Counter:** In `application.py`, the `ApplicationState` class maintains `completion_counter` using Python's `threading.Lock`. Every successful request through `GET /work` atomically increments this integer.
- **Restart Mechanics & Adapter Segregation:** Restart operations are decoupled using an adapter interface so we never accidentally invoke cloud APIs during development:
  - `SimulatedRestartAdapter`: Used for local test suites, dashboard simulations, and the 30-trial benchmark batch. It simulates reboot delay (~50–80 ms) and executes an in-memory state reset callback. Real AWS restarts are strictly **0**.
  - `ElasticBeanstalkRestartAdapter`: Used only when explicitly testing against live AWS infrastructure via `run_live_aws.py`. It invokes `boto3.client('elasticbeanstalk').restart_app_server()`.
- **Vanilla Web Dashboard:** Accessible at `GET /dashboard`. It uses plain HTML5, vanilla CSS, and the native HTML5 Canvas API for live telemetry streaming (zero external charting libraries or heavy CDN dependencies). It also provides an interactive work probe tester (`GET /work?delay_ms=N`) and CSV export buttons.

---

## 5. Local Setup and Usage

### Prerequisites
- Python 3.10, 3.11, or 3.12
- Windows PowerShell, macOS terminal, or Linux bash

### 1. Clone the Repository and Set Up Virtual Environment

```powershell
# Navigate into the project folder (or your cloned path)
cd progress-gated-restart

# Create a virtual environment
python -m venv .venv

# Activate it (Windows PowerShell)
.\.venv\Scripts\Activate.ps1

# (On Linux / macOS, use: source .venv/bin/activate)

# Install dependencies
pip install --upgrade pip
pip install -r requirements.txt
```

### 2. Run the Test Suite

We have 92 automated unit and integration tests covering the Flask routes, policy logic, cooldown timing, counter reset handling, and dashboard client behavior:

```powershell
pytest -v
```

All 92 tests should pass in under 10 seconds.

### 3. Start the Local Server and Dashboard

```powershell
python application.py
```

Open your browser and navigate to:
**`http://127.0.0.1:5000/dashboard`**

From the dashboard you can:
- Watch live node telemetry and latency metrics.
- Send test work requests using the probe tool (`GET /work?delay_ms=40`).
- Run interactive simulated benchmark trials across policies and scenarios.
- View and download historical benchmark CSV files.

### 4. Running CLI Simulations

You can also run benchmarks directly from the command line using `simulate_local.py`:

```powershell
# Run a single trial comparing policies under transient latency
python simulate_local.py --policy control --scenario transient_latency --duration 3.5
python simulate_local.py --policy fixed_threshold --scenario transient_latency --duration 3.5
python simulate_local.py --policy progress_gated --scenario transient_latency --duration 3.5

# Run a single trial under a stalled progress deadlock
python simulate_local.py --policy progress_gated --scenario stalled_progress --duration 3.5

# Run a full 30-trial benchmark batch (5 trials x 3 policies x 2 scenarios)
python simulate_local.py --batch --trials 5 --duration 3.5
```

---

## 6. Experimental Results & Analysis

We ran a controlled 30-trial benchmark batch using `SimulatedRestartAdapter` (Batch ID: `20261027_215526`). Each configuration was repeated 5 times with matched request schedules to ensure fair comparison.

The canonical raw data is stored in [`experiment/data/batch_aggregated_20261027_215526.csv`](experiment/data/batch_aggregated_20261027_215526.csv) and [`experiment/data/batch_summary_20261027_215526.csv`](experiment/data/batch_summary_20261027_215526.csv).

### Summary Table

| Scenario | Policy | Trials | Recovery Rate | Success Rate (Mean) | Restarts (Mean ± Std) | Recovery Time (Mean ± Std) | Estimated Downtime |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **Transient Latency** | Control | 5 | 5 / 5 (100%) | 100.0% | 0.0 ± 0.0 | 1.58s ± 0.16s | 0.0s |
| | **Fixed-Threshold** | 5 | 5 / 5 (100%) | 100.0% | **1.0 ± 0.0** | 1.70s ± 0.00s | 0.0s |
| | **Progress-Gated** | 5 | 5 / 5 (100%) | 100.0% | **0.0 ± 0.0** | 1.70s ± 0.00s | 0.0s |
| **Stalled Progress** | Control | 5 | 0 / 5 (0%) | 28.6% | 0.0 ± 0.0 | *Unrecovered* | 2.39s ± 0.01s |
| | **Fixed-Threshold** | 5 | 5 / 5 (100%) | 100.0% | 1.0 ± 0.0 | **0.79s ± 0.01s** | 0.0s |
| | **Progress-Gated** | 5 | 5 / 5 (100%) | 100.0% | 1.0 ± 0.0 | **1.10s ± 0.00s** | 0.0s |

### Metric Definitions & Measurement Methodology

- **Recovery Time:** Measured from the initial fault onset ($t = 1.0\text{ s}$) until the application server responds to 2 consecutive healthy, low-latency probes ($< 80\text{ ms}$). If a trial never recovered before the $3.5\text{ s}$ limit, it is recorded as *Unrecovered* (`N/A`).
- **Estimated Downtime (Request-Inferred):** Elapsed duration between the first failed client request and the first subsequent successful request. For transient latency, downtime was $0.0\text{ s}$ because work continued completing with HTTP 200. For stalled progress under Control, downtime was $2.39\text{ s}$ because threads stayed blocked until trial cutoff.
- **Restarts Count:** Monitored via `SimulatedRestartAdapter`. Every trial in this benchmark was run in simulated mode, so real AWS Elastic Beanstalk restarts were strictly **0**.

### What the Numbers Show

1. **Under Transient Latency (Temporary Slowdown):**
   - The **Control** policy shows that the system naturally recovers in about $1.58\text{ s}$ without any restarts.
   - The **Fixed-Threshold** policy triggers an unnecessary restart in 100% of the trials ($1.0 \pm 0.0$). This is a false-positive restart: the server was healthy and would have caught up, but was rebooted anyway simply because latency went above $150\text{ ms}$.
   - The **Progress-Gated** policy observes that work is still getting finished ($\Delta C > 0$) and holds back the restart ($0.0 \pm 0.0$ restarts), letting the server recover naturally.

2. **Under Stalled Progress (Deadlock):**
   - The **Control** policy completely fails to recover ($0\%$ recovery rate, only $28.6\%$ of requests completed before the hang), proving that automated intervention is required.
   - Both **Fixed-Threshold** and **Progress-Gated** policies successfully detect the problem, trigger a restart, and achieve a $100\%$ success rate.
   - **Trade-off:** Fixed-Threshold recovers faster ($0.79\text{ s}$ vs. $1.10\text{ s}$) because it triggers immediately. Progress-Gated takes slightly longer ($+0.31\text{ s}$) because it deliberately spends an observation window checking if the completion counter is incrementing before confirming that the worker is truly stuck.

---

## 7. HTTP API Reference

The Flask application exposes the following endpoints:

| Method & Route | Purpose | Description |
| :--- | :--- | :--- |
| `GET /` | Root Directory | Returns JSON metadata with project description, endpoints, and links. Redirects browser requests to `/dashboard`. |
| `GET /dashboard` | Web Dashboard | Returns the HTML5 research dashboard with live metrics, charts, and trial controls. |
| `GET /favicon.ico` | Favicon | Serves the standard 32x32 browser favicon from `static/favicon.ico`. |
| `GET /health` | Health Check | Returns HTTP 200 `{"status": "healthy"}` during normal operation, overload, and stalls. Returns HTTP 500 during simulated hard crashes. |
| `GET /metrics` | Telemetry Snapshot | Returns JSON containing current PID, uptime, `completion_counter`, completed/failed request totals, and current fault state. Non-blocking. |
| `GET /work?delay_ms=N` | Simulated Work | Simulates computational work with a configurable delay ($0$ to $30000\text{ ms}$). Increments `completion_counter` only on success. |
| `POST /experiment/fault` | Fault Injection | Sets the server fault state (`normal`, `temporary_overload`, `simulated_stalled_progress`, `hard_failure`). Requires `EXPERIMENT_MODE=true` and `X-Fault-Token` header. |
| `GET /api/results/aggregated` | Benchmark Stats | Returns aggregated statistics across policies and scenarios from the canonical benchmark dataset. |
| `GET /api/results/summary` | Benchmark Trials | Returns detailed per-trial records from the canonical benchmark dataset. |
| `GET /api/results/export?type=X` | CSV Download | Serves raw downloadable CSV files (`type=aggregated` or `type=summary`). |
| `POST /api/experiment/run_trial` | Run Local Trial | Executes a safe local simulated experiment trial through the dashboard or API. |

---

## 8. AWS Elastic Beanstalk Deployment Notes

The project was designed to be compatible with AWS Elastic Beanstalk (Python 3.11 / 3.12 on Amazon Linux 2023).

### WSGI Concurrency Model

Our [`Procfile`](Procfile) specifies:
```text
web: gunicorn --workers=1 --threads=8 application:application
```
We intentionally use **1 worker process with 8 threads**. Because `completion_counter` and thread synchronization events reside in Python's in-memory state, running multiple Gunicorn worker processes would fragment the counter across different PIDs. In a future production multi-worker deployment, this counter would be moved to shared memory or an external store like Redis.

### Mechanical Safety Gates in `run_live_aws.py`

When testing against live AWS environments, we added strict safety checks to prevent accidental cloud reboots:
- **`--allow-restarts`**: Must be explicitly supplied on the command line; otherwise, the runner aborts before calling any AWS API.
- **`--prohibit-restarts`**: Intercepts restart requests during validation trials and fails the trial safely if a restart is attempted.
- **`--dry-run`**: Verifies connectivity, queries health, and inspects parameters without mutating AWS infrastructure.
- **Confirmed Recovery Criteria:** An AWS API response of HTTP 200 merely means the restart request was accepted. We only consider the application recovered after $N = 3$ consecutive healthy, low-latency probes post-restart.

### Calibrating Cloud vs. Local Timing Parameters

Timing parameters in `run_live_aws.py` differ from local simulation because operating conditions in the cloud are fundamentally different:
- **Network Latency:** Over `localhost`, network transit takes $< 1\text{ ms}$, so any request taking $> 150\text{ ms}$ indicates server delay. Over the public internet with TLS termination and Nginx, baseline latency can be $50\text{ ms}$ to $100\text{ ms}$. Hence, `--latency-threshold` is set to $500\text{ ms}$ on AWS.
- **Container Reload Time:** Local simulated restarts finish in $\sim 50\text{ ms}$, but AWS Elastic Beanstalk container restarts take between $15\text{ s}$ and $45\text{ s}$. To prevent reboot loops while Gunicorn is already reloading, `--cooldown` is increased from $1.2\text{ s}$ to $60.0\text{ s}$, and the observation window is set to $2.0\text{ s}$.

---

## 9. Honest Limitations & What We Learned

1. **Single-Worker In-Memory State**
   Keeping the completion counter in-process memory is simple, fast, and thread-safe for a single worker. However, it does not scale across multiple Gunicorn workers or multiple EC2 instances behind a load balancer without an external atomic store (such as Redis or POSIX shared memory).

2. **The Observation Delay Trade-off**
   Progress-Gated restart is not strictly "better" in every metric. When a server is genuinely deadlocked, waiting for an observation window ($0.3\text{ s}$ locally, $2.0\text{ s}$ to $5.0\text{ s}$ over the internet) means the reboot happens slightly later than a fixed-threshold policy. The benefit is preventing reboot storms during transient spikes, but the price is a slight delay in detecting true stalls.

3. **Tuning the Observation Window**
   If the observation interval is set too short, it behaves almost identically to a fixed-threshold policy. If set too long, users experience longer wait times during real outages. Choosing this value depends on typical request durations and service SLA requirements.

4. **Simulated vs. Real Restarts**
   Our 30-trial benchmark figures come from `SimulatedRestartAdapter`. While the request latency and workload dispatching are authentic, the actual operating system reboot was simulated in software to allow fast, reproducible batch runs without generating AWS bills.

---

## 10. References & Background Reading

- Candea, G., & Fox, A. (2001). *Crash-Only Software.* Proceedings of the 9th Workshop on Hot Topics in Operating Systems (HotOS IX).
- Candea, G., Kawamoto, S., Fujiki, Y., Friedman, G., & Fox, A. (2004). *Microreboot &mdash; A Technique for Cheap Recovery.* 6th USENIX Symposium on Operating Systems Design and Implementation (OSDI).
- Michael, M. M., & Scott, M. L. (1996). *Simple, Fast, and Practical Non-Blocking and Blocking Concurrent Queue Algorithms.* PODC '96.
- AWS Elastic Beanstalk Documentation: *Restarting App Servers in Elastic Beanstalk Environments*.

---

*Academic project developed for BTech CSE 3rd Year Operating Systems Case Study.*
