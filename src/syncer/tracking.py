from __future__ import annotations

from datetime import UTC
from datetime import datetime
from operator import itemgetter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from syncer.config import DATA_DIR

EVENTS_FILE = DATA_DIR / 'events.jsonl'


class RepoSnapshot(BaseModel):
    name: str
    path: str
    status: Literal['synced', 'issues', 'pulled', 'pushed', 'cloned', 'missing', 'not_git', 'no_remote', 'path_mismatch']
    branch: str | None = None
    uncommitted: int = 0
    unpushed: int = 0
    behind: int = 0
    stashes: int = 0


class RunSummary(BaseModel):
    total: int
    synced: int
    pulled: int
    pushed: int
    issues: int
    duration_ms: int


class SyncRunEvent(BaseModel):
    timestamp: datetime
    config_name: str
    dry_run: bool = False
    repos: list[RepoSnapshot]
    summary: RunSummary


def emit_event(event: SyncRunEvent, events_file: Path = EVENTS_FILE) -> None:
    events_file.parent.mkdir(parents=True, exist_ok=True)
    with events_file.open('a') as f:
        f.write(event.model_dump_json() + '\n')


def read_events(events_file: Path = EVENTS_FILE) -> list[SyncRunEvent]:
    if not events_file.exists():
        return []
    events = []
    for line in events_file.read_text().splitlines():
        line = line.strip()
        if line:
            events.append(SyncRunEvent.model_validate_json(line))
    return events


def find_stale_repos(events: list[SyncRunEvent], threshold_days: int = 3) -> list[tuple[str, int]]:
    """Find repos with uncommitted changes persisting across recent runs.

    Returns list of (repo_path, days_stale) tuples.
    """
    if not events:
        return []

    # Sort events oldest-first
    sorted_events = sorted(events, key=lambda e: e.timestamp)

    # For each repo, find the earliest consecutive run (from the end) with uncommitted > 0
    repo_dirty_since: dict[str, datetime] = {}

    for event in sorted_events:
        for snap in event.repos:
            if snap.uncommitted > 0:
                if snap.path not in repo_dirty_since:
                    repo_dirty_since[snap.path] = event.timestamp
            else:
                # Clean in this run — reset tracking
                repo_dirty_since.pop(snap.path, None)

    now = datetime.now(UTC)
    stale = []
    for path, since in repo_dirty_since.items():
        days = (now - since).days
        if days >= threshold_days:
            stale.append((path, days))

    return sorted(stale, key=itemgetter(1), reverse=True)
