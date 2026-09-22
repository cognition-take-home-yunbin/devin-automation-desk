"""Shared handler context: DB handle, settings, and mode-selected clients."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..clients.factory import Clients
from ..config import Settings


@dataclass
class ServiceContext:
    conn: sqlite3.Connection
    settings: Settings
    clients: Clients

    @property
    def mode(self) -> str:
        return self.clients.mode
