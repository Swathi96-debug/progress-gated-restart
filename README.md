# Progress-Gated Application-Server Restart Under Transient Load

**Operating Systems Case Study**

**Deployment:** AWS Elastic Beanstalk

## 1. About the Project

Sometimes a web server becomes slow when too many requests come in. Restarting it right away may not help if the problem is only temporary. But if the server stops doing its work, restarting it may be needed.

In this project, we tested three ways to decide when a server should restart. Our main idea is to check whether the server is still finishing requests before restarting it.

## 2. Restart Policies

We tested three policies:

- **Control:** Never restarts the server.
- **Fixed-Threshold:** Restarts the server when its response time is too high.
- **Progress-Gated:** Checks whether the server is still finishing requests before restarting it.

## 3. Test Scenarios

We tested the policies in four situations:

- **Transient Latency:** The server becomes slow for a short time.
- **Stalled Progress:** The server stops finishing requests.
- **Flapping Overload:** The server sometimes accepts requests and sometimes rejects them.
- **Transient Overload:** The server rejects requests for a short time and then recovers.

## 4. Technologies Used

- Python and Flask
- HTML, CSS and JavaScript
- AWS Elastic Beanstalk
- Pytest
- CSV files for experiment results

`application.py` runs the web application. `simulate_local.py` runs local tests. The `experiment/` folder contains the restart policies and testing code.

## 5. Results

We ran 30 tests using simulated restarts. Each policy was tested five times in each of two situations.

| Scenario | Policy | Average Restarts | Recovery Rate |
|---|---|---:|---:|
| Temporary Slowdown | Control | 0 | 100% |
| Temporary Slowdown | Fixed-Threshold | 1 | 100% |
| Temporary Slowdown | Progress-Gated | 0 | 100% |
| Stalled Progress | Control | 0 | 0% |
| Stalled Progress | Fixed-Threshold | 1 | 100% |
| Stalled Progress | Progress-Gated | 1 | 100% |

The results show that Fixed-Threshold restarted the server even when it was only slow for a short time. Progress-Gated avoided these unnecessary restarts.

When the server stopped finishing requests, both restart policies helped it recover. Progress-Gated took a little longer because it checked for a real problem before restarting.

**Note:** These tests used simulated restarts. They did not restart real AWS servers.

## 6. How to Run

Create a virtual environment and install the required packages:

```bash
python -m venv .venv
```

On Windows, activate it and install the packages:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Run the tests:

```bash
pytest -v
```

Start the application:

```bash
python application.py
```

Open `http://127.0.0.1:5000/dashboard` in your browser.

## 7. AWS Deployment and Limitations

The application is deployed on AWS Elastic Beanstalk. Its dashboard shows application details and lets users run simulated tests.

The project stores its data in memory, so it is mainly designed for one application worker. Supporting multiple workers would need shared storage.

The main limitation is that our results come from simulations. More tests are needed to see how the policies work during real cloud failures.

## 8. Main API Endpoints

- `GET /dashboard` – Opens the dashboard.
- `GET /health` – Checks the application's health.
- `GET /metrics` – Shows application metrics.
- `GET /work?delay_ms=N` – Sends a test request.
- `GET /api/results/aggregated` – Shows summary results.
- `GET /api/results/export?type=summary` – Downloads the test results.
