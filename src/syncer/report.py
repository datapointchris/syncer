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

Collecting before rendering is what a live progress display pays for: the workers report
themselves as they start and finish, so the wait is legible — how far in, which repos are in
flight, how long each has been going — while the report itself stays sorted and arrives whole.
Streaming each result as it landed was the alternative, and it loses the sort, which is the
only reason the important rows are the ones nearest the prompt.

Output is sorted by attention, least-to-most, so the repos that need action land at the bottom
nearest the prompt (least scrolling): synced → operations → warnings → errors. Within each
group repos are path-sorted. Only the repos above SYNCED are rendered unless `-v` asks for the
rest: a registry is mostly synced on any ordinary day, and a report you have to scroll to read
is one where the four rows that mattered were indistinguishable from the seventy that did not.
"""

from __future__ import annotations

import random
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
from dataclasses import dataclass
from dataclasses import field
from enum import IntEnum
from functools import partial
from pathlib import Path

from rich.markup import escape

from syncer.breaker import HostBreaker
from syncer.breaker import Trip
from syncer.classify import ClassifyError
from syncer.classify import classify_repo
from syncer.classify import refresh_remote
from syncer.config import RepoConfig
from syncer.config import SyncerConfig
from syncer.config import ToolConfig
from syncer.config import resolve_clone_url
from syncer.config import resolve_policies
from syncer.config import resolve_policy_name
from syncer.diagnose import Cause
from syncer.diagnose import FailureGroup
from syncer.diagnose import group_failures
from syncer.diagnose import hint_lines
from syncer.execute import MUTATING_ACTIONS
from syncer.execute import Outcome
from syncer.execute import Refusal
from syncer.execute import blocking_refusal
from syncer.execute import describe_block
from syncer.execute import execute
from syncer.output import ICON_DOT
from syncer.output import ICON_DOWNLOAD
from syncer.output import ICON_ERR
from syncer.output import ICON_MOVE
from syncer.output import ICON_OK
from syncer.output import ICON_PULL
from syncer.output import ICON_PUSH
from syncer.output import ICON_WARN
from syncer.output import Tally
from syncer.output import console
from syncer.output import emit_json
from syncer.output import err_console
from syncer.output import error
from syncer.output import hint
from syncer.policy import Action
from syncer.policy import BranchState
from syncer.policy import Policy
from syncer.policy import PrimaryState
from syncer.policy import decide
from syncer.policy import is_watched_remote
from syncer.progress import RunProgress
from syncer.remedy import Remedy
from syncer.remedy import remedy_for
from syncer.repos import GitFailure
from syncer.repos import Repo
from syncer.repos import abort_running_commands
from syncer.repos import find_repo_in_search_paths
from syncer.repos import origin_mismatch
from syncer.repos import reset_abort

DEFAULT_JOBS = 16
# Upper bound on the random pre-fetch delay each worker sleeps, to desynchronize the initial
# burst of concurrent fetches. Bounded per task, so it adds at most one window of latency.
DEFAULT_JITTER_SECONDS = 0.3


class Severity(IntEnum):
    """Who has to do something about a result, least to most. Rendered in this order so the
    rows needing hands land at the bottom, nearest the prompt.

    The axis is deliberately *ownership*, not git state: a branch that is `behind` is not a
    problem, it is queued work syncer performs for you under `apply`, and coloring it like a
    diverged tree trains you to ignore the color. OPERATION is that band — an action is
    decided and nothing would refuse it — and it applies in `check` as much as in `apply`.
    """

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

# Icon says *what* the branch is; color says *who has to act* (below). Splitting the two is
# what fixes a dirty-but-synced row rendering as a green tick indistinguishable from a clean
# one — it kept its icon and color from the primary state alone, while the count in the summary
# line and the sort position both came from the severity that knew better.
_STATE_ICON = {
    PrimaryState.SYNCED: ICON_OK,
    PrimaryState.AHEAD: ICON_PUSH,
    PrimaryState.BEHIND: ICON_PULL,
    PrimaryState.DIVERGED: ICON_MOVE,
    PrimaryState.NO_UPSTREAM: ICON_WARN,
    PrimaryState.GONE: ICON_ERR,
    PrimaryState.DETACHED: ICON_WARN,
}

_SEVERITY_COLOR = {
    Severity.SYNCED: 'green',
    Severity.OPERATION: 'cyan',
    Severity.WARNING: 'yellow',
    Severity.ERROR: 'red',
}

# Substituted when the state's own icon understates the severity — only the benign tick can,
# since every other state icon already reads as "look at this".
_SEVERITY_ICON = {
    Severity.WARNING: ICON_WARN,
    Severity.ERROR: ICON_ERR,
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
    # Why `apply` would refuse this action, computed whether or not it ran: protection (static
    # config), a linked worktree holding the branch, or a dirty working tree (read once per repo).
    # All three are knowable without executing, so a report-only run can and should show them
    # rather than promising an action apply will decline. Anything a guard can only discover
    # mid-write stays absent here. Keyed, not prose — the remedy joins on it.
    blocked: Refusal | None = None
    # The same refusal as prose, for display only. The Outcome.reason / Outcome.message split,
    # for the same reason: the key is what severity and the remedy join on, and nothing joins on
    # a sentence. Held rather than derived because only the row builder has the policy the
    # protected pattern comes from.
    blocked_message: str = ''
    # What the reader can run themselves, for a row syncer has decided not to clear. Empty
    # wherever `apply` would handle it: a command printed beside queued work invites a race.
    remedy: Remedy = field(default_factory=Remedy)


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
    # Set when this repo was never contacted because its host had already failed this run. It is
    # an error like any other unmeasurable repo, but it is not rendered per repo: the cause is
    # already stated once in the failure summary, and repeating it sixty times is the wall of
    # noise the skip exists to prevent.
    skipped: Trip | None = None


def is_unverified(report: RepoBranchReport) -> bool:
    """True when this repo's state could not be established, as opposed to established and untidy.

    The distinction the summary line draws in red rather than yellow, and the one place it is
    decided. `_repo_status` and the live display both read it, so the count climbing during the
    run and the count sitting in the summary afterwards cannot disagree about which band a repo
    landed in. An unknown policy is deliberately *not* unverified: nothing was measured, but
    nothing failed either — that is a config problem, and git was never asked.
    """
    if report.skipped is not None:
        return True
    if report.lifecycle:
        return report.lifecycle == 'clone_failed'
    return bool(report.error and report.failures)


def _state_severity(state: BranchState) -> Severity:
    """Severity of the branch state alone, before who-acts-on-it is considered."""
    if state.primary in _ERROR_STATES:
        return Severity.ERROR
    if state.primary in _WARNING_STATES:
        return Severity.WARNING
    return Severity.SYNCED


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
    # Before the action band, and regardless of the branch state: a dirty tree is the one thing
    # syncer will never resolve for you, and it refuses every mutator that touches the tree. A
    # `behind` branch you cannot fast-forward because of uncommitted work is your problem, not
    # queued work.
    if row.state.dirty or row.state.stashed:
        return Severity.WARNING
    # An action syncer will actually perform, with nothing standing in its way: `apply` clears
    # this without you. It sorts above synced so you can see it, and below the rows needing hands.
    if row.action in MUTATING_ACTIONS and not row.blocked:
        return Severity.OPERATION
    return _state_severity(row.state)


def _row_style(row: BranchRow) -> tuple[str, str]:
    """Icon and color for a row: icon from the branch state, color from who has to act."""
    severity = _row_severity(row)
    icon = _STATE_ICON.get(row.state.primary, ICON_WARN)
    return _SEVERITY_ICON.get(severity, icon) if icon == ICON_OK else icon, _SEVERITY_COLOR[severity]


def _branch_prefix(row: BranchRow, uncommitted: int = 0, stashes: int = 0) -> str:
    """The colored state part of a line (icon, branch, flags, detail), without the action arrow."""
    state = row.state
    icon, color = _row_style(row)

    # For ahead/behind/diverged the commit counts carry the meaning; elsewhere use a label.
    counts = []
    if state.ahead:
        counts.append(f'{state.ahead} ahead')
    if state.behind:
        counts.append(f'{state.behind} behind')
    detail_parts = counts or [_STATE_LABEL.get(state.primary, state.primary.value)]
    # Named with its count rather than the bare word 'dirty': the count is the difference between
    # a stray generated file and a day's work, and it is already read for the event snapshot.
    if state.dirty:
        detail_parts.append(f'{uncommitted} uncommitted' if uncommitted else 'uncommitted changes')
    if state.stashed:
        detail_parts.append(f'{stashes} stashed' if stashes else 'stashed')
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


def _branch_line(row: BranchRow, uncommitted: int = 0, stashes: int = 0) -> str:
    """A report-only row. The arrow appears only when syncer will actually do something.

    `skip`, `report` and `prompt` all mean "syncer changes nothing here", which the line already
    conveys by existing — printing `→ skip` after every clean repo spent a column on the least
    informative word in the vocabulary and made the arrow meaningless where it mattered.
    """
    line = _branch_prefix(row, uncommitted, stashes)
    if row.action in MUTATING_ACTIONS:
        line = f'{line} [blue]→ {row.action.value}[/blue]'
    if not row.blocked:
        return line
    return f'{line} [yellow](would refuse: {row.blocked_message or row.blocked.value})[/yellow]'


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


def _apply_line(row: BranchRow, uncommitted: int = 0, stashes: int = 0) -> str:
    """An executed row, under the same rule `_branch_line` follows: say nothing when nothing happened.

    A non-mutating action can only produce `skipped` or `reported` — SKIP/REPORT/PROMPT are all in
    PROTECTED_ALLOWED, so none of them can be refused, and `blocked` is None for all three. So
    `→ skip: skipped` after every clean repo was the same empty column the report path already
    dropped, restated as an outcome: 79 of 80 rows on a synced run, which is what cost the arrow
    its meaning on the row that had one.

    A message is the sole exception, because only an executed run can know it — that is what apply
    legitimately adds over check, so it is kept even for an action that changed nothing.
    """
    outcome = row.outcome
    if outcome is None or (outcome.action not in MUTATING_ACTIONS and not outcome.message):
        return _branch_line(row, uncommitted, stashes)
    return f'{_branch_prefix(row, uncommitted, stashes)} {_outcome_suffix(outcome)}'


def report_severity(report: RepoBranchReport) -> Severity:
    if report.error:
        return Severity.ERROR
    if report.lifecycle:
        return LIFECYCLE_STYLE[report.lifecycle][3]
    branch_severity = max((_row_severity(row) for row in report.rows), default=Severity.SYNCED)
    if report.origin_mismatch:
        return max(branch_severity, Severity.WARNING)
    return branch_severity


def attention_tally(report: RepoBranchReport) -> Tally | None:
    """Which counter this repo belongs to, or None when it is simply synced.

    Severity alone cannot answer it. ERROR spans both a branch that is genuinely wrong and a repo
    git could not be asked about, and the summary line has always separated those — so a display
    keyed on severity had no honest word for the band and invented a fourth one nobody else prints.
    """
    severity = report_severity(report)
    if severity is Severity.SYNCED:
        return None
    if severity is Severity.OPERATION:
        return Tally.TO_SYNC
    return Tally.UNVERIFIED if is_unverified(report) else Tally.NEEDS_YOU


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
    # Read once, not per row: is_dirty shells out to `git status` every call, and it is the same
    # tree for every branch. Repo-wide, matching what the execute-time guards actually consult —
    # state.dirty is scoped to the current branch, so it would under-report the refusal.
    dirty = repo.is_dirty
    rows = []
    for state in classify_repo(repo, policy, fetched=fetched):
        action = decide(state, policy)
        outcome = execute(action, state, repo, policy) if apply else None
        blocked = blocking_refusal(action, state, policy, dirty)
        rows.append(
            BranchRow(
                state=state,
                action=action,
                outcome=outcome,
                blocked=blocked,
                blocked_message=describe_block(blocked, action, state, policy) if blocked else '',
                remedy=remedy_for(state, action, str(repo.path), blocked, outcome.reason if outcome else None),
            )
        )
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
        # Still computed, because it needs no network and may be the reason the fetch failed:
        # a repo pointing at a host you have no credential for fails exactly like this, and
        # "fetch failed" alone would send you to debug the network instead of the remote.
        origin_mismatch=origin_mismatch(repo),
        uncommitted=len(repo.uncommitted_changes),
        stashes=repo.stash_count,
    )


def _label(repo_config: RepoConfig) -> str:
    """How a repo is named on screen: its path when that says where it is, else its name."""
    return repo_config.path if repo_config.path.startswith('~') else repo_config.name


def _skipped_report(repo_config: RepoConfig, trip: Trip, url: str) -> RepoBranchReport:
    """A repo syncer deliberately did not contact, because its host had already failed."""
    return RepoBranchReport(
        label=_label(repo_config),
        path=repo_config.path,
        name=repo_config.name,
        error=f'not checked — {trip.summary} failed earlier this run',
        skipped=trip,
        expected_url=url,
    )


def _crashed_report(repo_config: RepoConfig, exc: Exception) -> RepoBranchReport:
    """A repo whose processing raised, turned into an ordinary error report.

    One repo must never take the run down with it. Every future is collected before anything is
    rendered, so an exception escaping a worker takes every *other* repo's report with it — the
    whole registry measured, a traceback printed, and not one line about the repos that were fine.
    That is the same failure as a dead fetch reporting `synced`, in the other direction.
    """
    return RepoBranchReport(
        label=_label(repo_config),
        path=repo_config.path,
        name=repo_config.name,
        error='syncer failed on this repo',
        error_detail=f'{type(exc).__name__}: {exc}',
    )


def _occupied_note(path: Path) -> str:
    """What is at stake, since a bare `would clone` against an occupied path reads as an oversight."""
    try:
        entries = len(list(path.iterdir()))
    except OSError:
        return 'something is already here — apply clones into it and keeps what it holds'
    noun = 'entry' if entries == 1 else 'entries'
    return f'a directory holding {entries} {noun} is already here — apply clones into it and keeps them'


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
    breaker: HostBreaker,
) -> RepoBranchReport | None:
    """Do all git work for one repo (runs in a worker thread). Never touches the console.

    include_lifecycle=False (branches view) returns None for anything that isn't a cloned git
    repo with a remote. include_lifecycle=True (full sync) surfaces those as lifecycle reports
    and, in apply mode, clones a missing repo.

    `breaker` is required rather than defaulted. A per-repo fallback instance would be the
    credential storm restored — every repo asking a host that had already refused, with nothing on
    screen distinguishing that run from a working one.
    """
    if jitter > 0:
        time.sleep(random.uniform(0, jitter))  # desync the initial burst of concurrent fetches

    path = Path(repo_config.path).expanduser()
    label = _label(repo_config)
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

    # A directory standing where a repo belongs is a repo waiting to be cloned, not a broken one:
    # setting a machine up seeds its gitignored files before the clones exist, and `git clone`
    # declines a non-empty destination outright.
    occupied = repo.exists and not repo.is_git_repo
    if occupied and (path / '.git').exists():
        # Git state syncer will not write into — a linked worktree's .git file, or one it cannot
        # read. Reserved for that, so `not_git` now means something syncer has no clone for.
        return lifecycle('not_git') if include_lifecycle else None
    if not repo.is_git_repo:
        if not include_lifecycle:
            return None
        found = find_repo_in_search_paths(repo.name, search_paths, claimed_paths)
        if found:
            return lifecycle('path_mismatch', f'found at {found} (update repos.json manually)')
        if apply:
            # repo.url, not contacted_url: there is no clone yet, so there is no origin to read —
            # and asking git for one inside a directory that does not exist raises.
            trip = breaker.trip_for(repo.url)
            if trip is not None:
                return _skipped_report(repo_config, trip, repo.url)
            cloned, err = repo.clone_into_existing() if occupied else repo.clone()
            if cloned:
                breaker.record_success(repo.url)
                # Which mechanism ran: a clone into an occupied directory can come out dirty on
                # files that were already there, and a plain one never can.
                landed = f'cloned into the existing directory at {path}' if occupied else f'cloned to {path}'
                return lifecycle('cloned', landed)
            if repo.failures:
                breaker.record_failure(repo.url, repo.failures[-1])
            # Name the URL as well as the error: a wrong url_template or an empty registry
            # owner produces a URL that git rejects for reasons its message alone never
            # explains ('repository not found' reads as a permissions problem).
            detail = f'{repo.url}\n{err}' if err else f'{repo.url}\ngit clone failed with no output'
            return lifecycle('clone_failed', detail)
        return lifecycle('would_clone', _occupied_note(path) if occupied else None)
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

    # Asked before the fetch, so a host that has already refused this run costs nothing further.
    # Keyed on where this clone really points, which is not always where the registry says.
    trip = breaker.trip_for(repo.contacted_url)
    if trip is not None:
        return _skipped_report(repo_config, trip, repo.contacted_url)

    # Before classifying, not after: every branch state is measured against remote-tracking
    # refs, so a dead fetch invalidates the whole report rather than degrading it. Returning
    # here is also what refuses execution for this repo — no execute() call is constructed.
    fetch_failure = refresh_remote(repo, policy)
    if fetch_failure is not None:
        breaker.record_failure(repo.contacted_url, fetch_failure)
        return _failed_report(repo, label, repo_config, 'fetch failed — sync state not verified against origin', policy_name)
    breaker.record_success(repo.contacted_url)
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
    show_progress: bool = False,
) -> list[RepoBranchReport]:
    """Process every active repo concurrently and return the reports sorted by
    (severity ascending, path) — synced first, errors last, path-sorted within each group.

    Results are collected as each repo finishes rather than in submission order, purely so the
    progress display can count them as they land; the sort at the end is total on path, so the
    rendered order does not depend on which repo won a race.

    A KeyboardInterrupt ends the run rather than the current call. Nothing is rendered and no run
    event is written: a partial sweep recorded as a run is a fact about a machine that was never
    measured, and `stats` would read it back as one.
    """
    policies = resolve_policies(tool_config)
    active_repos = [repo for repo in config.repos if repo.status != 'retired']
    if not active_repos:
        return []
    search_paths = [Path(p).expanduser() for p in config.search_paths]
    claimed_paths = {Path(rc.path).expanduser() for rc in active_repos}

    breaker = HostBreaker()
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
        breaker=breaker,
    )

    reset_abort()
    reports: list[RepoBranchReport] = []
    with RunProgress(len(active_repos), enabled=show_progress) as progress:

        def run_one(repo_config: RepoConfig) -> RepoBranchReport | None:
            # The bare name, not the report's label: a label is a path so the report says where
            # the repo is, and four of those overflow one line into a single ellipsised entry —
            # which loses the only thing the line is for, naming what is slow.
            token = progress.start(repo_config.name)
            report: RepoBranchReport | None = None
            try:
                report = worker(repo_config)
            except Exception as exc:
                report = _crashed_report(repo_config, exc)
            finally:
                progress.finish(token, attention_tally(report) if report is not None else None)
            return report

        pool = ThreadPoolExecutor(max_workers=max(1, min(jobs, len(active_repos))))
        futures = [pool.submit(run_one, repo_config) for repo_config in active_repos]
        try:
            for future in as_completed(futures):
                report = future.result()
                if report is not None:
                    reports.append(report)
        except KeyboardInterrupt:
            # Both halves matter: cancel() empties the queue, and the abort ends the git calls
            # already running. Without the second, shutdown waits out every in-flight fetch and a
            # Ctrl-C looks ignored for the length of git_timeout.
            abort_running_commands()
            for future in futures:
                future.cancel()
            raise
        finally:
            pool.shutdown(wait=True)

    reports.sort(key=lambda report: (report_severity(report), report.path))
    return reports


def needs_attention(report: RepoBranchReport) -> bool:
    """Whether this repo has anything to say. The default view renders only these.

    Severity is the test, so the rule is the same one the sort and the summary line already use:
    anything above SYNCED is either work syncer will do or work you have to. The exception is a
    watched remote-only branch, which deliberately never affects severity — it is opt-in, and a
    branch someone asked to be told about should not need a flag to appear.
    """
    return report_severity(report) > Severity.SYNCED or bool(report.remote_only)


def visible_reports(reports: list[RepoBranchReport], verbose: bool) -> list[RepoBranchReport]:
    """The reports to render: everything under `-v`, only what needs attention otherwise.

    A skipped repo is excluded at *both* levels, `-v` included. Its whole explanation is the
    one cause named in the failure summary, and one line per repo is the noise the skip exists to
    prevent — sixty of them is what a closed host would produce.
    """
    candidates = [report for report in reports if report.skipped is None]
    return candidates if verbose else [report for report in candidates if needs_attention(report)]


def hidden_count(reports: list[RepoBranchReport], visible: list[RepoBranchReport]) -> int:
    """How many repos were left out, excluding the ones the failure summary already accounts for.

    Counting skipped repos here stated the same repos twice under two framings, and offered a flag
    that would reveal repos syncer never measured.
    """
    return len([report for report in reports if report.skipped is None]) - len(visible)


def render_hidden_note(hidden: int) -> None:
    """Say how many repos were left out, so a short report is never mistaken for a short registry."""
    if hidden > 0:
        console.print(f'[blue]  {hidden} repos not shown — [cyan]-v[/cyan] shows every repo[/blue]')


def _branch_json(report: RepoBranchReport) -> dict:
    """One repo as plain data. Mirrors what render_report shows, including why it failed."""
    return {
        'name': report.name,
        'path': report.path,
        'policy': report.policy_name,
        'error': report.error,
        'error_detail': report.error_detail,
        'origin_mismatch': report.origin_mismatch,
        'skipped': {'host': report.skipped.host, 'cause': report.skipped.cause.value} if report.skipped else None,
        'branches': [
            {
                'branch': row.state.branch,
                'state': row.state.primary.value,
                'ahead': row.state.ahead,
                'behind': row.state.behind,
                'is_default': row.state.is_default,
                'is_current': row.state.is_current,
                'dirty': row.state.dirty,
                'worktree': row.state.worktree,
                'action': row.action.value,
                'blocked': row.blocked_message or None,
                'blocked_reason': row.blocked.value if row.blocked else None,
                'remedy': {'commands': list(row.remedy.commands), 'notes': list(row.remedy.notes)} if row.remedy else None,
                'outcome': row.outcome.status if row.outcome else None,
                'outcome_message': row.outcome.message or None if row.outcome else None,
            }
            for row in report.rows
        ],
    }


def collect_failures(reports: list[RepoBranchReport]) -> list[FailureGroup]:
    """Every git failure across the run, collapsed to one group per (cause, host).

    Grouped on the host git actually talked to: `origin_mismatch` holds the clone's real origin
    whenever it differs from the registry, and where it does not, `expected_url` is that same
    URL. Grouping on the registry's expectation alone would file a corporate host's outage under
    github.com and offer a hint for the wrong machine.
    """
    return group_failures(
        (report.label, report.origin_mismatch or report.expected_url, failure) for report in reports for failure in report.failures
    )


def render_failure_summary(reports: list[RepoBranchReport]) -> None:
    """One block per root cause, after the per-repo output.

    The per-repo lines already carry each failure's stderr, but twenty repos behind one dead VPN
    means twenty identical blobs and no statement of the single thing to fix. This says it once,
    and it is the only place on the sync surface that tells you what to *do* — hint() existed
    but was used exclusively by the config commands.

    It is also where the repos syncer skipped are accounted for. They are folded into the block
    for the failure that closed their host, because that is the whole of their explanation: one
    cause, the repos that proved it, and the count that never got asked.
    """
    groups = collect_failures(reports)
    skipped: Counter[tuple[Cause, str]] = Counter((report.skipped.cause, report.skipped.host) for report in reports if report.skipped)
    if not groups and not skipped:
        return
    err_console.print()
    for group in groups:
        error(f'{ICON_ERR}  {group.summary}')
        hint(f'    {", ".join(group.repos)}')
        # A group with no cause can never own skipped repos: the breaker refuses to trip on a
        # failure diagnose declined to name, so nothing was ever skipped on its account.
        count = skipped.pop((group.cause, group.host), 0) if group.cause is not None else 0
        if count:
            hint(f'    {count} more repo{"s" if count != 1 else ""} on {group.host} not contacted after this')
        for line in group.stderr.splitlines():
            # Every word kept — the cause is a summary and summaries lose things — but blank
            # lines dropped, since git's footer leaves a gap in the middle of the block.
            if line.strip():
                hint(f'    {escape(line)}')
        for line in group.hints:
            hint(f'  → {line}')
        err_console.print()
    # A skip whose closing failure produced no group of its own. Every path that trips the breaker
    # also records the failure onto a report, so this does not fire today — it is here because
    # repos nobody contacted must never become a silent fact, and that has to hold under a change
    # nobody has made yet. It carries the same hints as a group, since a cause with no next command
    # is the half of a diagnosis you cannot act on.
    for (cause, host), count in skipped.items():
        error(f'{ICON_ERR}  {count} repos on {host} not contacted: {cause.value.replace("_", " ")} earlier this run')
        for line in hint_lines(cause, host):
            hint(f'  → {line}')
        err_console.print()


def shorten_home(text: str) -> str:
    """Paths under $HOME written the way the row above already writes them.

    The registry stores `~/dotfiles` and the header prints that, while a remedy is built from the
    expanded path — so one block spelled the same directory two ways, and the longer spelling put
    `git worktree remove <worktree>` past 90 columns. soft_wrap keeps Rich from breaking it, but
    nothing keeps the terminal from doing so, and a command that wraps is one that gets
    half-copied. That is not hypothetical: it cost a leading `git`, and the shell then found a
    different `worktree` on PATH and reported the subcommand as invalid.

    Display only. Nothing joins on these strings, and the paths the guards use are resolved from
    git at write time.
    """
    return text.replace(f'{Path.home()}/', '~/')


def render_remedy(remedy: Remedy) -> None:
    """The commands under the row they belong to, on stdout with it.

    Not through hint(): that writes to stderr, which is right for the failure summary — a block
    about the run rather than about a repo — and wrong here, because a redirected report would
    keep every row and lose the line telling you what to do about it, and the two would interleave
    on the terminal besides.

    soft_wrap because a command that wrapped is one that cannot be double-clicked, and escape()
    because a branch name may legally contain the square brackets Rich reads as a style tag.
    """
    for command in remedy.commands:
        console.print(f'      [cyan]$ {escape(shorten_home(command))}[/cyan]', soft_wrap=True)
    for note in remedy.notes:
        console.print(f'      [blue]{escape(shorten_home(note))}[/blue]', soft_wrap=True)


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
        if report.origin_mismatch:
            # Printed here too: a repo pointing at a host you have no credential for fails
            # exactly like a network problem, and this is the line that tells them apart.
            console.print(f'    [yellow]origin is {escape(report.origin_mismatch)}[/yellow]', soft_wrap=True)
            console.print(f'    [yellow]registry expects {escape(report.expected_url)}[/yellow]', soft_wrap=True)
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
    render_row = _apply_line if apply else _branch_line
    for row in report.rows:
        console.print(render_row(row, report.uncommitted, report.stashes))
        render_remedy(row.remedy)
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
    as_json: bool = False,
    verbose: bool = False,
) -> list[RepoBranchReport]:
    """Per-branch view. Returns the reports so the caller can set an exit code."""
    # include_lifecycle defaults False; progress is a terminal affordance and would corrupt --json.
    reports = gather_reports(config, tool_config, cli_policy, apply, jobs, jitter, show_progress=not as_json)
    if as_json:
        emit_json({'repos': [_branch_json(report) for report in reports]})
        return reports
    console.print()
    visible = visible_reports(reports, verbose)
    for report in visible:
        render_report(report, apply)
    render_hidden_note(hidden_count(reports, visible))
    render_failure_summary(reports)
    return reports


def exit_code_for(reports: list[RepoBranchReport]) -> int:
    """1 if any repo reached ERROR, else 0. One rule, stated once, so it cannot drift.

    Report-only and --apply share it; they differ only in which severities they can produce.
    WARNING stays 0 deliberately — a repo that is `ahead` is the normal state of a machine
    somebody works on, and an exit code that is non-zero every day is one nobody can automate
    against, which is the same reason `issues` printing "N found" and exiting 0 was useless.
    """
    return 1 if any(report_severity(report) is Severity.ERROR for report in reports) else 0
