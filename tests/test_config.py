import pytest

from app.config import ConfigError, load_settings


def base_env(monkeypatch):
    monkeypatch.setenv("APP_MODE", "simulation")
    monkeypatch.setenv("DATABASE_PATH", ":memory:")
    monkeypatch.setenv("GITHUB_REPO", "acme/superset-demo")
    monkeypatch.setenv("GITHUB_ALLOWED_APPROVERS", "ops-lead")


def test_missing_mode_fails_closed(monkeypatch):
    monkeypatch.delenv("APP_MODE", raising=False)
    with pytest.raises(ConfigError):
        load_settings()


def test_unknown_mode_fails_closed(monkeypatch):
    monkeypatch.setenv("APP_MODE", "prod")
    with pytest.raises(ConfigError):
        load_settings()


def test_simulation_requires_no_secrets(monkeypatch):
    base_env(monkeypatch)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    s = load_settings()
    assert s.app_mode == "simulation"


def test_live_requires_all_config(monkeypatch):
    base_env(monkeypatch)
    monkeypatch.setenv("APP_MODE", "live")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(ConfigError) as e:
        load_settings()
    assert "GITHUB_TOKEN" in str(e.value)


def test_live_rejects_placeholder_values(monkeypatch):
    base_env(monkeypatch)
    monkeypatch.setenv("APP_MODE", "live")
    for k, v in {
        "GITHUB_TOKEN": "REPLACE_WITH_PAT",
        "GITHUB_BASE_BRANCH": "main",
        "DEVIN_API_KEY": "devin_live_xxx",
        "DEVIN_ORG_ID": "org-abc",
        "DEVIN_REPO_REF": "acme/superset-demo",
        "DEVIN_REMEDIATION_PLAYBOOK_ID": "playbook-x",
        "REPORT_GITHUB_REPO": "acme/devin-repair-desk",
        "REPORT_DATA_ISSUE_NUMBER": "1",
        "REPORT_GITHUB_TOKEN": "ghp_x",
    }.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ConfigError):
        load_settings()
