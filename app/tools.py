"""Tools available to the code review agent."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from langchain_core.tools import tool


@tool
def run_linter_tool(code_string: str) -> str:
    """Run flake8 against Python source in a temporary, isolated directory."""
    with tempfile.TemporaryDirectory(prefix="code-review-lint-") as directory:
        source_path = Path(directory) / "source.py"
        source_path.write_text(code_string, encoding="utf-8")

        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "flake8",
                    "--isolated",
                    "--max-line-length=88",
                    str(source_path),
                ],
                cwd=directory,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except FileNotFoundError as error:
            raise RuntimeError(
                "flake8 is unavailable. Install the project requirements first."
            ) from error
        except subprocess.TimeoutExpired as error:
            raise TimeoutError("flake8 exceeded its 30-second time limit.") from error

        output = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part.strip()
        )
        if result.returncode == 0:
            return output or "No lint issues found."
        if result.returncode == 1:
            return output or "flake8 reported lint issues without output."
        raise RuntimeError(
            f"flake8 failed with exit code {result.returncode}: "
            f"{output or 'no diagnostic output'}"
        )
