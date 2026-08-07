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
from syncer.output import ICON_DOWNLOAD
from syncer.output import ICON_ERR
from syncer.output import ICON_MOVE
from syncer.output import ICON_OK
from syncer.output import ICON_PULL
from syncer.output import ICON_PUSH
from syncer.output import ICON_WARN
from syncer.output import console
from syncer.output import emit_json
from syncer.policy import Action
from syncer.report import DEFAULT_JITTER_SECONDS
from syncer.report import DEFAULT_JOBS
from syncer.report import RepoBranchReport
from syncer.report import Severity
from syncer.report import collect_failures
from syncer.report import gather_reports
from syncer.report import render_failure_summary
from syncer.report import render_report
from syncer.report import report_severity
from syncer.tracking import BranchSnapshot
from syncer.tracking import RepoSnapshot
from syncer.tracking import RepoStatus
from syncer.tracking import RunSummary
from syncer.tracking import SyncRunEvent
from syncer.tracking import emit_event
from syncer.tracking import find_stale_repos
from syncer.tracking import read_events

_LIFECYCLE_TO_STATUS: dict[str, RepoStatus] = {
    'would_clone': 'missing',
    'cloned': 'cloned',
    'clone_failed': 'clone_failed',
    'path_mismatch': 'path_mismatch',
    'not_git': 'not_git',
    'no_remote': 'no_remote',
}
_ISSUE_STATUSES = {'issues', 'not_git', 'no_remote', 'path_mismatch', 'missing'}
# Counted apart from _ISSUE_STATUSES: these are runs where syncer could not find out, which is
# a different thing from finding out that a repo needs attention.
_FAILED_STATUSES = {'clone_failed', 'unverified'}


def _operation_status(report: RepoBranchReport) -> RepoStatus:
    """Status for a repo at severity OPERATION — syncer owns the next move here.

    No completed action means nothing ran, i.e. a `check`: the actions are decided and
    unobstructed but still pending. Recording one of the past-tense statuses there would put a
    mutation that never happened into the history that `stats` reads back.
    """
    acted = {row.action for row in report.rows if row.outcome is not None and row.outcome.status == 'done'}
    if not acted:
        return 'pending'
    if Action.REBASE_PUSH in acted or (acted & {Action.FAST_FORWARD, Action.PULL_FF, Action.FF_REF} and Action.PUSH in acted):
        return 'pull_pushed'
    if Action.PUSH in acted:
        return 'pushed'
    return 'pulled'


def _repo_status(report: RepoBranchReport) -> RepoStatus:
    if report.lifecycle:
        return _LIFECYCLE_TO_STATUS[report.lifecycle]
    if report.error:
        # Keyed on git having failed, not on the report being empty: an unknown policy also
        # produces no rows, but that is a config problem, not a repo syncer could not read.
        return 'unverified' if report.failures else 'issues'
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
            outcome_message=row.outcome.message or None if row.outcome is not None else None,
        )
        for row in report.rows
    ]
    default_row = next((row for row in report.rows if row.state.is_default), None)
    return RepoSnapshot(
        name=report.name,
        path=report.path,
        status=status,
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
        pending=counts['pending'],
        issues=issues,
        failed=sum(count for status, count in counts.items() if status in _FAILED_STATUSES),
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
    if summary.pending:
        # Cyan and worded as syncer's job, not yours: these are the repos `apply` clears without
        # you, and counting them under "need attention" alongside a dirty tree is what made the
        # number unactionable — you could not tell how much of it was actually yours to do.
        parts.append(f'[cyan]{ICON_MOVE}  {summary.pending} to sync[/cyan]')
    if summary.issues:
        parts.append(f'[yellow]{ICON_WARN}  {summary.issues} need you[/yellow]')
    if summary.failed:
        # Red and worded separately from `issues`: yellow "need attention" reads as "there is
        # work to do", when the truth is that syncer could not find out whether there is.
        parts.append(f'[red]{ICON_ERR}  {summary.failed} unverified[/red]')
    console.print('  │  '.join(parts))


def run_sync(
    config: SyncerConfig,
    tool_config: ToolConfig,
    events_file: Path,
    cli_policy: str | None = None,
    apply: bool = False,
    jobs: int = DEFAULT_JOBS,
    jitter: float = DEFAULT_JITTER_SECONDS,
    as_json: bool = False,
) -> list[RepoBranchReport]:
    """Run the full sync and render it. Returns the reports so the caller can set an exit code."""
    start = time.monotonic()
    reports = gather_reports(config, tool_config, cli_policy, apply, jobs, jitter, include_lifecycle=True)
    snapshots = [_snapshot(report) for report in reports]
    summary = _summary(snapshots)

    if not as_json:
        console.print()
        _print_summary_line(summary)
        console.print()
        # Reports are sorted synced → errors, so repos needing action land nearest the prompt.
        for report in reports:
            render_report(report, apply)
        render_failure_summary(reports)

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
    if as_json:
        # Built from the same snapshots the event stream records, so the JSON and the history
        # cannot disagree about what happened.
        emit_json(
            {
                'summary': summary.model_dump(),
                'repos': [snapshot.model_dump() for snapshot in snapshots],
                'failures': [
                    {
                        'cause': group.cause.value if group.cause else None,
                        'host': group.host,
                        'repos': list(group.repos),
                        'stderr': group.stderr,
                        'hints': list(group.hints),
                    }
                    for group in collect_failures(reports)
                ],
                'stale': [{'path': path, 'days': days} for path, days in stale],
            }
        )
        return reports

    if stale:
        console.print()
        for repo_path, days in stale:
            console.print(f'  [yellow]{ICON_WARN}  {repo_path} has had uncommitted changes for {days} days[/yellow]')
    return reports
