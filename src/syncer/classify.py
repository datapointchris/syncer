"""Impure half of the pipeline: turn real git state into BranchState objects.

classify_repo() runs the read-side remediation the design calls for — fetch --prune
plus `git remote set-head origin --auto` — so a renamed default resolves correctly and
stale remote-tracking refs are gone before anything is classified. It performs no
mutation of local branches; that is execute()'s job (a later slice).
"""

from __future__ import annotations

from syncer.policy import BranchState
from syncer.policy import Policy
from syncer.policy import PrimaryState
from syncer.policy import Scope
from syncer.repos import GitFailure
from syncer.repos import Repo


class ClassifyError(Exception):
    """A branch's state could not be measured even though the fetch succeeded.

    Raised rather than guessed at: every fallback value available here is indistinguishable
    from a real state — (0, 0) reads as SYNCED, an empty upstream reads as NO_UPSTREAM — so
    the caller turns this into a repo-level error instead of a plausible-looking branch row.
    """

    def __init__(self, branch: str, failure: GitFailure) -> None:
        super().__init__(f'could not classify {branch}: {failure.command}')
        self.branch = branch
        self.failure = failure


def _primary_from_counts(ahead: int, behind: int) -> PrimaryState:
    if ahead == behind == 0:
        return PrimaryState.SYNCED
    if behind == 0:
        return PrimaryState.AHEAD
    if ahead == 0:
        return PrimaryState.BEHIND
    return PrimaryState.DIVERGED


def classify_branch(
    repo: Repo,
    branch: str,
    *,
    default: str | None,
    current: str,
    dirty_current: bool,
    stashed: bool,
    merge_target: str | None = None,
) -> BranchState:
    is_current = branch == current
    is_default = branch == default
    # dirty only gates the *current* branch — a non-current branch's tree isn't checked out.
    dirty = dirty_current and is_current
    upstream_short, gone = repo.branch_upstream(branch)
    target = merge_target or default

    ahead = 0
    behind = 0
    merged_into_target = False
    if not upstream_short:
        primary = PrimaryState.NO_UPSTREAM
    elif gone:
        primary = PrimaryState.GONE
        if target:
            merged_into_target = repo.contains_branch(branch, target)
    else:
        counts = repo.ahead_behind(branch, upstream_short)
        if counts is None:
            raise ClassifyError(branch, repo.failures[-1])
        ahead, behind = counts
        primary = _primary_from_counts(ahead, behind)

    return BranchState(
        branch=branch,
        primary=primary,
        ahead=ahead,
        behind=behind,
        is_current=is_current,
        is_default=is_default,
        dirty=dirty,
        stashed=stashed,
        upstream=upstream_short or None,
        merged_into_target=merged_into_target,
    )


def _branches_in_scope(repo: Repo, scope: Scope, default: str | None, current: str, detached: bool) -> list[str]:
    if scope == Scope.DEFAULT:
        return [default] if default else []
    if scope == Scope.CURRENT:
        return [] if detached else [current]
    if scope == Scope.TRACKED:
        return [branch for branch in repo.local_branches() if repo.branch_upstream(branch)[0]]
    return repo.local_branches()


def refresh_remote(repo: Repo, policy: Policy) -> GitFailure | None:
    """Run the read-side remediation, returning the fetch's failure if it had one.

    Split out of classify_repo so the caller can stop before classifying: every state below is
    measured against remote-tracking refs, so a dead fetch does not degrade the report, it
    invalidates it. set_head_auto's own failure is deliberately not escalated — it is
    remediation for a renamed default, not the measurement.
    """
    failure = repo.fetch_prune() if policy.prune else repo.fetch()
    if failure is not None:
        return failure
    repo.set_head_auto()
    return None


def classify_repo(repo: Repo, policy: Policy, *, fetched: bool = False) -> list[BranchState]:
    """Classify every branch the policy's scope selects, after prune + set-head.

    `fetched=True` when the caller already ran refresh_remote and checked its result.
    """
    if not fetched:
        refresh_remote(repo, policy)

    default = repo.default_branch
    current = repo.current_branch
    detached = current == 'HEAD'
    dirty_current = repo.is_dirty
    stashed = repo.stash_count > 0

    states = [
        classify_branch(
            repo,
            branch,
            default=default,
            current=current,
            dirty_current=dirty_current,
            stashed=stashed,
            merge_target=policy.merge_target,
        )
        for branch in _branches_in_scope(repo, policy.scope, default, current, detached)
    ]

    if detached and policy.scope in (Scope.CURRENT, Scope.ALL):
        states.append(
            BranchState(
                branch='(detached)',
                primary=PrimaryState.DETACHED,
                is_current=True,
                dirty=dirty_current,
                stashed=stashed,
            )
        )

    return states
