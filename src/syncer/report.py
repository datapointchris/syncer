"""Shared per-branch sync core: classify every in-scope branch, decide the action each policy
would take (read-only) or execute it (apply), and render the result.

Repos are processed concurrently on a thread pool — each repo is independent and its git
calls block on network/disk I/O with the GIL released, so wall-clock is roughly the slowest
single repo rather than the sum. All git work happens in worker threads; results are collected,
then sorted and rendered on the main thread so output never interleaves.

The pool caps concurrency at `jobs` and acts as a rolling queue: repos beyond that wait and
start as slots free up. Each worker also sleeps a small random jitter before its first git
call, so the initial burst of `jobs` fetches doesn't hit the remote at the same instant. The
jitter is bounded per task (no cumulative N×delay floor), so it never slows large repo sets.

Output is sorted by attention, least-to-most, so the repos that need action land at the bottom
nearest the prompt (least scrolling): synced → operations → warnings → errors. Within each
group repos are path-sorted.
"""

from __future__ import annotations

import random
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from dataclasses import field
from enum import IntEnum
from functools import partial
from pathlib import Path

from syncer.classify import classify_repo
from syncer.config import RepoConfig
from syncer.config import SyncerConfig
from syncer.config import ToolConfig
from syncer.config import resolve_policies
from syncer.config import resolve_policy_name
from syncer.execute import Outcome
from syncer.execute import execute
from syncer.policy import Action
from syncer.policy import BranchState
from syncer.policy import Policy
from syncer.policy import PrimaryState
from syncer.policy import decide
from syncer.repos import ICON_DOWNLOAD
from syncer.repos import ICON_ERR
from syncer.repos import ICON_MOVE
from syncer.repos import ICON_OK
from syncer.repos import ICON_PULL
from syncer.repos import ICON_PUSH
from syncer.repos import ICON_WARN
from syncer.repos import Repo
from syncer.repos import console
from syncer.repos import find_repo_in_search_paths

DEFAULT_JOBS = 16
# Upper bound on the random pre-fetch delay each worker sleeps, to desynchronize the initial
# burst of concurrent fetches. Bounded per task, so it adds at most one window of latency.
DEFAULT_JITTER_SECONDS = 0.3


class Severity(IntEnum):
    """Attention level of a repo's result. Rendered least-to-most so errors land at the
    bottom, nearest the prompt."""

    SYNCED = 0
    OPERATION = 1
    WARNING = 2
    ERROR = 3


# Lifecycle statuses for repos that never reach branch classification.
LIFECYCLE_STYLE = {
    'would_clone': (ICON_DOWNLOAD, 'cyan', 'would clone', Severity.WARNING),
    'cloned': (ICON_DOWNLOAD, 'green', 'cloned', Severity.OPERATION),
    'clone_failed': (ICON_ERR, 'red', 'clone failed', Severity.ERROR),
    'path_mismatch': (ICON_MOVE, 'yellow', 'path mismatch', Severity.WARNING),
    'not_git': (ICON_ERR, 'red', 'not a git repository', Severity.ERROR),
    'no_remote': (ICON_ERR, 'red', 'no remote', Severity.ERROR),
}

_STATE_STYLE = {
    PrimaryState.SYNCED: (ICON_OK, 'green'),
    PrimaryState.AHEAD: (ICON_PUSH, 'yellow'),
    PrimaryState.BEHIND: (ICON_PULL, 'yellow'),
    PrimaryState.DIVERGED: (ICON_MOVE, 'yellow'),
    PrimaryState.NO_UPSTREAM: (ICON_WARN, 'yellow'),
    PrimaryState.GONE: (ICON_ERR, 'red'),
    PrimaryState.DETACHED: (ICON_WARN, 'red'),
}

_STATE_LABEL = {
    PrimaryState.SYNCED: 'synced',
    PrimaryState.NO_UPSTREAM: 'no upstream',
    PrimaryState.GONE: 'gone (remote deleted)',
    PrimaryState.DETACHED: 'detached HEAD',
}

_WARNING_STATES = {PrimaryState.AHEAD, PrimaryState.BEHIND, PrimaryState.DIVERGED, PrimaryState.NO_UPSTREAM}
_ERROR_STATES = {PrimaryState.GONE, PrimaryState.DETACHED}


