import re
from pathlib import Path

from app.tools import run_linter_tool


def test_linter_reports_python_issues_and_cleans_up_temporary_file() -> None:
    code = "import os\n\ndef unused():\n    pass\n"

    result = run_linter_tool.invoke({"code_string": code})

    assert "F401" in result
    assert "F841" not in result
    match = re.search(r"(?m)^(.*source\.py):\d+:\d+:", result)
    assert match is not None
    assert not Path(match.group(1)).exists()


def test_linter_isolates_runs_from_each_other() -> None:
    first = run_linter_tool.invoke({"code_string": "x=1\n"})
    second = run_linter_tool.invoke({"code_string": "x = 1\n"})

    assert "E225" in first
    assert "No lint issues found." == second


def test_linter_accepts_clean_source() -> None:
    result = run_linter_tool.invoke(
        {"code_string": "def add(a, b):\n    return a + b\n"}
    )

    assert result == "No lint issues found."
