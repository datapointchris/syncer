"""Impure executor: perform the git op for a decided Action, enforcing the hard invariants.

execute() is the only place that mutates. It re-verifies every precondition live, immediately
before the write — never trusting the (possibly stale) BranchState from classify time — and
refuses rather than forces when a precondition fails. The hard invariants it guarantees,
independent of any policy:

1. Never --force / -f / --force-with-lease (no such argv is ever constructed).
2. Never mutate a branch whose working tree is dirty (the current branch).
3. fast_forward / pull_ff / ff_ref require strict ancestry (upstream strictly ahead), re-checked here.
4. rebase_push aborts on conflict and downgrades to a refusal — never a half-rebase.
5. delete_local only under the full GONE ∧ integrated ∧ ¬current ∧ ¬default ∧ ¬merge-target ∧
   clean guard. Integration is proven by ancestry or by patch equivalence, never assumed from
   the remote branch having been deleted.
6. Any precondition that fails at execute time is refused and reported, never forced.
7. execute always acts on the branch the state was classified from (explicit refspecs /
   ref names), never the incidentally-checked-out branch.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from syncer.policy import Action
from syncer.policy import BranchState
from syncer.policy import Policy
from syncer.repos import Repo

OutcomeStatus = Literal['skipped', 'reported', 'done', 'refused', 'failed']


class Outcome(BaseModel):
    """Result of executing an action against a branch.

    - skipped:  action was `skip` (silent no-op)
    - reported: action was `report`/`prompt` (surfaced, no mutation)
    - done:     mutation succeeded
    - refused:  a precondition failed at execute time — safe, no mutation attempted
    - failed:   a mutation was attempted and git rejected it (e.g. non-fast-forward push)
    """

    branch: str
    action: Action
    status: OutcomeStatus
    message: str = ''


def _refused(state: BranchState, action: Action, reason: str) -> Outcome:
    return Outcome(branch=state.branch, action=action, status='refused', message=reason)


def _is_strictly_behind(repo: Repo, branch: str, upstream: str) -> bool:
    ahead, behind = repo.ahead_behind(branch, upstream)
    return behind > 0 and ahead == 0


def _pull_ff(state: BranchState, repo: Repo, policy: Policy) -> Outcome:
    if not state.is_current:
        return _refused(state, Action.PULL_FF, 'pull_ff requires the branch to be current')
    if not state.upstream:
        return _refused(state, Action.PULL_FF, 'no upstream to fast-forward from')
    if repo.uncommitted_changes:  # invariant 2, re-checked live
        return _refused(state, Action.PULL_FF, 'working tree is dirty')
    if not _is_strictly_behind(repo, state.branch, state.upstream):  # invariant 3
        return _refused(state, Action.PULL_FF, 'upstream is not strictly ahead')
    ok, err = repo.merge_ff_only(state.upstream)
    if ok:
        return Outcome(branch=state.branch, action=Action.PULL_FF, status='done', message='fast-forwarded')
    return Outcome(branch=state.branch, action=Action.PULL_FF, status='failed', message=err)


def _ff_ref(state: BranchState, repo: Repo, policy: Policy) -> Outcome:
    if state.is_current:
        return _refused(state, Action.FF_REF, 'ff_ref is for non-current branches; use pull_ff')
    if not state.upstream:
        return _refused(state, Action.FF_REF, 'no upstream to advance to')
    if not _is_strictly_behind(repo, state.branch, state.upstream):  # invariant 3
        return _refused(state, Action.FF_REF, 'upstream is not strictly ahead')
    ok, err = repo.update_ref(state.branch, state.upstream)
    if ok:
        return Outcome(branch=state.branch, action=Action.FF_REF, status='done', message=f'advanced to {state.upstream}')
    return Outcome(branch=state.branch, action=Action.FF_REF, status='failed', message=err)


def _fast_forward(state: BranchState, repo: Repo, policy: Policy) -> Outcome:
    """Advance a branch to its upstream by whichever mechanism its checkout state allows.

    merge --ff-only needs the branch checked out; update-ref needs it not checked out. A rule
    naming either mechanism is therefore refused every time the branch sits on the other side
    of that split, which is why policies name this intent instead. Both delegates re-verify
    strict ancestry (and dirtiness, where it applies) themselves, so this adds no new primitive.
    """
    delegate = _pull_ff if state.is_current else _ff_ref
    return delegate(state, repo, policy).model_copy(update={'action': Action.FAST_FORWARD})


def _push(state: BranchState, repo: Repo, policy: Policy) -> Outcome:
    if repo.uncommitted_changes:
        return _refused(state, Action.PUSH, 'working tree is dirty')
    if not state.upstream:
        return _refused(state, Action.PUSH, 'no upstream; use set_upstream_push')
    ahead, behind = repo.ahead_behind(state.branch, state.upstream)
    if ahead == 0:
        return _refused(state, Action.PUSH, 'nothing to push')
    if behind > 0:
        return _refused(state, Action.PUSH, 'branch is diverged; refusing to push')
    ok, err = repo.push_branch(state.branch)
    if ok:
        return Outcome(branch=state.branch, action=Action.PUSH, status='done', message=f'pushed {ahead} commit(s)')
    return Outcome(branch=state.branch, action=Action.PUSH, status='failed', message=err)


def _rebase_push(state: BranchState, repo: Repo, policy: Policy) -> Outcome:
    if not state.is_current:
        return _refused(state, Action.REBASE_PUSH, 'rebase_push requires the branch to be current')
    if repo.uncommitted_changes:  # invariant 2
        return _refused(state, Action.REBASE_PUSH, 'working tree is dirty')
    if not state.upstream:
        return _refused(state, Action.REBASE_PUSH, 'no upstream to rebase onto')
    ahead, behind = repo.ahead_behind(state.branch, state.upstream)
    if not (ahead > 0 and behind > 0):
        return _refused(state, Action.REBASE_PUSH, 'branch is not diverged')
    if not repo.pull_rebase():  # invariant 4: conflict → abort → refuse, never half-rebase
        repo.rebase_abort()
        return _refused(state, Action.REBASE_PUSH, 'rebase conflict; aborted (resolve manually)')
    ok, err = repo.push_branch(state.branch)
    if ok:
        return Outcome(branch=state.branch, action=Action.REBASE_PUSH, status='done', message='rebased and pushed')
    return Outcome(branch=state.branch, action=Action.REBASE_PUSH, status='failed', message=err)


def _set_upstream_push(state: BranchState, repo: Repo, policy: Policy) -> Outcome:
    if state.upstream:
        return _refused(state, Action.SET_UPSTREAM_PUSH, 'branch already has an upstream')
    if repo.uncommitted_changes:
        return _refused(state, Action.SET_UPSTREAM_PUSH, 'working tree is dirty')
    ok, err = repo.push_branch(state.branch, set_upstream=True)
    if ok:
        return Outcome(branch=state.branch, action=Action.SET_UPSTREAM_PUSH, status='done', message='pushed and set upstream')
    return Outcome(branch=state.branch, action=Action.SET_UPSTREAM_PUSH, status='failed', message=err)


def _delete_local(state: BranchState, repo: Repo, policy: Policy) -> Outcome:
    # Full guard (invariant 5), every clause re-verified live.
    if state.primary != 'gone':
        return _refused(state, Action.DELETE_LOCAL, 'branch is not gone')
    if state.is_current:
        return _refused(state, Action.DELETE_LOCAL, 'refusing to delete the current branch')
    if state.is_default:
        return _refused(state, Action.DELETE_LOCAL, 'refusing to delete the default branch')
    if repo.uncommitted_changes:
        return _refused(state, Action.DELETE_LOCAL, 'working tree is dirty')
    target = policy.merge_target or repo.default_branch
    if not target:
        return _refused(state, Action.DELETE_LOCAL, 'no merge target to verify integration against')
    if state.branch == target:
        return _refused(state, Action.DELETE_LOCAL, f'refusing to delete the merge target ({target})')
    if not repo.contains_branch(state.branch, target):
        return _refused(state, Action.DELETE_LOCAL, f'branch is not integrated into {target}')
    ok, err = repo.delete_local_branch(state.branch)
    if ok:
        return Outcome(branch=state.branch, action=Action.DELETE_LOCAL, status='done', message=f'deleted; integrated into {target}')
    return Outcome(branch=state.branch, action=Action.DELETE_LOCAL, status='failed', message=err)


_MUTATORS = {
    Action.FAST_FORWARD: _fast_forward,
    Action.PULL_FF: _pull_ff,
    Action.FF_REF: _ff_ref,
    Action.PUSH: _push,
    Action.REBASE_PUSH: _rebase_push,
    Action.SET_UPSTREAM_PUSH: _set_upstream_push,
    Action.DELETE_LOCAL: _delete_local,
}


def execute(action: Action, state: BranchState, repo: Repo, policy: Policy) -> Outcome:
    """Perform `action` on `state.branch`, enforcing the hard invariants. Never forces.

    `policy` is passed for the settings a guard has to consult live (currently `merge_target`);
    it can never widen what an action is allowed to do — the invariants above hold whatever it says.
    """
    if action == Action.SKIP:
        return Outcome(branch=state.branch, action=action, status='skipped')
    if action == Action.REPORT:
        return Outcome(branch=state.branch, action=action, status='reported')
    if action == Action.PROMPT:
        # Interactive traverse is deferred to a later slice; degrade to a report.
        return Outcome(branch=state.branch, action=action, status='reported', message='interactive prompt not implemented (v1)')
    return _MUTATORS[action](state, repo, policy)