@dataclass
class BranchRow:
    state: BranchState
    action: Action
    outcome: Outcome | None = None


@dataclass
class RepoBranchReport:
    label: str
    path: str
    name: str = ''
    policy_name: str | None = None
    rows: list[BranchRow] = field(default_factory=list)
    error: str | None = None
    lifecycle: str | None = None
    lifecycle_detail: str | None = None
    # Repo-level counts captured for event snapshots (0 for lifecycle reports).
    uncommitted: int = 0
    stashes: int = 0


def _branch_prefix(state: BranchState) -> str:
    """The colored state part of a line (icon, branch, flags, detail) without the action arrow."""
    icon, color = _STATE_STYLE.get(state.primary, (ICON_WARN, 'blue'))

    # For ahead/behind/diverged the commit counts carry the meaning; elsewhere use a label.
    counts = []
    if state.ahead:
        counts.append(f'{state.ahead} ahead')
    if state.behind:
        counts.append(f'{state.behind} behind')
    detail_parts = counts or [_STATE_LABEL.get(state.primary, state.primary.value)]
    if state.dirty:
        detail_parts.append('dirty')
    if state.stashed:
        detail_parts.append('stashed')
    detail = ', '.join(detail_parts)

    # Parens, not brackets: Rich console markup treats [..] as style tags and would
    # swallow the text.
    flags = []
    if state.is_default:
        flags.append('default')
    if state.is_current:
        flags.append('current')
    flag_str = f' ({", ".join(flags)})' if flags else ''

    return f'  [{color}]{icon}  {state.branch}{flag_str} — {detail}[/{color}]'


def _branch_line(state: BranchState, action: Action) -> str:
    return f'{_branch_prefix(state)} [blue]→ {action.value}[/blue]'


_OUTCOME_COLOR = {
    'done': 'green',
    'refused': 'yellow',
    'failed': 'red',
    'skipped': 'blue',
    'reported': 'blue',
}


def _outcome_suffix(outcome: Outcome) -> str:
    color = _OUTCOME_COLOR.get(outcome.status, 'blue')
    text = f'{outcome.action.value}: {outcome.status}'
    if outcome.message:
        text += f' ({outcome.message})'
    return f'[{color}]→ {text}[/{color}]'


def _row_severity(row: BranchRow) -> Severity:
    # An outcome (apply mode) reflects what actually happened, so it wins over the pre-execute state.
    if row.outcome is not None:
        if row.outcome.status in ('failed', 'refused'):
            return Severity.ERROR
        if row.outcome.status == 'done':
            return Severity.OPERATION
    if row.state.primary in _ERROR_STATES:
        return Severity.ERROR
    if row.state.primary in _WARNING_STATES or row.state.dirty or row.state.stashed:
        return Severity.WARNING
    return Severity.SYNCED


def report_severity(report: RepoBranchReport) -> Severity:
    if report.error:
        return Severity.ERROR
    if report.lifecycle:
        return LIFECYCLE_STYLE[report.lifecycle][3]
    return max((_row_severity(row) for row in report.rows), default=Severity.SYNCED)


def build_branch_rows(repo: Repo, policy: Policy, apply: bool) -> list[BranchRow]:
    """Classify → decide → (execute if apply) for every in-scope branch. Shared by both surfaces."""
    rows = []
    for state in classify_repo(repo, policy):
        action = decide(state, policy)
        outcome = execute(action, state, repo) if apply else None
        rows.append(BranchRow(state=state, action=action, outcome=outcome))
    return rows


