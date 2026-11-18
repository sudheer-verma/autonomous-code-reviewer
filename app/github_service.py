"""GitHub pull request retrieval and atomic commit operations."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from github import Github
from github.InputGitTreeElement import InputGitTreeElement

MAX_PYTHON_FILES = 20
PULL_REQUEST_URL_PATTERN = re.compile(r"^/([^/]+)/([^/]+)/pull/([1-9][0-9]*)/?$")


@dataclass(frozen=True)
class PullRequest:
    """Parsed pull request coordinates."""

    owner: str
    repository: str
    number: int


def parse_pull_request_url(pr_url: str) -> PullRequest:
    """Validate a GitHub pull request URL and return its coordinates."""
    parsed = urlparse(pr_url)
    match = PULL_REQUEST_URL_PATTERN.fullmatch(parsed.path)
    if parsed.scheme != "https" or parsed.hostname != "github.com" or not match:
        raise ValueError(
            "pr_url must be an https://github.com/{owner}/{repo}/pull/{number} URL."
        )
    owner, repository, number = match.groups()
    return PullRequest(owner, repository, int(number))


def _github_client() -> Github:
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is required to access GitHub.")
    return Github(login_or_token=token, timeout=30)


def fetch_pull_request_code(pr_url: str) -> dict[str, str]:
    """Return Python source files changed by a pull request, read from its head."""
    coordinates = parse_pull_request_url(pr_url)
    client = _github_client()
    repository = client.get_repo(f"{coordinates.owner}/{coordinates.repository}")
    pull_request = repository.get_pull(coordinates.number)

    python_files = [
        changed_file
        for changed_file in pull_request.get_files()
        if changed_file.filename.endswith(".py") and changed_file.status != "removed"
    ]
    if not python_files:
        raise ValueError("The pull request contains no changed Python files to review.")
    if len(python_files) > MAX_PYTHON_FILES:
        raise ValueError(
            f"The pull request changes {len(python_files)} Python files; "
            f"the supported maximum is {MAX_PYTHON_FILES}."
        )

    head_sha = pull_request.head.sha
    return {
        changed_file.filename: repository.get_contents(
            changed_file.filename, ref=head_sha
        ).decoded_content.decode("utf-8")
        for changed_file in python_files
    }


def commit_refactored_files(pr_url: str, refactored_files: dict[str, str]) -> str:
    """Commit all refactored files together on the pull request's head branch."""
    if not refactored_files:
        raise ValueError("There are no refactored files to commit.")

    coordinates = parse_pull_request_url(pr_url)
    client = _github_client()
    repository = client.get_repo(f"{coordinates.owner}/{coordinates.repository}")
    pull_request = repository.get_pull(coordinates.number)
    head_sha = pull_request.head.sha
    base_commit = repository.get_git_commit(head_sha)
    elements = [
        InputGitTreeElement(
            path=path,
            mode="100644",
            type="blob",
            sha=repository.create_git_blob(contents, "utf-8").sha,
        )
        for path, contents in refactored_files.items()
    ]
    tree = repository.create_git_tree(elements, base_tree=base_commit.tree)
    commit = repository.create_git_commit(
        "Apply approved code review refactor",
        tree,
        [base_commit],
    )
    repository.get_git_ref(f"heads/{pull_request.head.ref}").edit(commit.sha)
    return commit.html_url
