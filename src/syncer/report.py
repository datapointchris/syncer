"""Per-branch sync report. Classifies every in-scope branch and shows the action each
policy would take (read-only), or executes it (--apply).

Repos are processed concurrently on a thread pool — each repo is independent and its git
calls block on network/disk I/O with the GIL released, so wall-clock is roughly the
slowest single repo rather than the sum. All git work happens in worker threads; the
results are rendered on the main thread in config (path) order so output never interleaves.

The pool caps concurrency at `jobs` and acts as a rolling queue: repos beyond that wait and
start as slots free up. Each worker also sleeps a small random jitter before its first git
call, so the initial burst of `jobs` fetches doesn't hit the remote at the same instant. The
jitter is bounded per task (no cumulative N×delay floor), so it never slows large repo sets.
"""

from __future__ import annotations

import random
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
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
from syncer.repos import ICON_ERR
from syncer.repos import ICON_MOVE
from syncer.repos import ICON_OK
from syncer.repos import ICON_PULL
from syncer.repos import ICON_PUSH
from syncer.repos import ICON_WARN
from syncer.repos import Repo
from syncer.repos import console

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


DEFAULT_JOBS = 16
# Upper bound on the random pre-fetch delay each worker sleeps, to desynchronize the initial
# burst of concurrent fetches. Bounded per task, so it adds at most one window of latency.
DEFAULT_JITTER_SECONDS = 0.3


@dataclass
class BranchRow:
    state: BranchState
    action: Action
    outcome: Outcome | None = None


@dataclass
class RepoBranchReport:
    label: str
    policy_name: str
    rows: list[BranchRow]
    error: str | None = None


def _build_repo_report(
    repo_config: RepoConfig,
    config: SyncerConfig,
    tool_config: ToolConfig,
    policies: dict[str, Policy],
    cli_policy: str | None,
    apply: bool,
    jitter: float,
) -> RepoBranchReport | None:
    """Do all git work for one repo (runs in a worker thread). Returns None to skip a repo
    that isn't a cloned git repo with a remote. Never touches the console."""
    if jitter > 0:
        time.sleep(random.uniform(0, jitter))  # desync the initial burst of concurrent fetches

    path = Path(repo_config.path).expanduser()
    label = repo_config.path if repo_config.path.startswith('~') else repo_config.name
    owner = repo_config.owner or config.owner
    repo = Repo(name=repo_config.name, path=path, owner=owner, host=config.host)

    if not repo.exists or not repo.is_git_repo or not repo.has_remote:
        return None

    policy_name = resolve_policy_name(repo_config, tool_config, cli_policy)
    policy = policies.get(policy_name)
    if policy is None:
        return RepoBranchReport(label=label, policy_name=policy_name, rows=[], error=f'unknown policy {policy_name!r}')

    rows = []
    for state in classify_repo(repo, policy):
        action = decide(state, policy)
        outcome = execute(action, state, repo) if apply else None
        rows.append(BranchRow(state=state, action=action, outcome=outcome))
    return RepoBranchReport(label=label, policy_name=policy_name, rows=rows)


def _render_report(report: RepoBranchReport, apply: bool) -> None:
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
    policies = resolve_policies(tool_config)
    active_repos = [repo for repo in config.repos if repo.status != 'retired']

    worker = partial(
        _build_repo_report,
        config=config,
        tool_config=tool_config,
        policies=policies,
        cli_policy=cli_policy,
        apply=apply,
        jitter=jitter,
    )
    # ThreadPoolExecutor.map preserves input order; active_repos is already path-sorted
    # (config load sorts by path), so reports render in directory order.
    with ThreadPoolExecutor(max_workers=max(1, min(jobs, len(active_repos)))) as pool:
        reports = list(pool.map(worker, active_repos)) if active_repos else []

    console.print()
    for report in reports:
        if report is not None:
            _render_report(report, apply)
