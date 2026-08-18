"""Regression checks for the supported Python 3.12 production runtime."""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _requirements() -> dict[str, str]:
    pins = {}
    for raw in (ROOT / "backend" / "requirements.txt").read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if "==" in line:
            name, version = line.split("==", 1)
            pins[name.strip().lower()] = version.strip()
    return pins


def test_python312_alpaca_compatibility_is_pinned():
    pins = _requirements()
    assert pins.get("pandas") == "2.3.3"
    assert pins.get("pytz") == "2026.3.post1"


def test_startup_backfill_explicitly_uses_iex():
    tree = ast.parse((ROOT / "backend" / "main.py").read_text())
    orchestrator = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Orchestrator"
    )
    backfill = next(
        node for node in orchestrator.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_run_historical_backfill"
    )
    requests = [
        node for node in ast.walk(backfill)
        if isinstance(node, ast.Call)
        and (
            isinstance(node.func, ast.Name) and node.func.id == "StockBarsRequest"
            or isinstance(node.func, ast.Attribute) and node.func.attr == "StockBarsRequest"
        )
    ]
    assert requests, "startup backfill no longer constructs StockBarsRequest"
    for request in requests:
        feed = next((kw.value for kw in request.keywords if kw.arg == "feed"), None)
        assert isinstance(feed, ast.Attribute)
        assert isinstance(feed.value, ast.Name)
        assert (feed.value.id, feed.attr) == ("DataFeed", "IEX")


def test_ci_does_not_mask_pytest_and_requires_completed_cycle():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "python -m pytest -q backend/tests || true" not in workflow
    assert "--ignore=backend/tests/test_suite.py" in workflow
    assert "--ignore=backend/tests/test_end_to_end.py" in workflow
    assert 'grep -q "Cycle #1 complete"' in workflow
    assert "authenticated API ready and cycle 1 completed" in workflow