def _build_repo_report(
    repo_config: RepoConfig,
    config: SyncerConfig,
    tool_config: ToolConfig,
    policies: dict[str, Policy],
    cli_policy: str | None,
    apply: bool,
    jitter: float,
    include_lifecycle: bool,
    search_paths: list[Path],
    claimed_paths: set[Path],
) -> RepoBranchReport | None:
    """Do all git work for one repo (runs in a worker thread). Never touches the console.

    include_lifecycle=False (branches view) returns None for anything that isn't a cloned git
    repo with a remote. include_lifecycle=True (full sync) surfaces those as lifecycle reports
    and, in apply mode, clones a missing repo.
    """
    if jitter > 0:
        time.sleep(random.uniform(0, jitter))  # desync the initial burst of concurrent fetches

    path = Path(repo_config.path).expanduser()
    label = repo_config.path if repo_config.path.startswith('~') else repo_config.name
    owner = repo_config.owner or config.owner
    repo = Repo(name=repo_config.name, path=path, owner=owner, host=config.host)

    def lifecycle(status: str, detail: str | None = None) -> RepoBranchReport:
        return RepoBranchReport(label=label, path=repo_config.path, name=repo_config.name, lifecycle=status, lifecycle_detail=detail)

    if not repo.exists:
        if not include_lifecycle:
            return None
        found = find_repo_in_search_paths(repo.name, search_paths, claimed_paths)
        if found:
            return lifecycle('path_mismatch', f'found at {found} (update repos.json manually)')
        if apply:
            return lifecycle('cloned' if repo.clone() else 'clone_failed', f'cloned to {path}' if repo.exists else None)
        return lifecycle('would_clone')
    if not repo.is_git_repo:
        return lifecycle('not_git') if include_lifecycle else None
    if not repo.has_remote:
        return lifecycle('no_remote') if include_lifecycle else None

    policy_name = resolve_policy_name(repo_config, tool_config, cli_policy)
    policy = policies.get(policy_name)
    if policy is None:
        return RepoBranchReport(
            label=label, path=repo_config.path, name=repo_config.name, policy_name=policy_name, error=f'unknown policy {policy_name!r}'
        )

    rows = build_branch_rows(repo, policy, apply)
    return RepoBranchReport(
        label=label,
        path=repo_config.path,
        name=repo_config.name,
        policy_name=policy_name,
        rows=rows,
        uncommitted=len(repo.uncommitted_changes),
        stashes=repo.stash_count,
    )


def gather_reports(
    config: SyncerConfig,
    tool_config: ToolConfig,
    cli_policy: str | None = None,
    apply: bool = False,
    jobs: int = DEFAULT_JOBS,
    jitter: float = DEFAULT_JITTER_SECONDS,
    include_lifecycle: bool = False,
) -> list[RepoBranchReport]:
    """Process every active repo concurrently and return the reports sorted by
    (severity ascending, path) — synced first, errors last, path-sorted within each group."""
    policies = resolve_policies(tool_config)
    active_repos = [repo for repo in config.repos if repo.status != 'retired']
    search_paths = [Path(p).expanduser() for p in config.search_paths]
    claimed_paths = {Path(rc.path).expanduser() for rc in active_repos}

    worker = partial(
        _build_repo_report,
        config=config,
        tool_config=tool_config,
        policies=policies,
        cli_policy=cli_policy,
        apply=apply,
        jitter=jitter,
        include_lifecycle=include_lifecycle,
        search_paths=search_paths,
        claimed_paths=claimed_paths,
    )
    with ThreadPoolExecutor(max_workers=max(1, min(jobs, len(active_repos)))) as pool:
        raw = list(pool.map(worker, active_repos)) if active_repos else []

    reports = [report for report in raw if report is not None]
    reports.sort(key=lambda report: (report_severity(report), report.path))
    return reports


def render_report(report: RepoBranchReport, apply: bool) -> None:
    if report.lifecycle:
        icon, color, message, _ = LIFECYCLE_STYLE[report.lifecycle]
        console.print(f'[{color}]{icon}  {report.label} — {message}[/{color}]')
        if report.lifecycle_detail:
            console.print(f'    {report.lifecycle_detail}')
        console.print()
        return
    if report.error:
        console.print(f'[red]{report.label}: {report.error}[/red]')
        console.print()
        return
    mode = 'apply' if apply else 'report-only'
    console.print(f'[bold]{report.label}[/bold] [blue](policy: {report.policy_name}, {mode})[/blue]')
    for row in report.rows:
        if apply and row.outcome is not None:
            console.print(f'{_branch_prefix(row.state)} {_outcome_suffix(row.outcome)}')
        else:
            console.print(_branch_line(row.state, row.action))
    console.print()


def report_branches(
    config: SyncerConfig,
    tool_config: ToolConfig,
    cli_policy: str | None = None,
    apply: bool = False,
    jobs: int = DEFAULT_JOBS,
    jitter: float = DEFAULT_JITTER_SECONDS,
) -> None:
    reports = gather_reports(config, tool_config, cli_policy, apply, jobs, jitter)  # include_lifecycle defaults False
    console.print()
    for report in reports:
        render_report(report, apply)
