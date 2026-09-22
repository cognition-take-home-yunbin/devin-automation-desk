"""Devin Repair Desk — automation that turns approved Superset bug reports
into Devin-authored pull requests.

Milestone A: one application process (FastAPI) with a single asynchronous
background worker claiming durable jobs from SQLite. Simulation mode uses
fake GitHub/Devin clients only; live external writes are not implemented yet.
"""

__version__ = "0.1.0"
