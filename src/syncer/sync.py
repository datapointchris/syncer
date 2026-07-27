"""The default `syncer` run: the full concurrent, policy-driven sync across all repos.

Report-first — it classifies every repo/branch and shows what each policy would do, but only
mutates (pull/push/ff/clone/delete) when apply=True. Reuses the shared concurrent core in
report.py (gather_reports + render_report) and adds repo-lifecycle handling, a summary line,
per-branch event emission, and stale-repo warnings.
"""

from __future__ import annotations

import time
from collections import Counter
from datetime import UTC
from datetime import datetime
from pathlib import Path

from syncer.config import SyncerConfig
from syncer.config import ToolConfig
from syncer.policy import Action
from syncer.report import DEFAULT_JITTER_SECONDS
from syncer.report import DEFAULT_JOBS
from syncer.report import RepoBranchReport
from syncer.report import Severity
from syncer.report import gather_reports
from syncer.report import render_report
from syncer.report import report_severity
from syncer.repos import ICON_DOWNLOAD
from syncer.repos import ICON_MOVE
from syncer.repos import ICON_OK
from syncer.repos import ICON_PULL
from syncer.repos import ICON_PUSH
from syncer.repos import ICON_WARN
from syncer.repos import console
from syncer.tracking import BranchSnapshot
from syncer.tracking import RepoSnapshot
from syncer.tracking import RunSummary
from syncer.tracking import SyncRunEvent
from syncer.tracking import emit_event
from syncer.tracking import find_stale_repos
from syncer.tracking import read_events

_LIFECYCLE_TO_STATUS = {
    'would_clone': 'missing',
    'cloned': 'cloned',
    'clone_failed': 'missing',
    'path_mismatch': 'path_mismatch',
    'not_git': 'not_git',
    'no_remote': 'no_remote',
}
_ISSUE_STATUSES = {'issues', 'not_git', 'no_remote', 'path_mismatch', 'missing'}


def _operation_status(report: RepoBranchReport) -> str:
    """Status for a repo where actions ran and nothing needs attention (severity OPERATION)."""
    acted = {row.action for row in report.rows if row.outcome is not None and row.outcome.status == 'done'}
    if Action.REBASE_PUSH in acted or (acted & {Action.FAST_FORWARD, Action.PULL_FF, Action.FF_REF} and Action.PUSH in acted):
        return 'pull_pushed'
    if Action.PUSH in acted:
        return 'pushed'
    return 'pulled'


def _repo_status(report: RepoBranchReport) -> str:
    if report.lifecycle:
        return _LIFECYCLE_TO_STATUS[report.lifecycle]
    if report.error:
        return 'issues'
    severity = report_severity(report)
    if severity == Severity.SYNCED:
        return 'synced'
    if severity == Severity.OPERATION:
        return _operation_status(report)
    return 'issues'


def _snapshot(report: RepoBranchReport) -> RepoSnapshot:
    status = _repo_status(report)
    branches = [
        BranchSnapshot(
            branch=row.state.branch,
            primary=row.state.primary.value,
            ahead=row.state.ahead,
            behind=row.state.behind,
            is_default=row.state.is_default,
            is_current=row.state.is_current,
            action=row.action.value,
            outcome=row.outcome.status if row.outcome is not None else None,
        )
        for row in report.rows
    ]
    default_row = next((row for row in report.rows if row.state.is_default), None)
    return RepoSnapshot(
        name=report.name,
        path=report.path,
        status=status,  # type: ignore[arg-type]
        branch=default_row.state.branch if default_row else None,
        uncommitted=report.uncommitted,
        unpushed=default_row.state.ahead if default_row else 0,
        behind=default_row.state.behind if default_row else 0,
        stashes=report.stashes,
        policy=report.policy_name,
        branches=branches,
    )


def _summary(snapshots: list[RepoSnapshot]) -> RunSummary:
    counts = Counter(snap.status for snap in snapshots)
    issues = sum(count for status, count in counts.items() if status in _ISSUE_STATUSES)
    return RunSummary(
        total=len(snapshots),
        synced=counts['synced'],
        cloned=counts['cloned'],
        pulled=counts['pulled'],
        pushed=counts['pushed'],
        pull_pushed=counts['pull_pushed'],
        issues=issues,
        duration_ms=0,
    )


def _print_summary_line(summary: RunSummary) -> None:
    parts = [f'[green]{ICON_OK}  {summary.synced} synced[/green]']
    if summary.cloned:
        parts.append(f'[green]{ICON_DOWNLOAD}  {summary.cloned} cloned[/green]')
    if summary.pulled:
        parts.append(f'[green]{ICON_PULL}  {summary.pulled} pulled[/green]')
    if summary.pushed:
        parts.append(f'[green]{ICON_PUSH}  {summary.pushed} pushed[/green]')
    if summary.pull_pushed:
        parts.append(f'[green]{ICON_MOVE}  {summary.pull_pushed} pull+pushed[/green]')
    if summary.issues:
        parts.append(f'[yellow]{ICON_WARN}  {summary.issues} need attention[/yellow]')
    console.print('  │  '.join(parts))


def run_sync(
    config: SyncerConfig,
    tool_config: ToolConfig,
    events_file: Path,
    cli_policy: str | None = None,
    apply: bool = False,
    jobs: int = DEFAULT_JOBS,
    jitter: float = DEFAULT_JITTER_SECONDS,
) -> None:
    start = time.monotonic()
    reports = gather_reports(config, tool_config, cli_policy, apply, jobs, jitter, include_lifecycle=True)
    snapshots = [_snapshot(report) for report in reports]
    summary = _summary(snapshots)

    console.print()
    _print_summary_line(summary)
    console.print()
    # Reports are sorted synced → errors, so the repos needing attention land nearest the prompt.
    for report in reports:
        render_report(report, apply)

    summary.duration_ms = int((time.monotonic() - start) * 1000)
    event = SyncRunEvent(
        timestamp=datetime.now(UTC),
        config_name=config.owner,
        dry_run=not apply,
        repos=snapshots,
        summary=summary,
    )
    emit_event(event, events_file)

    stale = find_stale_repos(read_events(events_file))
    if stale:
        console.print()
        for repo_path, days in stale:
            console.print(f'  [yellow]{ICON_WARN}  {repo_path} has had uncommitted changes for {days} days[/yellow]')
