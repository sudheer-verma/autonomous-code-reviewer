import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import agent
from app import main as main_module
from app.main import app


class FakeModel:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, messages: list[object]) -> SimpleNamespace:
        self.calls += 1
        return SimpleNamespace(content=f"# proposal {self.calls}\nprint('ok')\n")


def test_rejection_routes_back_to_proposal_and_keeps_checkpoint_history(
    monkeypatch,
) -> None:
    model = FakeModel()
    monkeypatch.setattr(
        agent.github_service,
        "fetch_pull_request_code",
        lambda _url: {"sample.py": "print('original')\n"},
    )
    monkeypatch.setattr(agent, "ChatOpenAI", lambda **_kwargs: model)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    review_graph = agent.build_graph()
    config = {"configurable": {"thread_id": "rejection-test"}}
    initial = {
        "pr_url": "https://github.com/example/project/pull/1",
        "code_contents": "",
        "lint_errors": "",
        "refactored_code": "",
        "is_approved": False,
        "reviewer_feedback": "",
        "history": [],
    }

    review_graph.invoke(initial, config=config)
    before_rejection = review_graph.get_state(config)
    assert before_rejection.next == ("apply_changes_node",)
    first_proposal = json.loads(before_rejection.values["refactored_code"])
    assert first_proposal["sample.py"].startswith("# proposal 1")
    original_contents = before_rejection.values["code_contents"]
    original_history = before_rejection.values["history"]

    review_graph.update_state(
        config,
        {
            "is_approved": False,
            "reviewer_feedback": "Keep the existing output unchanged.",
            "refactored_code": "",
        },
        as_node="lint_code_node",
    )
    rerouted = review_graph.get_state(config)
    assert rerouted.next == ("propose_refactor_node",)
    assert rerouted.values["code_contents"] == original_contents
    assert rerouted.values["refactored_code"] == ""
    assert rerouted.values["reviewer_feedback"] == (
        "Keep the existing output unchanged."
    )
    assert rerouted.values["history"] == original_history

    review_graph.invoke(None, config=config)
    after_rejection = review_graph.get_state(config)
    second_proposal = json.loads(after_rejection.values["refactored_code"])
    assert after_rejection.next == ("apply_changes_node",)
    assert second_proposal["sample.py"].startswith("# proposal 2")
    assert after_rejection.values["reviewer_feedback"] == (
        "Keep the existing output unchanged."
    )
    assert after_rejection.values["code_contents"] == original_contents
    assert len(list(review_graph.get_state_history(config))) >= 2


def test_approval_resumes_at_commit_node(monkeypatch) -> None:
    committed: list[dict[str, str]] = []
    monkeypatch.setattr(
        agent.github_service,
        "fetch_pull_request_code",
        lambda _url: {"sample.py": "print('original')\n"},
    )
    monkeypatch.setattr(agent, "ChatOpenAI", lambda **_kwargs: FakeModel())
    monkeypatch.setattr(
        agent.github_service,
        "commit_refactored_files",
        lambda _url, files: committed.append(files) or "https://github.com/commit/abc",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    review_graph = agent.build_graph()
    config = {"configurable": {"thread_id": "approval-test"}}
    review_graph.invoke(
        {
            "pr_url": "https://github.com/example/project/pull/1",
            "code_contents": "",
            "lint_errors": "",
            "refactored_code": "",
            "is_approved": False,
            "reviewer_feedback": "",
            "history": [],
        },
        config=config,
    )

    review_graph.update_state(config, {"is_approved": True})
    result = review_graph.invoke(None, config=config)

    assert committed and committed[0]["sample.py"].startswith("# proposal")
    assert any("https://github.com/commit/abc" in item for item in result["history"])


def test_approval_endpoint_rejects_missing_feedback(monkeypatch) -> None:
    monkeypatch.setattr(
        agent.github_service,
        "fetch_pull_request_code",
        lambda _url: {"sample.py": "print('original')\n"},
    )
    monkeypatch.setattr(agent, "ChatOpenAI", lambda **_kwargs: FakeModel())
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("REVIEW_API_TOKEN", "test-review-token")
    review_graph = agent.build_graph()
    monkeypatch.setattr(main_module, "graph", review_graph)
    review_graph.invoke(
        {
            "pr_url": "https://github.com/example/project/pull/1",
            "code_contents": "",
            "lint_errors": "",
            "refactored_code": "",
            "is_approved": False,
            "reviewer_feedback": "",
            "history": [],
        },
        config={"configurable": {"thread_id": "missing-feedback-test"}},
    )
    client = TestClient(app)
    review_response = client.get(
        "/api/reviews/missing-feedback-test",
        headers={"Authorization": "Bearer test-review-token"},
    )
    assert review_response.status_code == 200
    assert review_response.json()["code_contents"] == {
        "sample.py": "print('original')\n"
    }
    assert review_response.json()["refactored_code"]["sample.py"].startswith(
        "# proposal"
    )

    response = client.post(
        "/api/reviews/missing-feedback-test/approve",
        json={"approved": False, "feedback": ""},
        headers={"Authorization": "Bearer test-review-token"},
    )

    assert response.status_code == 422


def test_approval_endpoint_requires_bearer_authentication(monkeypatch) -> None:
    monkeypatch.setenv("REVIEW_API_TOKEN", "test-review-token")
    client = TestClient(app)

    response = client.post(
        "/api/reviews/unknown-thread/approve",
        json={"approved": True},
    )

    assert response.status_code == 401


def test_webhook_rejects_requests_when_signature_secret_is_unset(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    client = TestClient(app)

    response = client.post("/api/webhooks/github", json={"action": "opened"})

    assert response.status_code == 503


def test_webhook_rejects_invalid_signature(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "test-webhook-secret")
    client = TestClient(app)

    response = client.post(
        "/api/webhooks/github",
        json={"action": "opened"},
        headers={"X-Hub-Signature-256": "sha256=invalid"},
    )

    assert response.status_code == 401
