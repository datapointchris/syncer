from __future__ import annotations

from contextlib import suppress
from datetime import UTC
from datetime import datetime
from operator import itemgetter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from pydantic import ValidationError

from syncer.config import LEGACY_DATA_DIR
from syncer.config import STATE_DIR

# The single global stream that predates per-registry events. Adopted once by the default
# registry (see migrate_legacy_events) rather than orphaned.
LEGACY_EVENTS_FILE = LEGACY_DATA_DIR / 'events.jsonl'


def events_file_for(repos_file: Path) -> Path:
    """Event stream for one registry, keyed on the registry file that defines it.

    One stream per registry, because a shared file makes `stats` a blend of two unrelated
    working sets, and find_stale_repos() scopes to the paths in the most recent run — so
    alternating registries would make each set's dirty-repo warnings vanish on the other's run.
    """
    return STATE_DIR / f'{repos_file.stem}-events.jsonl'


# MIGRATION (v5.0.0): run history moved from ~/.local/share/syncer (XDG data) to
# $XDG_STATE_HOME/syncer, and from a single global events.jsonl to one stream per registry.
# Retire once every machine you run syncer on has run >= 5.0.0, observable as
# ~/.local/share/syncer being absent on all of them.
def migrate_legacy_events(events_file: Path, adopt_global: bool = False) -> None:
    """Move any run history left in the old data dir into the state dir.

    Two shapes exist out there, because a machine may have skipped the release that split the
    stream per registry: already-split `*-events.jsonl` files, which keep their names, and the
    pre-split global `events.jsonl`, which belongs to the default registry alone — an explicit
    --repos-file names a set that never contributed to it, hence `adopt_global`.

    Renames, never copies, so it runs exactly once and cannot re-adopt a stream another registry
    has since claimed. An existing target always wins.
    """
    if not LEGACY_DATA_DIR.is_dir():
        return

    events_file.parent.mkdir(parents=True, exist_ok=True)
    for legacy_stream in sorted(LEGACY_DATA_DIR.glob('*-events.jsonl')):
        target = events_file.parent / legacy_stream.name
        if not target.exists():
            legacy_stream.rename(target)

    if adopt_global and LEGACY_EVENTS_FILE.exists() and not events_file.exists():
        LEGACY_EVENTS_FILE.rename(events_file)

    # Leaving an empty directory behind would make the retirement condition above unobservable.
    with suppress(OSError):
        LEGACY_DATA_DIR.rmdir()


# The write-side vocabulary. Kept as a Literal so a typo where snapshots are built is a type
# error, but never used to *parse* a stored event — see RepoSnapshot.status.
RepoStatus = Literal[
    'synced',
    'issues',
    # An action is decided and nothing would refuse it, but nothing has run — the `check` half of
    # what 'pulled'/'pushed' record for `apply`. Distinct from both: a dry run recording 'pulled'
    # would make the history claim a mutation that never happened.
    'pending',
    'pulled',
    'pushed',
    'pull_pushed',
    'cloned',
    'missing',
    'not_git',
    'no_remote',
    'path_mismatch',
    # 'we tried and git rejected it' — folded into 'missing' before, which made it
    # indistinguishable in history from a repo nobody had got round to cloning.
    'clone_failed',
    # A repo whose state could not be measured. Distinct from 'issues', which means the state
    # was read and is untidy.
    'unverified',
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
    # git's stderr for a failed outcome. Dropped before, so history recorded that a push failed
    # but never why — and by the time anyone looks, the run is long gone.
    outcome_message: str | None = None


class RepoSnapshot(BaseModel):
    name: str
    path: str
    # `str`, not RepoStatus, deliberately. The vocabulary below is the *write* contract, checked
    # by mypy where snapshots are built; a closed Literal here is a *read* contract, and it makes
    # every older syncer choke on a stream a newer one wrote — adding one status member was
    # enough to make the previous release refuse to read its own history file. An append-only
    # record read across versions has to tolerate values it does not know; stats.py already
    # falls through to the raw string for anything it does not recognise.
    status: str
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
    # Repos `apply` would act on, counted on a `check` run. Defaulted, so events written before
    # the field existed still parse.
    pending: int = 0
    issues: int
    # Repos whose state could not be established at all, counted apart from `issues`: a run
    # where nothing could be verified is not the same as a run where everything needs a push.
    failed: int = 0
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
    """Parse the run history, skipping any line that will not parse.

    Skipping rather than raising because history is a side channel: the sync report is the
    thing the user asked for, and a single unreadable line — a truncated write, or one written
    by a version this one predates — must not take the whole run down with it.
    """
    if not events_file.exists():
        return []
    events = []
    for line in events_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(SyncRunEvent.model_validate_json(line))
        except ValidationError:
            continue
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
