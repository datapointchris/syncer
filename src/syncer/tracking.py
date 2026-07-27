from __future__ import annotations

from datetime import UTC
from datetime import datetime
from operator import itemgetter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from syncer.config import DATA_DIR

# The single global stream that predates per-registry events. Adopted once by the default
# registry (see adopt_legacy_events) rather than orphaned.
LEGACY_EVENTS_FILE = DATA_DIR / 'events.jsonl'


def events_file_for(repos_file: Path) -> Path:
    """Event stream for one registry, keyed on the registry file that defines it.

    One stream per registry, because a shared file makes `stats` a blend of two unrelated
    working sets, and find_stale_repos() scopes to the paths in the most recent run — so
    alternating registries would make each set's dirty-repo warnings vanish on the other's run.
    """
    return DATA_DIR / f'{repos_file.stem}-events.jsonl'


def adopt_legacy_events(events_file: Path) -> None:
    """Hand the pre-split global stream to the registry that actually wrote it.

    A rename, not a copy, so it happens exactly once and no second registry can claim it.
    Call only for the default registry; an explicit --repos-file names a set that never
    contributed to that history.
    """
    if events_file.exists() or not LEGACY_EVENTS_FILE.exists():
        return
    events_file.parent.mkdir(parents=True, exist_ok=True)
    LEGACY_EVENTS_FILE.rename(events_file)


RepoStatus = Literal[
    'synced',
    'issues',
    'pulled',
    'pushed',
    'pull_pushed',
    'cloned',
    'missing',
    'not_git',
    'no_remote',
    'path_mismatch',
]


class BranchSnapshot(BaseModel):
    """Per-branch detail captured on a run. Added additively — older events have none."""

    branch: str
    primary: str
    ahead: int = 0
    behind: int = 0
    is_default: bool = False
    is_current: bool = False
    action: str | None = None
    outcome: str | None = None


class RepoSnapshot(BaseModel):
    name: str
    path: str
    status: RepoStatus
    branch: str | None = None
    uncommitted: int = 0
    unpushed: int = 0
    behind: int = 0
    stashes: int = 0
    # Additive per-branch fields (default empty → old events still validate, stats.py unaffected).
    policy: str | None = None
    branches: list[BranchSnapshot] = []


class RunSummary(BaseModel):
    total: int
    synced: int
    cloned: int = 0
    pulled: int
    pushed: int
    pull_pushed: int = 0
    issues: int
    duration_ms: int


class SyncRunEvent(BaseModel):
    timestamp: datetime
    config_name: str
    dry_run: bool = False
    repos: list[RepoSnapshot]
    summary: RunSummary


def emit_event(event: SyncRunEvent, events_file: Path) -> None:
    events_file.parent.mkdir(parents=True, exist_ok=True)
    with events_file.open('a') as f:
        f.write(event.model_dump_json() + '\n')


def read_events(events_file: Path) -> list[SyncRunEvent]:
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

    # Only report repos that exist in the most recent event (current config).
    # Old paths from renamed/removed repos would otherwise stay dirty forever.
    current_paths = {snap.path for snap in sorted_events[-1].repos}

    now = datetime.now(UTC)
    stale = []
    for path, since in repo_dirty_since.items():
        if path not in current_paths:
            continue
        days = (now - since).days
        if days >= threshold_days:
            stale.append((path, days))

    return sorted(stale, key=itemgetter(1), reverse=True)
