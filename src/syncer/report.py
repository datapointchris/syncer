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

from rich.markup import escape

from syncer.classify import ClassifyError
from syncer.classify import classify_repo
from syncer.classify import refresh_remote
from syncer.config import RepoConfig
from syncer.config import SyncerConfig
from syncer.config import ToolConfig
from syncer.config import resolve_clone_url
from syncer.config import resolve_policies
from syncer.config import resolve_policy_name
from syncer.diagnose import FailureGroup
from syncer.diagnose import group_failures
from syncer.execute import Outcome
from syncer.execute import execute
from syncer.execute import protection_refusal
from syncer.output import ICON_DOT
from syncer.output import ICON_DOWNLOAD
from syncer.output import ICON_ERR
from syncer.output import ICON_MOVE
from syncer.output import ICON_OK
from syncer.output import ICON_PULL
from syncer.output import ICON_PUSH
from syncer.output import ICON_WARN
from syncer.output import console
from syncer.output import err_console
from syncer.output import error
from syncer.output import hint
from syncer.policy import Action
from syncer.policy import BranchState
from syncer.policy import Policy
from syncer.policy import PrimaryState
from syncer.policy import decide
from syncer.policy import is_watched_remote
from syncer.repos import GitFailure
from syncer.repos import Repo
from syncer.repos import find_repo_in_search_paths
from syncer.repos import origin_mismatch

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
    # Why protection would refuse this action, computed whether or not --apply ran. Protection
    # is static config, so a report-only run can and should show it.
    blocked: str | None = None


@dataclass
class RepoBranchReport:
    label: str
    path: str
    name: str = ''
    policy_name: str | None = None
    rows: list[BranchRow] = field(default_factory=list)
    error: str | None = None
    # Git's own words for `error`, rendered under it. An error a user cannot act on is barely
    # better than no error, and every realistic cause here is named only in git's stderr.
    error_detail: str | None = None
    # Every git call that failed while building this report, for the grouped failure summary.
    failures: list[GitFailure] = field(default_factory=list)
    lifecycle: str | None = None
    lifecycle_detail: str | None = None
    # The clone's actual origin when it disagrees with the registry, and what was expected.
    # Deliberately NOT a lifecycle status: a repo pointing at the wrong upstream is otherwise
    # perfectly healthy, and a lifecycle status would replace its branch report rather than
    # annotate it.
    origin_mismatch: str | None = None
    expected_url: str = ''
    # Watched branches that exist only on origin, as (branch, age). Informational: there is no
    # local branch to sync, so this never affects severity — a repo is not unhealthy for having
    # branches you deliberately do not keep locally.
    remote_only: list[tuple[str, str]] = field(default_factory=list)
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


def _branch_line(state: BranchState, action: Action, blocked: str | None = None) -> str:
    line = f'{_branch_prefix(state)} [blue]→ {action.value}[/blue]'
    return f'{line} [yellow](would refuse: {blocked})[/yellow]' if blocked else line


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
        # A protection refusal is the guard working as configured, not something that went
        # wrong — still worth seeing (there is unpushed work on a shared branch), but it would
        # be red at the bottom of every run forever if it counted as an error.
        if row.outcome.status == 'refused' and row.blocked:
            return Severity.WARNING
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
    branch_severity = max((_row_severity(row) for row in report.rows), default=Severity.SYNCED)
    if report.origin_mismatch:
        return max(branch_severity, Severity.WARNING)
    return branch_severity


def _watched_remote_branches(repo: Repo, policy: Policy) -> list[tuple[str, str]]:
    """Remote-only branches the policy asked to watch, with each one's age.

    Skipped entirely when watch_remote is empty, so the extra git calls cost nothing for the
    policies that never opted in.
    """
    if not policy.watch_remote:
        return []
    return [(branch, repo.branch_age(f'origin/{branch}')) for branch in repo.remote_only_branches() if is_watched_remote(branch, policy)]


