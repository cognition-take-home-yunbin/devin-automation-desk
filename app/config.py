"""Runtime configuration — the application contract from tutorial step 10.4.

APP_MODE selects the client implementations and is fail-closed: an unset or
unknown mode refuses to start, and ``live`` mode rejects incomplete
configuration rather than falling back to simulation behaviour. Simulation
mode never reads the credential variables.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

APP_MODES = ("simulation", "live")

DEFAULT_VERIFICATION_POLICY_PATH = "config/verification.yaml"

# Variables that must be present (non-placeholder) for APP_MODE=live. Numeric
# and scheduling values have safe defaults; identity and credential values do
# not. Simulation mode never requires keys.
LIVE_REQUIRED_VARS = (
    "GITHUB_REPO",
    "GITHUB_BASE_BRANCH",
    "GITHUB_TOKEN",
    "GITHUB_ALLOWED_APPROVERS",
    "CANDIDATE_ISSUE_LABEL",
    "APPROVAL_ISSUE_LABEL",
    "DEVIN_API_KEY",
    "DEVIN_ORG_ID",
    "DEVIN_REPO_REF",
    "DEVIN_REMEDIATION_PLAYBOOK_ID",
    "DEVIN_KNOWLEDGE_IDS",
    "REPORT_GITHUB_REPO",
    "REPORT_DATA_ISSUE_NUMBER",
    "REPORT_GITHUB_TOKEN",
    "VERIFICATION_POLICY_PATH",
)

_PLACEHOLDERS = ("REPLACE_", "YOUR_GITHUB_NAME")


class ConfigError(RuntimeError):
    """Configuration is missing, inconsistent, or unsafe."""


def _is_placeholder(value: str) -> bool:
    return any(value.startswith(p) for p in _PLACEHOLDERS)


@dataclass(frozen=True)
class Settings:
    app_mode: str
    database_path: str
    app_host: str
    app_port: int

    github_repo: str
    github_base_branch: str
    github_token: str
    github_allowed_approvers: tuple[str, ...]
    github_api_base_url: str
    candidate_issue_label: str
    approval_issue_label: str
    scan_interval_seconds: float
    verification_policy_path: str

    devin_api_key: str
    devin_org_id: str
    devin_repo_ref: str
    devin_remediation_playbook_id: str
    devin_knowledge_ids: tuple[str, ...]
    devin_api_base_url: str

    max_active_sessions: int
    repair_acu_limit: int
    daily_admission_acu_limit: int
    project_admission_acu_limit: int
    poll_interval_seconds: float
    dispatch_paused_on_first_start: bool

    report_github_repo: str
    report_data_issue_number: int | None
    report_github_token: str
    report_timezone: str
    report_publish_interval_seconds: float
    report_stale_after_seconds: float
    native_report_session_tag: str

    job_max_attempts: int
    job_lease_seconds: float
    worker_idle_seconds: float
    evidence_export_dir: str

    raw: dict[str, str] = field(default_factory=dict)

    @property
    def is_simulation(self) -> bool:
        return self.app_mode == "simulation"


def _get(raw: dict[str, str], name: str, default: str | None = None) -> str:
    value = raw.get(name, default)
    if value is None:
        raise ConfigError(f"required environment variable {name} is not set")
    return value.strip()


def _get_csv(raw: dict[str, str], name: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in raw.get(name, "").split(",") if v.strip())


def load_settings(env: dict[str, str] | None = None) -> Settings:
    raw = dict(os.environ if env is None else env)

    app_mode = _get(raw, "APP_MODE", "").lower()
    if app_mode not in APP_MODES:
        # Fail closed: never guess a mode. An unset or misspelled APP_MODE
        # must not silently behave like simulation or like live.
        shown = repr(app_mode) if app_mode else "<unset>"
        raise ConfigError(
            f"APP_MODE must be one of {APP_MODES}, got {shown}"
        )

    settings = Settings(
        app_mode=app_mode,
        database_path=_get(raw, "DATABASE_PATH", "./data/repairdesk-sim.sqlite"),
        app_host=_get(raw, "APP_HOST", "127.0.0.1"),
        app_port=int(_get(raw, "APP_PORT", "8000")),

        github_repo=raw.get("GITHUB_REPO", "").strip(),
        github_base_branch=raw.get("GITHUB_BASE_BRANCH", "").strip(),
        github_token=raw.get("GITHUB_TOKEN", "").strip(),
        github_allowed_approvers=_get_csv(raw, "GITHUB_ALLOWED_APPROVERS"),
        github_api_base_url=raw.get(
            "GITHUB_API_BASE_URL", "https://api.github.com"
        ).strip(),
        candidate_issue_label=raw.get(
            "CANDIDATE_ISSUE_LABEL", "devin-candidate"
        ).strip(),
        approval_issue_label=raw.get(
            "APPROVAL_ISSUE_LABEL", "devin-approved"
        ).strip(),
        scan_interval_seconds=float(_get(raw, "SCAN_INTERVAL_SECONDS", "60")),
        verification_policy_path=_get(
            raw, "VERIFICATION_POLICY_PATH", DEFAULT_VERIFICATION_POLICY_PATH
        ),

        devin_api_key=raw.get("DEVIN_API_KEY", "").strip(),
        devin_org_id=raw.get("DEVIN_ORG_ID", "").strip(),
        devin_repo_ref=raw.get("DEVIN_REPO_REF", "").strip(),
        devin_remediation_playbook_id=raw.get(
            "DEVIN_REMEDIATION_PLAYBOOK_ID", ""
        ).strip(),
        devin_knowledge_ids=_get_csv(raw, "DEVIN_KNOWLEDGE_IDS"),
        devin_api_base_url=raw.get(
            "DEVIN_API_BASE_URL", "https://api.devin.ai"
        ).strip(),

        max_active_sessions=int(_get(raw, "MAX_ACTIVE_SESSIONS", "1")),
        repair_acu_limit=int(_get(raw, "REPAIR_ACU_LIMIT", "20")),
        daily_admission_acu_limit=int(_get(raw, "DAILY_ADMISSION_ACU_LIMIT", "60")),
        project_admission_acu_limit=int(
            _get(raw, "PROJECT_ADMISSION_ACU_LIMIT", "180")
        ),
        poll_interval_seconds=float(_get(raw, "POLL_INTERVAL_SECONDS", "15")),
        dispatch_paused_on_first_start=_get(
            raw, "DISPATCH_PAUSED_ON_FIRST_START", "false"
        ).lower()
        in ("1", "true", "yes"),

        report_github_repo=raw.get("REPORT_GITHUB_REPO", "").strip(),
        report_data_issue_number=(
            int(raw["REPORT_DATA_ISSUE_NUMBER"])
            if raw.get("REPORT_DATA_ISSUE_NUMBER", "").strip().isdigit()
            else None
        ),
        report_github_token=raw.get("REPORT_GITHUB_TOKEN", "").strip(),
        report_timezone=raw.get("REPORT_TIMEZONE", "UTC").strip(),
        report_publish_interval_seconds=float(
            _get(raw, "REPORT_PUBLISH_INTERVAL_SECONDS", "300")
        ),
        report_stale_after_seconds=float(
            _get(raw, "REPORT_STALE_AFTER_SECONDS", "900")
        ),
        native_report_session_tag=raw.get(
            "NATIVE_REPORT_SESSION_TAG", "repairdesk-native-report"
        ).strip(),

        job_max_attempts=int(_get(raw, "JOB_MAX_ATTEMPTS", "8")),
        job_lease_seconds=float(_get(raw, "JOB_LEASE_SECONDS", "120")),
        worker_idle_seconds=float(_get(raw, "WORKER_IDLE_SECONDS", "1")),
        evidence_export_dir=_get(raw, "EVIDENCE_EXPORT_DIR", "./exports"),

        raw=raw,
    )

    if app_mode == "live":
        missing = [
            name
            for name in LIVE_REQUIRED_VARS
            if not raw.get(name, "").strip()
            or _is_placeholder(raw.get(name, "").strip())
        ]
        if missing:
            raise ConfigError(
                "APP_MODE=live requires complete configuration; "
                "missing or placeholder: " + ", ".join(missing)
            )
        if not settings.github_allowed_approvers:
            raise ConfigError(
                "APP_MODE=live requires at least one login in "
                "GITHUB_ALLOWED_APPROVERS"
            )
    return settings


def doctor_report(env: dict[str, str] | None = None) -> tuple[bool, list[dict]]:
    """Return (ok, checks) describing configuration health for `doctor`.

    Read-only: reports presence and shape, never secret values.
    """
    raw = dict(os.environ if env is None else env)
    checks: list[dict[str, object]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    mode = raw.get("APP_MODE", "").strip().lower()
    check("APP_MODE set", mode in APP_MODES, mode or "<unset>")

    db_path = raw.get("DATABASE_PATH", "./data/repairdesk-sim.sqlite")
    try:
        import sqlite3

        from . import db as db_mod

        conn = db_mod.connect(db_path)
        conn.execute("SELECT 1")
        conn.close()
        check("database writable", True, db_path)
    except Exception as exc:  # noqa: BLE001 - surfaced as a failed check
        check("database writable", False, f"{db_path}: {exc}")

    policy_path = raw.get(
        "VERIFICATION_POLICY_PATH", DEFAULT_VERIFICATION_POLICY_PATH
    )
    policy_exists = os.path.exists(policy_path)
    check("verification policy file exists", policy_exists, policy_path)
    if policy_exists:
        try:
            from .services.policy import load_policy

            policy = load_policy(policy_path)
            check(
                "verification policy parses",
                True,
                f"version={policy.version} checks={len(policy.required_checks)}",
            )
            check(
                "verification policy non-empty",
                len(policy.required_checks) > 0,
                "an empty check list is not success",
            )
        except Exception as exc:  # noqa: BLE001
            check("verification policy parses", False, str(exc))

    if mode == "live":
        missing = [
            name
            for name in LIVE_REQUIRED_VARS
            if not raw.get(name, "").strip()
            or _is_placeholder(raw.get(name, "").strip())
        ]
        check(
            "live configuration complete",
            not missing,
            "missing/placeholder: " + ", ".join(missing) if missing else "all present",
        )
        check(
            "report source configured",
            bool(raw.get("REPORT_GITHUB_REPO") and raw.get("REPORT_DATA_ISSUE_NUMBER")),
            raw.get("REPORT_GITHUB_REPO", "<unset>"),
        )
        check(
            "live external writes",
            False,
            "milestone A does not implement live external writes",
        )
    elif mode == "simulation":
        check(
            "simulation mode",
            True,
            "fake GitHub/Devin clients; no credentials required",
        )

    return all(bool(c["ok"]) for c in checks), checks
