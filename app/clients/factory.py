"""Client selection — fail closed on APP_MODE.

``build_clients`` is the single place that maps configuration to concrete
clients, so job handlers stay identical across modes.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..config import Settings
from .base import DevinClient, GitHubClient, ReportSink
from .devin import LiveDevinClient, LiveReportSink
from .fakes import FakeDevinClient, FakeGitHubClient, FakeReportSink
from .github import LiveGitHubClient


@dataclass
class Clients:
    github: GitHubClient
    devin: DevinClient
    report_sink: ReportSink
    mode: str


def build_clients(settings: Settings, conn: sqlite3.Connection) -> Clients:
    if settings.app_mode == "simulation":
        return Clients(
            github=FakeGitHubClient(conn),
            devin=FakeDevinClient(conn),
            report_sink=FakeReportSink(conn),
            mode="simulation",
        )
    if settings.app_mode == "live":
        return Clients(
            github=LiveGitHubClient(
                repo=settings.github_repo,
                token=settings.github_token,
                api_base_url=settings.github_api_base_url,
            ),
            devin=LiveDevinClient(
                api_key=settings.devin_api_key,
                org_id=settings.devin_org_id,
                api_base_url=settings.devin_api_base_url,
            ),
            report_sink=LiveReportSink(
                token=settings.report_github_token,
                api_base_url=settings.github_api_base_url,
            ),
            mode="live",
        )
    # Unreachable for settings from load_settings, but keeps a hand-built
    # Settings object from smuggling in a third mode.
    raise ValueError(f"unknown APP_MODE {settings.app_mode!r}")
