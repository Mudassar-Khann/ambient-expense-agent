"""Ambient expense-approval workflow agent using the ADK 2.0 graph API with security controls."""

import base64
import datetime
import json
import os
import re
from zoneinfo import ZoneInfo
from typing import Any

import dotenv
from pydantic import BaseModel, Field

from google.adk.agents import LlmAgent
from google.adk.workflow import Workflow, START, node
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.agents.context import Context
from google.adk.apps import App
from google.genai import types

from app.expense_agent.config import THRESHOLD, MODEL_NAME

# Load environment variables
dotenv.load_dotenv()

# Setup default Google Cloud configuration if available
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")
if os.environ.get("GEMINI_API_KEY"):
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "False")
else:
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")


# --- 1. Define Schemas ---

class ExpenseDetails(BaseModel):
    amount: float = Field(default=0.0, description="The amount spent in USD")
    submitter: str = Field(default="Unknown", description="The person who submitted the expense")
    category: str = Field(default="Uncategorized", description="The category of the expense")
    description: str = Field(default="No description", description="Description of the purchase")
    date: str = Field(default="", description="The date of the expense")


class RiskAssessment(BaseModel):
    risk_score: int = Field(description="Risk score from 1 (low risk) to 5 (high risk)")
    risk_factors: list[str] = Field(default_factory=list, description="List of identified risk factors")
    explanation: str = Field(description="Detailed explanation for the risk assessment")
    alert_raised: bool = Field(description="True if an alert is raised due to risk factors")


class WorkflowState(BaseModel):
    expense: ExpenseDetails | None = None
    risk_assessment: RiskAssessment | None = None
    human_decision: str | None = None
    status: str = "Pending"
    redacted_pii: list[str] = Field(default_factory=list)
    security_event: bool = False
    subscription: str | None = None


# --- 2. Security Regex and Trigger Keywords ---

SSN_REGEX = re.compile(r'\b\d{3}-\d{2}-\d{4}\b')
CC_REGEX = re.compile(r'\b\d{4}[- ]?\d{6}[- ]?\d{5}\b|\b(?:\d{4}[- ]?){3}\d{4}\b|\b\d{13,16}\b')

INJECTION_KEYWORDS = [
    "ignore previous", "ignore above", "ignore instructions",
    "forget previous", "forget instructions", "system prompt",
    "override rules", "bypass rules", "bypass verification",
    "force approval", "force approve", "auto-approve this",
    "you are now", "instead of the above", "new instruction"
]


def detect_prompt_injection(text: str) -> bool:
    """Scans text for common prompt injection patterns."""
    lowercased = text.lower()
    return any(kw in lowercased for kw in INJECTION_KEYWORDS)


# --- 3. Define Graph Nodes ---

def parse_event_node(ctx: Context, node_input: Any) -> ExpenseDetails:
    """Parses incoming JSON events and extracts standard expense details."""
    raw_str = ""
    event_dict = {}

    if hasattr(node_input, "parts") and node_input.parts:
        raw_str = node_input.parts[0].text or ""
    elif isinstance(node_input, str):
        raw_str = node_input
    elif isinstance(node_input, dict):
        event_dict = node_input
    else:
        event_dict = {}

    if raw_str:
        try:
            event_dict = json.loads(raw_str)
        except Exception:
            try:
                decoded = base64.b64decode(raw_str).decode("utf-8")
                event_dict = json.loads(decoded)
            except Exception:
                event_dict = {"description": raw_str}

    data_val = None
    if isinstance(event_dict, dict):
        if "message" in event_dict and isinstance(event_dict["message"], dict) and "data" in event_dict["message"]:
            data_val = event_dict["message"]["data"]
        elif "data" in event_dict:
            data_val = event_dict["data"]
        else:
            data_val = event_dict
    else:
        data_val = event_dict

    if isinstance(data_val, str):
        try:
            decoded_b64 = base64.b64decode(data_val).decode("utf-8")
            data_val = json.loads(decoded_b64)
        except Exception:
            try:
                data_val = json.loads(data_val)
            except Exception:
                pass

    if not isinstance(data_val, dict):
        data_val = {}

    amount_val = data_val.get("amount")
    if isinstance(amount_val, str):
        try:
            amount_val = float(amount_val.replace("$", "").replace(",", "").strip())
        except Exception:
            amount_val = 0.0
    elif isinstance(amount_val, (int, float)):
        amount_val = float(amount_val)
    else:
        amount_val = 0.0

    submitter = str(data_val.get("submitter", "Unknown"))
    category = str(data_val.get("category", "Uncategorized"))
    description = str(data_val.get("description", "No description"))
    date = str(data_val.get("date", ""))

    return ExpenseDetails(
        amount=amount_val,
        submitter=submitter,
        category=category,
        description=description,
        date=date,
    )