def build_branch_rows(repo: Repo, policy: Policy, apply: bool, *, fetched: bool = False) -> list[BranchRow]:
    """Classify → decide → (execute if apply) for every in-scope branch. Shared by both surfaces."""
    rows = []
    for state in classify_repo(repo, policy, fetched=fetched):
        action = decide(state, policy)
        outcome = execute(action, state, repo, policy) if apply else None
        blocked = protection_refusal(action, state, policy)
        rows.append(BranchRow(state=state, action=action, outcome=outcome, blocked=blocked))
    return rows


def _failed_report(repo: Repo, label: str, repo_config: RepoConfig, error: str, policy_name: str | None = None) -> RepoBranchReport:
    """A repo whose state could not be established. Never carries branch rows.

    Rows are the thing being refused: a row is a claim about a branch, and the whole point is
    that no such claim can be made. Local counts are still read so the stale-repo warnings keep
    working when only the network half failed.
    """
    detail = repo.failures[-1].stderr if repo.failures else None
    return RepoBranchReport(
        label=label,
        path=repo_config.path,
        name=repo_config.name,
        policy_name=policy_name,
        error=error,
        error_detail=detail,
        failures=list(repo.failures),
        expected_url=repo.url,
        uncommitted=len(repo.uncommitted_changes),
        stashes=repo.stash_count,
    )


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
    repo = Repo(
        name=repo_config.name,
        path=path,
        owner=owner,
        host=config.host,
        timeout=tool_config.git_timeout,
        url=resolve_clone_url(repo_config, config),
    )

    def lifecycle(status: str, detail: str | None = None) -> RepoBranchReport:
        # Carries the url and any recorded failures so a clone rejection can be grouped by cause
        # with everything else that failed this run, rather than only existing as detail text.
        return RepoBranchReport(
            label=label,
            path=repo_config.path,
            name=repo_config.name,
            lifecycle=status,
            lifecycle_detail=detail,
            failures=list(repo.failures),
            expected_url=repo.url,
        )

    if not repo.exists:
        if not include_lifecycle:
            return None
        found = find_repo_in_search_paths(repo.name, search_paths, claimed_paths)
        if found:
            return lifecycle('path_mismatch', f'found at {found} (update repos.json manually)')
        if apply:
            cloned, err = repo.clone()
            if cloned:
                return lifecycle('cloned', f'cloned to {path}')
            # Name the URL as well as the error: a wrong url_template or an empty registry
            # owner produces a URL that git rejects for reasons its message alone never
            # explains ('repository not found' reads as a permissions problem).
            detail = f'{repo.url}\n{err}' if err else f'{repo.url}\ngit clone failed with no output'
            return lifecycle('clone_failed', detail)
        return lifecycle('would_clone')
    if not repo.is_git_repo:
        return lifecycle('not_git') if include_lifecycle else None
    remotes = repo.remotes()
    if remotes is None:
        return _failed_report(repo, label, repo_config, 'cannot read this repo (git failed)')
    if not remotes:
        return lifecycle('no_remote') if include_lifecycle else None

    policy_name = resolve_policy_name(repo_config, tool_config, cli_policy)
    policy = policies.get(policy_name)
    if policy is None:
        return RepoBranchReport(
            label=label, path=repo_config.path, name=repo_config.name, policy_name=policy_name, error=f'unknown policy {policy_name!r}'
        )

    # Before classifying, not after: every branch state is measured against remote-tracking
    # refs, so a dead fetch invalidates the whole report rather than degrading it. Returning
    # here is also what refuses execution for this repo — no execute() call is constructed.
    # Before classifying, not after: every branch state is measured against remote-tracking
    # refs, so a dead fetch invalidates the whole report rather than degrading it. Returning
    # here is also what refuses execution for this repo — no execute() call is constructed.
    if refresh_remote(repo, policy) is not None:
        return _failed_report(repo, label, repo_config, 'fetch failed — sync state not verified against origin', policy_name)
    try:
        rows = build_branch_rows(repo, policy, apply, fetched=True)
    except ClassifyError as exc:
        return _failed_report(repo, label, repo_config, f'could not classify {exc.branch}', policy_name)
    return RepoBranchReport(
        label=label,
        path=repo_config.path,
        name=repo_config.name,
        policy_name=policy_name,
        rows=rows,
        uncommitted=len(repo.uncommitted_changes),
        stashes=repo.stash_count,
        origin_mismatch=origin_mismatch(repo),
        expected_url=repo.url,
        remote_only=_watched_remote_branches(repo, policy),
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


def collect_failures(reports: list[RepoBranchReport]) -> list[FailureGroup]:
    """Every git failure across the run, collapsed to one group per (cause, host)."""
    return group_failures((report.label, report.expected_url, failure) for report in reports for failure in report.failures)


def render_failure_summary(reports: list[RepoBranchReport]) -> None:
    """One block per root cause, after the per-repo output.

    The per-repo lines already carry each failure's stderr, but twenty repos behind one dead VPN
    means twenty identical blobs and no statement of the single thing to fix. This says it once,
    and it is the only place on the sync surface that tells you what to *do* — hint() existed
    but was used exclusively by the config commands.
    """
    groups = collect_failures(reports)
    if not groups:
        return
    err_console.print()
    for group in groups:
        error(f'{ICON_ERR}  {group.summary}')
        hint(f'    {", ".join(group.repos)}')
        for line in group.stderr.splitlines():
            # Every word kept — the cause is a summary and summaries lose things — but blank
            # lines dropped, since git's footer leaves a gap in the middle of the block.
            if line.strip():
                hint(f'    {escape(line)}')
        for line in group.hints:
            hint(f'  → {line}')
        err_console.print()


def render_report(report: RepoBranchReport, apply: bool) -> None:
    if report.lifecycle:
        icon, color, message, _ = LIFECYCLE_STYLE[report.lifecycle]
        console.print(f'[{color}]{icon}  {report.label} — {message}[/{color}]')
        for line in report.lifecycle_detail.splitlines() if report.lifecycle_detail else []:
            # escape(): the detail can carry raw git stderr, and Rich would swallow anything in
            # square brackets as a style tag. soft_wrap: Rich's own wrapping breaks mid-URL and
            # drops the indent on the continuation, and a URL you cannot copy is the half of the
            # diagnosis that matters.
            console.print(f'    {escape(line)}', soft_wrap=True)
        console.print()
        return
    if report.error:
        console.print(f'[red]{ICON_ERR}  {report.label} — {report.error}[/red]')
        for line in report.error_detail.splitlines() if report.error_detail else []:
            console.print(f'    {escape(line)}', soft_wrap=True)
        console.print()
        return
    mode = 'apply' if apply else 'report-only'
    console.print(f'[bold]{report.label}[/bold] [blue](policy: {report.policy_name}, {mode})[/blue]')
    if report.origin_mismatch:
        # soft_wrap: both lines are URLs, and the point of the check is to hand you two you can
        # compare and copy. Rich's own wrapping breaks them mid-path.
        console.print(f'  [yellow]{ICON_WARN}  origin is {escape(report.origin_mismatch)}[/yellow]', soft_wrap=True)
        console.print(
            f'    [yellow]registry expects {escape(report.expected_url)} (fix the remote or repos.json by hand)[/yellow]',
            soft_wrap=True,
        )
    for row in report.rows:
        if apply and row.outcome is not None:
            console.print(f'{_branch_prefix(row.state)} {_outcome_suffix(row.outcome)}')
        else:
            console.print(_branch_line(row.state, row.action, row.blocked))
    for branch, age in report.remote_only:
        # No local copy, so nothing to sync — browse it with `git log origin/<branch>`.
        console.print(f'  [blue]{ICON_DOT}  origin/{branch} — remote only, last commit {age}[/blue]')
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
    render_failure_summary(reports)
