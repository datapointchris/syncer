"""`syncer fetch`: refresh every clone's remote-tracking refs and tags, and say what moved.

The fetch is `refresh_remote`, the one `check` and `apply` run before classifying, so there is no
second fetch path to drift from theirs. It writes only under `refs/remotes/` and new tags. It never
touches a working tree, the index, HEAD or a local branch, and it never pushes or clones. That is
also why it never runs `git status`, which can rewrite the index to refresh its stat cache.

Tags keep the invariant every verb keeps: a tag origin moved is named and never overwritten. A new
tag still arrives, because git refuses only the moved one and fetches the rest.

No run event is written. `stats` reads events as measurements of branch state, and this verb
classifies no branch.

A clean run prints one line. It is built to run unattended every few minutes, and whatever it
prints lands in a log each time.
"""

from __future__ import annotations

import random
import time
from collections import Counter
from functools import partial
from pathlib import Path

from rich.markup import escape

from syncer.breaker import HostBreaker
from syncer.classify import refresh_remote
from syncer.config import RepoConfig
from syncer.config import SyncerConfig
from syncer.config import ToolConfig
from syncer.config import resolve_clone_url
from syncer.config import resolve_policies
from syncer.config import resolve_policy_name
from syncer.output import ICON_DOT
from syncer.output import ICON_ERR
from syncer.output import ICON_OK
from syncer.output import ICON_PULL
from syncer.output import console
from syncer.output import emit_json
from syncer.policy import Policy
from syncer.report import DEFAULT_JITTER_SECONDS
from syncer.report import DEFAULT_JOBS
from syncer.report import RepoBranchReport
from syncer.report import Severity
from syncer.report import active_repo_configs
from syncer.report import failures_json
from syncer.report import render_failure_summary
from syncer.report import render_report
from syncer.report import render_tag_findings
from syncer.report import report_severity
from syncer.report import run_pool
from syncer.report import screen_label
from syncer.report import skipped_report
from syncer.repos import RefMoves
from syncer.repos import Repo
from syncer.repos import origin_mismatch

UPDATED = 'updated'
CURRENT = 'current'


def unfetched(repo: Repo, label: str, repo_config: RepoConfig, error: str, policy_name: str | None = None) -> RepoBranchReport:
    """A repo that could not be fetched, carrying git's own words for why.

    `failed_report` without its local counts, because reading those runs `git status`.
    """
    return RepoBranchReport(
        label=label,
        path=repo_config.path,
        name=repo_config.name,
        policy_name=policy_name,
        error=error,
        error_detail=repo.failures[-1].stderr if repo.failures else None,
        failures=list(repo.failures),
        expected_url=repo.url,
        origin_mismatch=origin_mismatch(repo),
    )


def build_fetch_report(
    repo_config: RepoConfig,
    config: SyncerConfig,
    tool_config: ToolConfig,
    policies: dict[str, Policy],
    jitter: float,
    breaker: HostBreaker,
) -> RepoBranchReport:
    """Fetch one repo and compare its refs before and after (runs in a worker thread)."""
    if jitter > 0:
        time.sleep(random.uniform(0, jitter))  # desync the initial burst of concurrent fetches

    path = Path(repo_config.path).expanduser()
    label = screen_label(repo_config)
    repo = Repo(
        name=repo_config.name,
        path=path,
        owner=repo_config.owner or config.owner,
        host=config.host,
        timeout=tool_config.git_timeout,
        url=resolve_clone_url(repo_config, config),
    )

    def lifecycle(status: str, detail: str | None = None) -> RepoBranchReport:
        return RepoBranchReport(
            label=label, path=repo_config.path, name=repo_config.name, lifecycle=status, lifecycle_detail=detail, expected_url=repo.url
        )

    if not repo.is_git_repo:
        # A `.git` file is a linked worktree or git state syncer cannot read, not a missing clone.
        return (
            lifecycle('not_git') if (path / '.git').exists() else lifecycle('not_cloned', '`syncer apply` clones it, and fetch never does')
        )
    remotes = repo.remotes()
    if remotes is None:
        return unfetched(repo, label, repo_config, 'cannot read this repo (git failed)')
    if not remotes:
        return lifecycle('no_remote')

    # The policy decides only whether the fetch prunes, exactly as it does for `check`.
    policy_name = resolve_policy_name(repo_config, tool_config, None)
    policy = policies.get(policy_name)
    if policy is None:
        return RepoBranchReport(
            label=label, path=repo_config.path, name=repo_config.name, policy_name=policy_name, error=f'unknown policy {policy_name!r}'
        )

    trip = breaker.trip_for(repo.contacted_url)
    if trip is not None:
        return skipped_report(repo_config, trip, repo.contacted_url)

    before = repo.remote_refs()
    if before is None:
        return unfetched(repo, label, repo_config, 'cannot read this repo (git failed)', policy_name)
    failure = refresh_remote(repo, policy)
    if failure is not None:
        breaker.record_failure(repo.contacted_url, failure)
        return unfetched(repo, label, repo_config, 'fetch failed', policy_name)
    breaker.record_success(repo.contacted_url)
    after = repo.remote_refs()
    if after is None:
        return unfetched(repo, label, repo_config, 'fetched, but its refs could not be read (git failed)', policy_name)
    return RepoBranchReport(
        label=label,
        path=repo_config.path,
        name=repo_config.name,
        policy_name=policy_name,
        expected_url=repo.url,
        moved_tags=list(repo.moved_tags),
        tag_fetch_error=repo.tag_fetch_error,
        ref_moves=RefMoves.between(before, after),
    )