def security_checkpoint(ctx: Context, node_input: ExpenseDetails) -> Event:
    """Performs PII redaction and checks for prompt injection.
    
    Routes flagged expenses directly to human, bypassing the LLM.
    """
    description = node_input.description
    redacted = []

    # 1. Redact PII (SSN and Credit Cards)
    scrubbed_desc = description
    if SSN_REGEX.search(scrubbed_desc):
        scrubbed_desc = SSN_REGEX.sub("[REDACTED SSN]", scrubbed_desc)
        redacted.append("SSN")
    if CC_REGEX.search(scrubbed_desc):
        scrubbed_desc = CC_REGEX.sub("[REDACTED CREDIT CARD]", scrubbed_desc)
        redacted.append("Credit Card")

    # Update node description to be clean
    node_input.description = scrubbed_desc
    expense_dict = node_input.model_dump()

    # 2. Check for prompt injection
    has_injection = detect_prompt_injection(description)

    if has_injection:
        return Event(
            output=node_input,
            route="flagged",
            state={
                "expense": expense_dict,
                "redacted_pii": redacted,
                "security_event": True,
                "status": "Security Check Failed"
            }
        )

    return Event(
        output=node_input,
        route="clean",
        state={
            "expense": expense_dict,
            "redacted_pii": redacted
        }
    )


def check_threshold(ctx: Context, node_input: ExpenseDetails) -> Event:
    """Applies the routing rule based on the config threshold."""
    expense_dict = node_input.model_dump()
    if node_input.amount >= THRESHOLD:
        return Event(
            output=node_input,
            route="review",
            state={"expense": expense_dict, "status": "Review Required"}
        )
    return Event(
        output=node_input,
        route="auto_approve",
        state={"expense": expense_dict, "status": "Processing Auto-Approval"}
    )


def auto_approve_node(ctx: Context, node_input: ExpenseDetails) -> dict:
    """Instantly approves expenses below the threshold without calling the LLM."""
    redacted_info = ctx.state.get("redacted_pii", [])
    return {
        "status": "Auto-Approved",
        "expense": node_input.model_dump(),
        "risk": None,
        "security_event": False,
        "redacted_pii": redacted_info
    }


def format_review_prompt(ctx: Context, node_input: ExpenseDetails) -> str:
    """Formats the instruction for the LLM reviewer using the parsed details."""
    return (
        f"Please analyze the following expense details for potential risks (such as unusual categories, "
        f"vague descriptions, suspicious submitters, or excessive amounts):\n\n"
        f"Submitter: {node_input.submitter}\n"
        f"Amount: ${node_input.amount:.2f}\n"
        f"Category: {node_input.category}\n"
        f"Description: {node_input.description}\n"
        f"Date: {node_input.date}\n"
    )


# LLM node to analyze risks
risk_reviewer = LlmAgent(
    name="risk_reviewer",
    model=MODEL_NAME,
    instruction=(
        "You are an expert risk auditor. Analyze the provided expense details and return a structured risk "
        "assessment indicating any potential policy violations or suspicious activity. "
        "Set alert_raised to True if the risk score is 3 or above."
    ),
    output_schema=RiskAssessment,
    output_key="risk_assessment",
)


