# Ambient Expense Approval Agent

An event-driven, security-aware AI Agent that processes expense approvals asynchronously. It runs as a local FastAPI web service that accepts Pub/Sub push trigger events, processes risk analysis using Gemini, and maintains human-in-the-loop (HITL) approval states.

## Key Features

1. **Ambient Event-Driven Architecture**: Exposes a local web service on port `8080` that accepts Pub/Sub push trigger messages.
2. **Subscription Path Normalization**: Automatically normalizes fully-qualified Pub/Sub subscription paths (e.g. `projects/my-project/subscriptions/expense-approval-subscription`) into short, clean user session IDs (e.g. `expense-approval-subscription`) for clean data storage and visual layout.
3. **Advanced Security Controls**:
   - **PII Redaction**: Pre-processes expense descriptions to redact Credit Cards (`[REDACTED CREDIT CARD]`) and Social Security Numbers (`[REDACTED SSN]`) before passing them to any LLM.
   - **Prompt Injection Defense**: Scans descriptions for injection attempts (e.g. `"bypass rules"`, `"override rules"`, `"auto-approve"`). If detected, the LLM reviewer is bypassed completely to protect prompt integrity, raising a security flag and routing the request directly to a human reviewer with a security alert.
4. **Amount-Based Routing Logic**:
   - Expenses under **$100** are automatically approved (unless a security alert/prompt injection is flagged).
   - Expenses **$100 or more** are escalated for human-in-the-loop (HITL) review.
5. **No Cloud Dependencies**: Operates entirely locally. Telemetry is configured offline (`otel_to_cloud=False`), logs write to the standard console, and session state is stored in a local SQLite database (`app/.adk/session.db`).

---

## Project Structure

```
ambient-expense-agent/
├── app/
│   ├── app_utils/
│   │   ├── telemetry.py        # Local telemetry configuration (otel_to_cloud=False)
│   │   └── typing.py           # Custom Pydantic models for Pub/Sub payloads
│   ├── expense_agent/
│   │   ├── __init__.py
│   │   ├── agent.py            # Main Agent logic, Prompt Injection & PII Redaction
│   │   └── config.py           # Agent configuration
│   ├── __init__.py
│   ├── agent.py                # Graph API / ADK 2.0 Workflow definitions
│   └── fast_api_app.py         # Local FastAPI event-driven endpoint (Port 8080)
├── Dockerfile                  # Containerization definition
├── Makefile                    # Target automation commands
├── pyproject.toml              # Dependencies & packages configuration
└── README.md                   # Project documentation
```

---

## Getting Started

### Prerequisites
- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (Astral's fast Python package manager)

### Installation
Install project dependencies and the `google-agents-cli` tool:
```bash
make install
```

---

## How to Run

### 1. Configure API Key
Create a `.env` file in the root directory:
```env
GEMINI_API_KEY=your_gemini_api_key_here
```

### 2. Run the Local Web Service
Start the local FastAPI web service on port `8080`:
```bash
make run-web
```

### 3. Send a Pub/Sub Push Trigger
While `run-web` is running, send a Pub/Sub mock push payload using `curl` or PowerShell.

#### PowerShell Example:
```powershell
$body = @{
    message = @{
        data = "eyJhbW91bnQiOiAxNTAuMCwgInN1Ym1pdHRlciI6ICJhbGljZUBjb21wYW55LmNvbSIsICJjYXRlZ29yeSI6ICJzb2Z0d2FyZSIsICJkZXNjcmlwdGlvbiI6ICJJREUgTGljZW5zZSIsICJkYXRlIjogIjIwMjYtMDYtMDYifQ=="
    }
    subscription = "projects/my-project/subscriptions/expense-approval-subscription"
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://127.0.0.1:8080/" -Method Post -Body $body -ContentType "application/json"
```

#### Bash/cURL Example:
```bash
curl -X POST http://127.0.0.1:8080/ \
  -H "Content-Type: application/json" \
  -d '{
    "message": {
      "data": "eyJhbW91bnQiOiAxNTAuMCwgInN1Ym1pdHRlciI6ICJhbGljZUBjb21wYW55LmNvbSIsICJjYXRlZ29yeSI6ICJzb2Z0d2FyZSIsICJkZXNjcmlwdGlvbiI6ICJJREUgTGljZW5zZSIsICJkYXRlIjogIjIwMjYtMDYtMDYifQ=="
    },
    "subscription": "projects/my-project/subscriptions/expense-approval-subscription"
  }'
```

### 4. Interactive Testing via Playground
You can also run the interactive visual playground UI locally:
```bash
make playground
```
Once started, open [http://127.0.0.1:8080/dev-ui/?app=app](http://127.0.0.1:8080/dev-ui/?app=app) in your browser.
