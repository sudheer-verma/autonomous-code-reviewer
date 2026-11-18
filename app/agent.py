"""LangGraph workflow for reviewing and proposing pull request refactors."""

from __future__ import annotations

import json
import os
import re
from typing import TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app import github_service
from app.tools import run_linter_tool


class ReviewState(TypedDict):
    """State persisted between graph nodes and human decisions."""

    pr_url: str
    code_contents: str
    lint_errors: str
    refactored_code: str
    is_approved: bool
    reviewer_feedback: str
    history: list[str]


def _decode_files(serialized: str, state_field: str) -> dict[str, str]:
    try:
        files = json.loads(serialized)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"{state_field} must contain a JSON object of file contents."
        ) from error
    if not isinstance(files, dict) or not all(
        isinstance(path, str) and isinstance(content, str)
        for path, content in files.items()
    ):
        raise ValueError(f"{state_field} must contain a JSON object of file contents.")
    return files


def _model_response_text(response: object) -> str:
    content = getattr(response, "content", None)
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("The refactoring model returned an empty response.")
    code = content.strip()
    fenced = re.fullmatch(r"```(?:python)?\s*\n(.*?)\n```", code, re.DOTALL)
    return fenced.group(1) if fenced else code


def fetch_pr_node(state: ReviewState) -> dict[str, object]:
    """Fetch changed Python files from the PR head."""
    files = github_service.fetch_pull_request_code(state["pr_url"])
    return {
        "code_contents": json.dumps(files),
        "history": [*state["history"], f"Fetched {len(files)} Python file(s)."],
    }


def lint_code_node(state: ReviewState) -> dict[str, object]:
    """Lint each fetched Python file and record per-file output."""
    files = _decode_files(state["code_contents"], "code_contents")
    lint_results = {
        path: run_linter_tool.invoke({"code_string": source})
        for path, source in files.items()
    }
    return {
        "lint_errors": json.dumps(lint_results),
        "history": [*state["history"], f"Linted {len(files)} Python file(s)."],
    }


def propose_refactor_node(state: ReviewState) -> dict[str, object]:
    """Ask the configured model for a complete replacement for each file."""
    files = _decode_files(state["code_contents"], "code_contents")
    lint_results = _decode_files(state["lint_errors"], "lint_errors")
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required to propose refactors.")

    model = ChatOpenAI(model="gpt-4o-mini", api_key=api_key, temperature=0)
    proposals: dict[str, str] = {}
    for path, source in files.items():
        response = model.invoke(
            [
                SystemMessage(
                    content=(
                        "You are a careful Python code reviewer. Propose a minimal, "
                        "correct refactor. Preserve behavior and public interfaces. "
                        "Return only the complete replacement Python source, without "
                        "markdown fences or commentary."
                    )
                ),
                HumanMessage(
                    content=(
                        f"File: {path}\n\n"
                        f"Lint output:\n{lint_results.get(path, '')}\n\n"
                        f"Reviewer feedback from the previous proposal:\n"
                        f"{state['reviewer_feedback'] or '(none)'}\n\n"
                        f"Original source:\n{source}"
                    )
                ),
            ]
        )
        proposals[path] = _model_response_text(response)

    return {
        "refactored_code": json.dumps(proposals),
        "is_approved": False,
        "history": [*state["history"], "Prepared a refactoring proposal for review."],
    }


def apply_changes_node(state: ReviewState) -> dict[str, object]:
    """Commit approved replacements to the pull request branch."""
    if not state["is_approved"]:
        raise ValueError("Refusing to apply changes without explicit approval.")
    files = _decode_files(state["refactored_code"], "refactored_code")
    commit_url = github_service.commit_refactored_files(state["pr_url"], files)
    return {
        "history": [*state["history"], f"Committed approved changes: {commit_url}"],
    }


def build_graph(checkpointer: MemorySaver | None = None):
    """Build the review graph with an interruption immediately before commits."""
    workflow = StateGraph(ReviewState)
    workflow.add_node("fetch_pr_node", fetch_pr_node)
    workflow.add_node("lint_code_node", lint_code_node)
    workflow.add_node("propose_refactor_node", propose_refactor_node)
    workflow.add_node("apply_changes_node", apply_changes_node)
    workflow.add_edge(START, "fetch_pr_node")
    workflow.add_edge("fetch_pr_node", "lint_code_node")
    workflow.add_edge("lint_code_node", "propose_refactor_node")
    workflow.add_edge("propose_refactor_node", "apply_changes_node")
    workflow.add_edge("apply_changes_node", END)
    return workflow.compile(
        checkpointer=checkpointer or MemorySaver(),
        interrupt_before=["apply_changes_node"],
    )


graph = build_graph()
