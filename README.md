# Autonomous Code Review & Refactoring Agent

A FastAPI and LangGraph service that reviews changed Python files in GitHub pull
requests, runs `flake8`, proposes refactors with OpenAI, and commits them only
after a human explicitly approves the proposal.

## Requirements

- Python 3.10 or newer
- A GitHub personal access token that can read pull requests and write to the
  target repository
- An OpenAI API key

## Setup

From the project directory, create a local environment file and install the
dependencies:

```powershell
Copy-Item .env.example .env
```

Edit `.env` and set `GITHUB_TOKEN`, `OPENAI_API_KEY`,
`GITHUB_WEBHOOK_SECRET`, and `REVIEW_API_TOKEN` to valid secrets. The webhook
secret is required to verify GitHub's HMAC-SHA256 signature, and the approval
endpoint requires `Authorization: Bearer <REVIEW_API_TOKEN>`. Generate unique,
high-entropy secrets for production and rotate them if exposed. Requests fail
closed with HTTP 503 if either endpoint's secret is not configured.

Install dependencies:

```powershell
pip install -r requirements.txt
```

Run the API server:

```powershell
uvicorn app.main:app --reload
```

The API documentation is available at `http://127.0.0.1:8000/docs`.

## GitHub webhook

Configure a repository webhook to send `pull_request` events to
`https://<your-host>/api/webhooks/github`. The service starts reviews for
`opened`, `reopened`, and `synchronize` actions. Set the GitHub webhook secret
to the same value as `GITHUB_WEBHOOK_SECRET`; the endpoint rejects requests
until signature verification is configured.

The response contains a `thread_id`. Retrieve the original source and current
proposal through the authenticated review endpoint before deciding:

```http
GET /api/reviews/{thread_id}
Authorization: Bearer <REVIEW_API_TOKEN>
```

Then submit the decision:

```http
POST /api/reviews/{thread_id}/approve
Authorization: Bearer <REVIEW_API_TOKEN>
Content-Type: application/json

{"approved": false, "feedback": "Please preserve the existing public API."}
```

Rejecting a proposal stores the feedback and re-runs the proposal node from the
pre-proposal checkpoint. Approving it resumes the graph and creates one commit
on the pull request's head branch.

## Tests

Run the test suite:

```powershell
pytest
```

## Operational notes

- The graph checkpoint store is `MemorySaver`, as required. Checkpoints and
  thread IDs live only in the running process and are lost on restart; use a
  durable LangGraph checkpointer before deploying multiple workers or requiring
  restart recovery.
- The linter writes source to a unique temporary directory and runs flake8 in
  isolated-configuration mode. It does not execute the reviewed Python source.
- Reviews are limited to 20 changed, non-deleted `.py` files per pull request.
- Use HTTPS and consider rate limiting both API routes at the deployment
  boundary. Never expose repository write credentials in client-side code.