@node(rerun_on_resume=True)
async def human_approval_node(ctx: Context, node_input: Any):
    """HITL node that handles both normal LLM review and security flagged cases."""
    expense_data = ctx.state.get("expense")
    expense = ExpenseDetails(**expense_data) if expense_data else ExpenseDetails()
    redacted_info = ctx.state.get("redacted_pii", [])
    security_event = ctx.state.get("security_event", False)

    redacted_msg = f" (Redacted: {', '.join(redacted_info)})" if redacted_info else ""

    if security_event:
        # Prompt Injection flagged warning
        msg = (
            f"🚨 SECURITY ALERT: Expense of ${expense.amount:.2f} submitted by {expense.submitter} "
            f"has been flagged for potential prompt injection!\n"
            f"Description: {expense.description}{redacted_msg}\n\n"
            f"--- Security Check Status ---\n"
            f"Prompt Injection: DETECTED\n"
            f"PII Redacted: {', '.join(redacted_info) if redacted_info else 'None'}\n\n"
            f"WARNING: The LLM reviewer was bypassed to protect prompt integrity.\n"
            f"Do you want to override and approve or reject this expense? (approve/reject)"
        )
    else:
        # Normal path with LLM risk assessment
        risk = node_input
        if not isinstance(risk, RiskAssessment):
            risk_data = ctx.state.get("risk_assessment")
            risk = RiskAssessment(**risk_data) if risk_data else RiskAssessment(
                risk_score=1, risk_factors=[], explanation="No LLM report available", alert_raised=False
            )

        msg = (
            f"⚠️ ALERT: Expense of ${expense.amount:.2f} submitted by {expense.submitter} requires human approval.\n"
            f"Description: {expense.description}{redacted_msg}\n\n"
            f"--- LLM Risk Judgment ---\n"
            f"Risk Score: {risk.risk_score}/5\n"
            f"Alert Raised: {'YES' if risk.alert_raised else 'NO'}\n"
            f"Risk Factors: {', '.join(risk.risk_factors) if risk.risk_factors else 'None'}\n"
            f"Explanation: {risk.explanation}\n\n"
            f"Do you approve or reject this expense? (approve/reject)"
        )

    if not ctx.resume_inputs:
        yield RequestInput(
            interrupt_id="approval_decision",
            message=msg,
        )
        return

    # Process human decision
    response_data = ctx.resume_inputs.get("approval_decision", "")
    if isinstance(response_data, dict):
        response = response_data.get("response") or response_data.get("approval_decision") or ""
        if not response and response_data:
            response = list(response_data.values())[0]
    else:
        response = response_data

    if isinstance(response, str):
        response_str = response.strip().lower()
    else:
        response_str = str(response).strip().lower()

    decision = "Approved" if response_str in ("yes", "y", "approve", "approved") else "Rejected"

    if security_event:
        risk_dict = {
            "risk_score": 5,
            "risk_factors": ["Prompt Injection Attempt"],
            "explanation": "Flagged by security checkpoint and bypassed LLM",
            "alert_raised": True
        }
    else:
        if isinstance(node_input, RiskAssessment):
            risk_dict = node_input.model_dump()
        else:
            risk_data = ctx.state.get("risk_assessment")
            risk_dict = risk_data if risk_data else {}

    yield Event(
        output={
            "status": decision,
            "expense": expense.model_dump(),
            "risk": risk_dict,
            "security_event": security_event,
            "redacted_pii": redacted_info
        },
        state={
            "human_decision": decision,
            "status": decision,
        }
    )


def record_outcome(ctx: Context, node_input: dict) -> Event:
    """Records the outcome to a local audit file and yields a formatted response."""
    status = node_input.get("status", "Unknown")
    expense = node_input.get("expense", {})
    risk = node_input.get("risk")
    security_event = node_input.get("security_event", False)
    redacted_pii = node_input.get("redacted_pii", [])
    subscription = ctx.state.get("subscription")

    record = {
        "timestamp": datetime.datetime.now(ZoneInfo("UTC")).isoformat(),
        "session_id": ctx.session.id if ctx.session else "unknown",
        "status": status,
        "subscription": subscription,
        "expense": expense,
        "risk": risk,
        "security_event": security_event,
        "redacted_pii": redacted_pii
    }

    try:
        with open("expense_outcomes.jsonl", "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass

    outcome_msg = (
        f"Expense Processed!\n"
        f"Status: {status}\n"
        f"Submitter: {expense.get('submitter')}\n"
        f"Amount: ${expense.get('amount', 0.0):.2f}\n"
        f"Description: {expense.get('description')}\n"
    )
    if redacted_pii:
        outcome_msg += f"Security Note: Redacted {', '.join(redacted_pii)}\n"
    if security_event:
        outcome_msg += f"Security Alert: Prompt Injection Detected and Blocked!\n"
    if risk:
        outcome_msg += (
            f"Risk Score: {risk.get('risk_score')}/5\n"
            f"Risk Factors: {', '.join(risk.get('risk_factors', []))}\n"
        )

    return Event(
        output=outcome_msg,
        content=types.Content(role="model", parts=[types.Part.from_text(text=outcome_msg)])
    )


# --- 4. Wire Up Workflow ---

root_agent = Workflow(
    name="ambient_expense_agent",
    edges=[
        (START, parse_event_node),
        (parse_event_node, security_checkpoint),
        (security_checkpoint, {
            "flagged": human_approval_node,
            "clean": check_threshold
        }),
        (check_threshold, {
            "auto_approve": auto_approve_node,
            "review": format_review_prompt
        }),
        (format_review_prompt, risk_reviewer),
        (risk_reviewer, human_approval_node),
        (auto_approve_node, record_outcome),
        (human_approval_node, record_outcome),
    ],
    state_schema=WorkflowState,
)

# App container setup
app = App(
    name="app",
    root_agent=root_agent,
)
