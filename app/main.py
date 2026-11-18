"""FastAPI service exposing GitHub review and approval endpoints."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.agent import ReviewState, graph

load_dotenv()

app = FastAPI(
    title="Autonomous Code Review & Refactoring Agent",
    version="1.0.0",
    description=(
        "Reviews GitHub pull requests and applies refactors only after approval."
    ),
)


class ReviewDecision(BaseModel):
    """Human decision submitted for a pending pull request review."""

    approved: bool
    feedback: str = Field(default="", max_length=4000)


def _require_review_auth(authorization: str | None) -> None:
    expected_token = os.getenv("REVIEW_API_TOKEN")
    if not expected_token:
        raise HTTPException(
            status_code=503,
            detail="Review API authentication is not configured.",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="A bearer token is required.")
    supplied_token = authorization.removeprefix("Bearer ")
    if not hmac.compare_digest(
        expected_token.encode("utf-8"), supplied_token.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="Invalid bearer token.")


def _verify_webhook_signature(secret: str, body: bytes, signature: str | None) -> None:
    if not signature or not signature.startswith("sha256="):
        raise HTTPException(
            status_code=401, detail="Missing or invalid GitHub signature."
        )
    expected = (
        "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    )
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(
            status_code=401, detail="GitHub signature verification failed."
        )


@app.post("/api/webhooks/github", status_code=202)
async def github_webhook(
    request: Request,
    x_github_event: str | None = Header(default=None),
    x_hub_signature_256: str | None = Header(default=None),
) -> dict[str, str]:
    """Start a review for supported pull_request webhook events."""
    body = await request.body()
    secret = os.getenv("GITHUB_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(
            status_code=503,
            detail="GitHub webhook signature verification is not configured.",
        )
    _verify_webhook_signature(secret, body, x_hub_signature_256)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        raise HTTPException(
            status_code=400, detail="Request body must be valid JSON."
        ) from error
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400, detail="Webhook payload must be a JSON object."
        )

    if x_github_event and x_github_event != "pull_request":
        return {"status": "ignored", "reason": "Unsupported GitHub event."}
    action = payload.get("action")
    if action not in {"opened", "reopened", "synchronize"}:
        return {"status": "ignored", "reason": "Unsupported pull request action."}
    pull_request = payload.get("pull_request")
    pr_url = pull_request.get("html_url") if isinstance(pull_request, dict) else None
    if not isinstance(pr_url, str):
        raise HTTPException(
            status_code=400, detail="Webhook payload has no pull request URL."
        )

    thread_id = str(uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    initial_state: ReviewState = {
        "pr_url": pr_url,
        "code_contents": "",
        "lint_errors": "",
        "refactored_code": "",
        "is_approved": False,
        "reviewer_feedback": "",
        "history": [],
    }
    try:
        graph.invoke(initial_state, config=config)
    except (ValueError, RuntimeError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {
        "status": "awaiting_approval",
        "thread_id": thread_id,
        "pr_url": pr_url,
    }


@app.get("/api/reviews/{thread_id}")
async def get_review(
    thread_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    """Return original source and the current proposal for an authenticated reviewer."""
    _require_review_auth(authorization)
    config = {"configurable": {"thread_id": thread_id}}
    snapshot = graph.get_state(config)
    if not snapshot.values:
        raise HTTPException(status_code=404, detail="Review thread was not found.")

    values = snapshot.values
    try:
        code_contents = json.loads(values["code_contents"])
        lint_errors = json.loads(values["lint_errors"])
        refactored_code = json.loads(values["refactored_code"])
    except (KeyError, json.JSONDecodeError) as error:
        raise HTTPException(
            status_code=500, detail="Review state is incomplete or invalid."
        ) from error
    return {
        "status": (
            "awaiting_approval"
            if "apply_changes_node" in snapshot.next
            else "completed"
        ),
        "thread_id": thread_id,
        "pr_url": values["pr_url"],
        "code_contents": code_contents,
        "lint_errors": lint_errors,
        "refactored_code": refactored_code,
        "reviewer_feedback": values["reviewer_feedback"],
    }


@app.post("/api/reviews/{thread_id}/approve")
async def decide_review(
    thread_id: str,
    decision: ReviewDecision,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    """Approve a proposal or return it to the proposal node with feedback."""
    _require_review_auth(authorization)

    config = {"configurable": {"thread_id": thread_id}}
    snapshot = graph.get_state(config)
    if not snapshot.values:
        raise HTTPException(status_code=404, detail="Review thread was not found.")
    if "apply_changes_node" not in snapshot.next:
        raise HTTPException(
            status_code=409,
            detail="This review is not waiting for an approval decision.",
        )
    if not decision.approved and not decision.feedback.strip():
        raise HTTPException(
            status_code=422,
            detail="Feedback is required when rejecting a proposal.",
        )

    if decision.approved:
        graph.update_state(config, {"is_approved": True})
    else:
        graph.update_state(
            config,
            {
                "is_approved": False,
                "reviewer_feedback": decision.feedback.strip(),
                "refactored_code": "",
            },
            as_node="lint_code_node",
        )

    try:
        result = graph.invoke(None, config=config)
    except (ValueError, RuntimeError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    current = graph.get_state(config)
    if "apply_changes_node" in current.next:
        return {
            "status": "awaiting_approval",
            "thread_id": thread_id,
            "history": result.get("history", []),
        }
    return {
        "status": "approved_and_applied",
        "thread_id": thread_id,
        "history": result.get("history", []),
    }
