"""FastAPI web service serving the ambient expense workflow on port 8080."""

import os
import json
import logging
from fastapi import FastAPI, Request, Response, status
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.runners import Runner
from google.adk.sessions.sqlite_session_service import SqliteSessionService
from google.genai import types

from app.app_utils.telemetry import setup_telemetry
from app.app_utils.typing import Feedback
from app.expense_agent.agent import root_agent

# 1. Initialize standard Python logging for console logs
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("ambient-expense-agent")

# Configure telemetry
setup_telemetry()

allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None
)

# SQLite session database path (re-using the same one ADK uses)
DB_PATH = os.path.join("app", ".adk", "session.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
session_service = SqliteSessionService(db_path=DB_PATH)

# Initialize ADK Fast API app container
# Set otel_to_cloud=False to disable cloud telemetry export
app: FastAPI = get_fast_api_app(
    agents_dir=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    web=True,
    artifact_service_uri=None,
    allow_origins=allow_origins,
    session_service_uri=None,
    otel_to_cloud=False,
)
app.title = "ambient-expense-agent"
app.description = "API for interacting with the Agent ambient-expense-agent"


@app.post("/")
async def handle_pubsub_push(request: Request):
    """Ambient endpoint that accepts Pub/Sub push trigger messages.
    
    Feeds each event into the workflow session and normalizes subscription names.
    """
    try:
        body = await request.json()
    except Exception:
        logger.error("Failed to parse request JSON body")
        return Response(content="Invalid JSON body", status_code=status.HTTP_400_BAD_REQUEST)

    logger.info(f"Received Pub/Sub push trigger: {json.dumps(body)}")

    # Extract and normalize subscription path to short name
    sub_path = body.get("subscription", "")
    normalized_sub_name = sub_path.split("/")[-1] if sub_path else "default-sub"
    logger.info(f"Normalized subscription name: {normalized_sub_name}")

    # Verify Pub/Sub message structure
    if "message" not in body:
        logger.error("Missing 'message' envelope in Pub/Sub body")
        return Response(content="Missing 'message' envelope", status_code=status.HTTP_400_BAD_REQUEST)

    # Create a new session in SQLite using 'user' as user_id to ensure it's visible in the playground UI,
    # but store the normalized subscription name in the state.
    try:
        session = await session_service.create_session(
            user_id="user",
            app_name="app",
            state={"subscription": normalized_sub_name}
        )
        logger.info(f"Created new workflow session: {session.id} for user: user (subscription: {normalized_sub_name})")
    except Exception as e:
        logger.exception("Failed to create session in SQLite database")
        return Response(content=f"Database Error: {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # Initialize runner and prepare payload message
    runner = Runner(agent=root_agent, session_service=session_service, app_name="app")
    message = types.Content(
        role="user",
        parts=[types.Part.from_text(text=json.dumps(body))]
    )

    events = []
    try:
        # Run until completed or suspended for HITL
        async for event in runner.run_async(
            new_message=message,
            user_id="user",
            session_id=session.id,
        ):
            events.append(event)
    except Exception as e:
        logger.exception("Error executing workflow run")
        return Response(content=f"Workflow Error: {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # Check if the execution suspended waiting for human approval
    is_suspended = any(
        e.content and e.content.parts and 
        any(p.function_call and p.function_call.name == "adk_request_input" for p in e.content.parts)
        for e in events
    )

    if is_suspended:
        logger.info(f"Workflow session {session.id} suspended. Awaiting human approval.")
        return {
            "status": "Awaiting Human Approval",
            "session_id": session.id,
            "user_id": normalized_sub_name,
            "message": "Expense exceeds threshold and is routed for HITL review."
        }

    # Otherwise, it completed (e.g. auto-approved)
    final_output = next((e.output for e in reversed(events) if e.output is not None), "Completed")
    logger.info(f"Workflow session {session.id} completed. Output: {final_output}")
    return {
        "status": "Completed",
        "session_id": session.id,
        "user_id": normalized_sub_name,
        "output": final_output
    }


@app.post("/feedback")
def collect_feedback(feedback: Feedback) -> dict[str, str]:
    logger.info(f"Feedback collected: {feedback.model_dump()}")
    return {"status": "success"}


if __name__ == "__main__":
    import uvicorn
    # Serve on port 8080 as requested
    uvicorn.run(app, host="0.0.0.0", port=8080)
