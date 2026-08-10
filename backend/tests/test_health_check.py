"""Focused tests for the standalone external health-check configuration."""

import sys
from pathlib import Path

import pytest

from scripts import health_check


def test_warn_if_webhook_unconfigured_is_actionable(monkeypatch, capsys):
    monkeypatch.setattr(health_check, "WEBHOOK_URL", "")

    assert health_check.warn_if_webhook_unconfigured() is False

    output = capsys.readouterr().out
    assert "HEALTH_WEBHOOK_URL is empty" in output
    assert "/etc/educated-trades-health.env" in output
    assert "restart the health-check timer" in output


def test_warn_if_webhook_unconfigured_is_quiet_when_configured(monkeypatch, capsys):
    monkeypatch.setattr(health_check, "WEBHOOK_URL", "https://example.test/webhook")

    assert health_check.warn_if_webhook_unconfigured() is True
    assert capsys.readouterr().out == ""


def test_normal_run_continues_without_webhook_but_reports_disabled_delivery(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(health_check, "WEBHOOK_URL", "")
    monkeypatch.setattr(health_check, "STATE_PATH", Path(tmp_path) / "state.json")
    monkeypatch.setattr(health_check, "check_heartbeat", lambda: [])
    monkeypatch.setattr(health_check, "check_critical_logs", lambda state: ([], None))
    monkeypatch.setattr(
        health_check, "check_mode_change", lambda state: ([], "autonomous")
    )
    monkeypatch.setattr(sys, "argv", ["health_check.py"])

    health_check.main()

    output = capsys.readouterr().out
    assert "HEALTH_WEBHOOK_URL is empty" in output
    assert "All checks passed" in output
    assert "Webhook alerts are disabled" in output


def test_test_mode_without_webhook_fails_loudly(monkeypatch, capsys):
    monkeypatch.setattr(health_check, "WEBHOOK_URL", "")
    monkeypatch.setattr(sys, "argv", ["health_check.py", "--test"])

    with pytest.raises(SystemExit) as exc_info:
        health_check.main()

    assert exc_info.value.code == 1
    output = capsys.readouterr().out
    assert "HEALTH_WEBHOOK_URL is empty" in output
    assert "TEST MODE" in output
    assert "Test alert FAILED" in output


def test_health_check_service_uses_deployment_env_file():
    service = Path(__file__).parents[2] / "scripts" / "health-check.service"

    assert "EnvironmentFile=/etc/educated-trades-health.env" in service.read_text()