def repo_outcome(report: RepoBranchReport) -> str:
    """One word for what happened to this repo: `updated`, `current`, `failed`, `skipped`, or its lifecycle."""
    if report.lifecycle:
        return report.lifecycle
    if report.skipped is not None:
        return 'skipped'
    if report.error:
        return 'failed'
    return UPDATED if report.ref_moves else CURRENT


def summary_counts(reports: list[RepoBranchReport]) -> dict[str, int]:
    """Every repo lands in exactly one of the first three counts, so they sum to the registry.

    `stale_tags` counts tags instead. A floating tag such as `v1` moves on every release of its
    repo, and the local one stays stale until a person deletes it. So it is counted on the line
    a clean run prints, and only `-v` names each one.
    """
    outcomes = Counter(repo_outcome(report) for report in reports)
    return {
        CURRENT: outcomes[CURRENT],
        UPDATED: outcomes[UPDATED],
        'not_fetched': len(reports) - outcomes[CURRENT] - outcomes[UPDATED],
        'stale_tags': sum(len(report.moved_tags) for report in reports),
    }


def needs_attention(report: RepoBranchReport) -> bool:
    """Shown without `-v`: a repo not fetched, or one whose new tags did not arrive."""
    return report_severity(report) is Severity.ERROR or report.tag_fetch_error is not None


def visible_reports(reports: list[RepoBranchReport], verbose: bool) -> list[RepoBranchReport]:
    """A skipped repo is left out at both levels, because the failure summary names its host once."""
    candidates = [report for report in reports if report.skipped is None]
    return candidates if verbose else [report for report in candidates if needs_attention(report)]


def describe_moves(moves: RefMoves) -> str:
    return ', '.join([*moves.updated, *(f'{name} pruned' for name in moves.pruned), *(f'tag {tag}' for tag in moves.tags)])


def render_fetch_report(report: RepoBranchReport) -> None:
    if report.lifecycle or report.error:
        render_report(report, apply=False)
        return
    if report.ref_moves:
        console.print(f'[cyan]{ICON_PULL}  {report.label} — {escape(describe_moves(report.ref_moves))}[/cyan]', soft_wrap=True)
    else:
        console.print(f'[green]{ICON_OK}  {report.label} — current[/green]')
    render_tag_findings(report)


def print_summary_line(counts: dict[str, int]) -> None:
    parts = [f'[green]{ICON_OK}  {counts[CURRENT]} current[/green]']
    if counts[UPDATED]:
        parts.append(f'[cyan]{ICON_PULL}  {counts[UPDATED]} updated[/cyan]')
    if counts['not_fetched']:
        parts.append(f'[red]{ICON_ERR}  {counts["not_fetched"]} not fetched[/red]')
    if counts['stale_tags']:
        noun = 'tag' if counts['stale_tags'] == 1 else 'tags'
        parts.append(f'[blue]{ICON_DOT}  {counts["stale_tags"]} stale {noun} origin moved[/blue]')
    console.print('  │  '.join(parts))


def repo_json(report: RepoBranchReport) -> dict:
    moves = report.ref_moves or RefMoves()
    return {
        'name': report.name,
        'path': report.path,
        'status': repo_outcome(report),
        'updated': list(moves.updated),
        'pruned': list(moves.pruned),
        'tags': list(moves.tags),
        'moved_tags': report.moved_tags,
        'tag_fetch_error': report.tag_fetch_error,
        'error': report.error,
        'error_detail': report.error_detail,
        'skipped': {'host': report.skipped.host, 'cause': report.skipped.cause.value} if report.skipped else None,
    }


def run_fetch(
    config: SyncerConfig,
    tool_config: ToolConfig,
    jobs: int = DEFAULT_JOBS,
    jitter: float = DEFAULT_JITTER_SECONDS,
    as_json: bool = False,
    verbose: bool = False,
) -> list[RepoBranchReport]:
    """Fetch every active repo and render the result. Returns the reports so the caller can set an exit code.

    The summary line comes first, because it is the whole of a clean run. Below it go the repos
    that could not be fetched, or under `-v` every repo with what moved.
    """
    worker = partial(
        build_fetch_report,
        config=config,
        tool_config=tool_config,
        policies=resolve_policies(tool_config),
        jitter=jitter,
        breaker=HostBreaker(),
    )
    reports = run_pool(active_repo_configs(config), worker, jobs, show_progress=not as_json)
    if as_json:
        emit_json(
            {'summary': summary_counts(reports), 'repos': [repo_json(report) for report in reports], 'failures': failures_json(reports)}
        )
        return reports
    print_summary_line(summary_counts(reports))
    for report in visible_reports(reports, verbose):
        render_fetch_report(report)
    render_failure_summary(reports)
    return reports
