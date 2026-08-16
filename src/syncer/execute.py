"""Impure executor: perform the git op for a decided Action, enforcing the hard invariants.

execute() is the only place that mutates. It re-verifies every precondition live, immediately
before the write — never trusting the (possibly stale) BranchState from classify time — and
refuses rather than forces when a precondition fails. The hard invariants it guarantees,
independent of any policy:

1. Never --force / -f / --force-with-lease (no such argv is ever constructed).
2. Never mutate a branch whose working tree is dirty — or whose cleanliness cannot be verified.
   `git status` failing used to read as a clean tree, i.e. as permission to mutate. The exemptions
   are the actions that touch no tree at all (_TREE_INDEPENDENT), each refusing under invariant 9
   first so that "checked out nowhere" is verified rather than assumed.
3. fast_forward / pull_ff / ff_ref require strict ancestry (upstream strictly ahead), re-checked here.
4. rebase_push aborts on conflict and downgrades to a refusal — never a half-rebase.
5. delete_local only under the full GONE ∧ integrated ∧ ¬current ∧ ¬worktree ∧ ¬default ∧
   ¬merge-target guard. Integration is proven by ancestry or by patch equivalence, never assumed
   from the remote branch having been deleted.
6. Any precondition that fails at execute time is refused and reported, never forced.
7. execute always acts on the branch the state was classified from (explicit refspecs /
   ref names), never the incidentally-checked-out branch.
8. A branch matching the policy's `protected` patterns admits no action that publishes or
   destroys — checked centrally here, before dispatch, so it holds for every action including
   ones added later.
9. Never write the ref of a branch another worktree has checked out. ff_ref and delete_local guard
   it explicitly — update-ref is plumbing and git does not check it at all, and git's own refusal
   of `branch -D` arrives as prose in a failure, which no caller can join on and no report can
   predict.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Literal
from typing import NamedTuple

from pydantic import BaseModel

from syncer.policy import PROTECTED_ALLOWED
from syncer.policy import Action
from syncer.policy import BranchState
from syncer.policy import Policy
from syncer.policy import PrimaryState
from syncer.policy import matching_protection
from syncer.repos import Repo

OutcomeStatus = Literal['skipped', 'reported', 'done', 'refused', 'failed']


class Refusal(StrEnum):
    """Why an action was refused, as a stable key rather than a sentence.

    The key is what everything else joins on — ACTION_DOCS lists these, the tests compare these,
    and `policy actions show` renders them through REFUSAL_TEXT. Wording therefore lives in
    exactly one place and can be rewritten without touching a test, which is the point: a test
    pinned to prose fails when nothing is wrong, and that churn teaches you to loosen the
    assertion rather than to trust it.
    """

    NOT_CURRENT = 'not_current'
    IS_CURRENT = 'is_current'
    WORKTREE_CHECKOUT = 'worktree_checkout'
    NO_WORKTREE = 'no_worktree'
    WORKTREE_DIRTY = 'worktree_dirty'
    NO_UPSTREAM = 'no_upstream'
    HAS_UPSTREAM = 'has_upstream'
    DIRTY_TREE = 'dirty_tree'
    NOT_STRICTLY_BEHIND = 'not_strictly_behind'
    COUNTS_UNREADABLE = 'counts_unreadable'
    NOTHING_TO_PUSH = 'nothing_to_push'
    DIVERGED = 'diverged'
    NOT_DIVERGED = 'not_diverged'
    REBASE_CONFLICT = 'rebase_conflict'
    NOT_GONE = 'not_gone'
    DELETE_CURRENT = 'delete_current'
    DELETE_DEFAULT = 'delete_default'
    NO_MERGE_TARGET = 'no_merge_target'
    DELETE_MERGE_TARGET = 'delete_merge_target'
    NOT_INTEGRATED = 'not_integrated'
    PROTECTED = 'protected'


# The single source of refusal wording. Fields come from _refused's keyword arguments.
REFUSAL_TEXT: dict[Refusal, str] = {
    Refusal.NOT_CURRENT: '{verb} requires the branch to be current',
    Refusal.IS_CURRENT: 'ff_ref is for non-current branches; use pull_ff',
    Refusal.WORKTREE_CHECKOUT: 'branch is checked out in another worktree',
    Refusal.NO_WORKTREE: 'ff_worktree is for a branch a linked worktree holds; use fast_forward',
    Refusal.WORKTREE_DIRTY: 'the worktree holding this branch has uncommitted changes',
    Refusal.NO_UPSTREAM: 'branch has no upstream',
    Refusal.HAS_UPSTREAM: 'branch already has an upstream',
    Refusal.DIRTY_TREE: 'working tree is dirty',
    Refusal.NOT_STRICTLY_BEHIND: 'upstream is not strictly ahead',
    Refusal.COUNTS_UNREADABLE: 'cannot read ahead/behind counts',
    Refusal.NOTHING_TO_PUSH: 'nothing to push',
    Refusal.DIVERGED: 'branch is diverged; refusing to push',
    Refusal.NOT_DIVERGED: 'branch is not diverged',
    Refusal.REBASE_CONFLICT: 'rebase conflict; aborted (resolve manually)',
    Refusal.NOT_GONE: 'branch is not gone',
    Refusal.DELETE_CURRENT: 'refusing to delete the current branch',
    Refusal.DELETE_DEFAULT: 'refusing to delete the default branch',
    Refusal.NO_MERGE_TARGET: 'no merge target to verify integration against',
    Refusal.DELETE_MERGE_TARGET: 'refusing to delete the merge target ({target})',
    Refusal.NOT_INTEGRATED: 'branch is not integrated into {target}',
    Refusal.PROTECTED: 'protected by {pattern!r}',
}

# Stand-ins for the runtime values a refusal template interpolates, used when a reason is being
# *documented* rather than reported and there is no actual value to show.
_DOC_FIELDS = {'target': '<target>', 'verb': 'this action', 'pattern': '<glob>'}


def describe_refusal(reason: Refusal) -> str:
    """A refusal's wording for reference output, with generic stand-ins for runtime values."""
    return REFUSAL_TEXT[reason].format_map(_DOC_FIELDS)


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
    # The refusal's stable key. Anything that needs to *identify* a refusal reads this; message
    # is for display only, and nothing joins on its text.
    reason: Refusal | None = None


def _refused(state: BranchState, action: Action, reason: Refusal, **fields: object) -> Outcome:
    return Outcome(
        branch=state.branch,
        action=action,
        status='refused',
        reason=reason,
        message=REFUSAL_TEXT[reason].format(**fields),
    )


def _is_strictly_behind(repo: Repo, branch: str, upstream: str) -> bool:
    """Invariant 3. False when the counts cannot be read at all — unverified is not permission."""
    counts = repo.ahead_behind(branch, upstream)
    if counts is None:
        return False
    ahead, behind = counts
    return behind > 0 and ahead == 0


def _pull_ff(state: BranchState, repo: Repo, policy: Policy) -> Outcome:
    if not state.is_current:
        return _refused(state, Action.PULL_FF, Refusal.NOT_CURRENT, verb='pull_ff')
    if not state.upstream:
        return _refused(state, Action.PULL_FF, Refusal.NO_UPSTREAM)
    if repo.is_dirty:  # invariant 2, re-checked live
        return _refused(state, Action.PULL_FF, Refusal.DIRTY_TREE)
    if not _is_strictly_behind(repo, state.branch, state.upstream):  # invariant 3
        return _refused(state, Action.PULL_FF, Refusal.NOT_STRICTLY_BEHIND)
    ok, err = repo.merge_ff_only(state.upstream)
    if ok:
        return Outcome(branch=state.branch, action=Action.PULL_FF, status='done', message='fast-forwarded')
    return Outcome(branch=state.branch, action=Action.PULL_FF, status='failed', message=err)


def _ff_ref(state: BranchState, repo: Repo, policy: Policy) -> Outcome:
    if state.is_current:
        return _refused(state, Action.FF_REF, Refusal.IS_CURRENT)
    # Invariant 9. `is_current` is this checkout's HEAD only, so a branch live in a linked
    # worktree reads as "not checked out" here and update-ref — plumbing, and the one mechanism
    # on the menu git does not protect — would move it under a tree syncer never measured.
    if repo.held_by_worktree(state.branch):
        return _refused(state, Action.FF_REF, Refusal.WORKTREE_CHECKOUT)
    if not state.upstream:
        return _refused(state, Action.FF_REF, Refusal.NO_UPSTREAM)
    if not _is_strictly_behind(repo, state.branch, state.upstream):  # invariant 3
        return _refused(state, Action.FF_REF, Refusal.NOT_STRICTLY_BEHIND)
    ok, err = repo.update_ref(state.branch, state.upstream)
    if ok:
        return Outcome(branch=state.branch, action=Action.FF_REF, status='done', message=f'advanced to {state.upstream}')
    return Outcome(branch=state.branch, action=Action.FF_REF, status='failed', message=err)


def _ff_worktree(state: BranchState, repo: Repo, policy: Policy) -> Outcome:
    """Fast-forward a branch a linked worktree holds, by merging inside that worktree.

    Invariant 9 forbids writing such a branch's ref from *outside*, because update-ref moves the
    ref while the worktree's index stays where it was. Running the merge in the worktree is the
    opposite operation: git moves ref, index and tree together, exactly as it does for _pull_ff
    on the current branch. So this adds a location, not a primitive — same argv, same strict
    ancestry check, and a dirty guard against the tree it actually writes into.

    The worktree is resolved live rather than read off the state: one that has been removed since
    classify time must refuse, and worktree_for answering None when git could not be asked lands
    on the same refusal, which is the direction that declines to act.
    """
    worktree = repo.worktree_for(state.branch)
    if worktree is None:
        return _refused(state, Action.FF_WORKTREE, Refusal.NO_WORKTREE)
    if repo.worktree_is_dirty(worktree):  # invariant 2, against the tree this actually writes
        return _refused(state, Action.FF_WORKTREE, Refusal.WORKTREE_DIRTY)
    if not state.upstream:
        return _refused(state, Action.FF_WORKTREE, Refusal.NO_UPSTREAM)
    if not _is_strictly_behind(repo, state.branch, state.upstream):  # invariant 3
        return _refused(state, Action.FF_WORKTREE, Refusal.NOT_STRICTLY_BEHIND)
    ok, err = repo.merge_ff_only_in(worktree, state.upstream)
    if ok:
        return Outcome(branch=state.branch, action=Action.FF_WORKTREE, status='done', message=f'fast-forwarded in {worktree}')
    return Outcome(branch=state.branch, action=Action.FF_WORKTREE, status='failed', message=err)


def _fast_forward(state: BranchState, repo: Repo, policy: Policy) -> Outcome:
    """Advance a branch to its upstream by whichever mechanism its checkout state allows.

    Three places a branch can be checked out, three mechanisms: here (merge), in a linked
    worktree (merge, there), or nowhere (update-ref). A rule naming any one of them is refused
    every time the branch is somewhere else, which is why policies name this intent instead.
    Every delegate re-verifies strict ancestry and its own dirtiness, so this adds no primitive.

    Dispatch reads git rather than `state.worktree`, per invariant 6 — a worktree added or
    removed since classify time would otherwise pick a mechanism that is now the wrong one. A
    failed `git worktree list` lands on _ff_ref, whose own guard refuses what it cannot verify.
    """
    if state.is_current:
        delegate = _pull_ff
    elif repo.worktree_for(state.branch) is not None:
        delegate = _ff_worktree
    else:
        delegate = _ff_ref
    return delegate(state, repo, policy).model_copy(update={'action': Action.FAST_FORWARD})


def _push(state: BranchState, repo: Repo, policy: Policy) -> Outcome:
    if repo.is_dirty:
        return _refused(state, Action.PUSH, Refusal.DIRTY_TREE)
    if not state.upstream:
        return _refused(state, Action.PUSH, Refusal.NO_UPSTREAM)
    counts = repo.ahead_behind(state.branch, state.upstream)
    if counts is None:
        return _refused(state, Action.PUSH, Refusal.COUNTS_UNREADABLE)
    ahead, behind = counts
    if ahead == 0:
        return _refused(state, Action.PUSH, Refusal.NOTHING_TO_PUSH)
    if behind > 0:
        return _refused(state, Action.PUSH, Refusal.DIVERGED)
    ok, err = repo.push_branch(state.branch)
    if ok:
        return Outcome(branch=state.branch, action=Action.PUSH, status='done', message=f'pushed {ahead} commit(s)')
    return Outcome(branch=state.branch, action=Action.PUSH, status='failed', message=err)


def _rebase_push(state: BranchState, repo: Repo, policy: Policy) -> Outcome:
    if not state.is_current:
        return _refused(state, Action.REBASE_PUSH, Refusal.NOT_CURRENT, verb='rebase_push')
    if repo.is_dirty:  # invariant 2
        return _refused(state, Action.REBASE_PUSH, Refusal.DIRTY_TREE)
    if not state.upstream:
        return _refused(state, Action.REBASE_PUSH, Refusal.NO_UPSTREAM)
    counts = repo.ahead_behind(state.branch, state.upstream)
    if counts is None:
        return _refused(state, Action.REBASE_PUSH, Refusal.COUNTS_UNREADABLE)
    ahead, behind = counts
    if not (ahead > 0 and behind > 0):
        return _refused(state, Action.REBASE_PUSH, Refusal.NOT_DIVERGED)
    if not repo.pull_rebase():  # invariant 4: conflict → abort → refuse, never half-rebase
        repo.rebase_abort()
        return _refused(state, Action.REBASE_PUSH, Refusal.REBASE_CONFLICT)
    ok, err = repo.push_branch(state.branch)
    if ok:
        return Outcome(branch=state.branch, action=Action.REBASE_PUSH, status='done', message='rebased and pushed')
    return Outcome(branch=state.branch, action=Action.REBASE_PUSH, status='failed', message=err)


def _set_upstream_push(state: BranchState, repo: Repo, policy: Policy) -> Outcome:
    if state.upstream:
        return _refused(state, Action.SET_UPSTREAM_PUSH, Refusal.HAS_UPSTREAM)
    if repo.is_dirty:
        return _refused(state, Action.SET_UPSTREAM_PUSH, Refusal.DIRTY_TREE)
    ok, err = repo.push_branch(state.branch, set_upstream=True)
    if ok:
        return Outcome(branch=state.branch, action=Action.SET_UPSTREAM_PUSH, status='done', message='pushed and set upstream')
    return Outcome(branch=state.branch, action=Action.SET_UPSTREAM_PUSH, status='failed', message=err)


def _delete_local(state: BranchState, repo: Repo, policy: Policy) -> Outcome:
    # Full guard (invariant 5), every clause re-verified live.
    if state.primary != 'gone':
        return _refused(state, Action.DELETE_LOCAL, Refusal.NOT_GONE)
    if state.is_current:
        return _refused(state, Action.DELETE_LOCAL, Refusal.DELETE_CURRENT)
    # Invariant 9, stated rather than inherited. git refuses this itself, but a refusal only git
    # knows about arrives as a `failed` outcome carrying prose, which nothing joins on and the
    # reporter cannot predict — so the row promised an arrow apply was never going to follow.
    if repo.held_by_worktree(state.branch):
        return _refused(state, Action.DELETE_LOCAL, Refusal.WORKTREE_CHECKOUT)
    if state.is_default:
        return _refused(state, Action.DELETE_LOCAL, Refusal.DELETE_DEFAULT)
    target = policy.merge_target or repo.default_branch
    if not target:
        return _refused(state, Action.DELETE_LOCAL, Refusal.NO_MERGE_TARGET)
    if state.branch == target:
        return _refused(state, Action.DELETE_LOCAL, Refusal.DELETE_MERGE_TARGET, target=target)
    if not repo.contains_branch(state.branch, target):
        return _refused(state, Action.DELETE_LOCAL, Refusal.NOT_INTEGRATED, target=target)
    ok, err = repo.delete_local_branch(state.branch)
    if ok:
        return Outcome(branch=state.branch, action=Action.DELETE_LOCAL, status='done', message=f'deleted; integrated into {target}')
    return Outcome(branch=state.branch, action=Action.DELETE_LOCAL, status='failed', message=err)


_MUTATORS: dict[Action, Callable[[BranchState, Repo, Policy], Outcome]] = {
    Action.FAST_FORWARD: _fast_forward,
    Action.PULL_FF: _pull_ff,
    Action.FF_WORKTREE: _ff_worktree,
    Action.FF_REF: _ff_ref,
    Action.PUSH: _push,
    Action.REBASE_PUSH: _rebase_push,
    Action.SET_UPSTREAM_PUSH: _set_upstream_push,
    Action.DELETE_LOCAL: _delete_local,
}

# The actions syncer performs, as opposed to the ones that only surface something (skip, report,
# prompt). Derived from _MUTATORS rather than listed again, so the reporter's notion of "syncer
# will handle this" cannot drift from the set of things that actually have a mutator.
MUTATING_ACTIONS: frozenset[Action] = frozenset(_MUTATORS)

# The mutators that never read or write a working tree at all: both move a ref whose branch is
# checked out nowhere, so no repo's dirtiness bears on them. The premise holds only because
# invariant 9 refuses first in each — a branch a linked worktree has checked out is not "not
# checked out", and writing its ref from outside is exactly the tree-corrupting write these
# exemptions read as impossible. Uncommitted changes belong to a tree, never to a branch, so a
# dirty tree cannot be evidence about the ref being deleted; refusing on it protected nothing and
# left every merged branch in a repo with work in progress uncollectable.
_TREE_INDEPENDENT = frozenset({Action.FF_REF, Action.DELETE_LOCAL})


class ActionDoc(NamedTuple):
    """What `syncer policy actions show` renders for one action.

    `applies_to` is declared rather than derived, and that is deliberate. Only _delete_local tests
    `state.primary`; every other mutator checks the live facts instead — _push wants ahead > 0 and
    behind == 0, _rebase_push wants both non-zero, the fast-forward pair wants behind > 0 and
    ahead == 0. Those *are* the definitions of AHEAD / DIVERGED / BEHIND, so the mapping is real,
    but there is no primary check to read it off. Adding one would break invariant 6's whole
    point: a guard consulting the classify-time state instead of git is trusting a value that may
    already be stale. So it is declared here and *proven* in test_execute.py, which drives each
    mutator against a repo in every primary state and asserts it refuses exactly outside this set.
    """

    summary: str
    runs: str | None
    refuses: tuple[Refusal, ...]
    never: str | None
    applies_to: frozenset[PrimaryState]


# Every Action, with the record `policy actions show` prints. The refusal strings are the ones the
# guards actually return, so the two cannot drift silently — see TestActionDocs. A new Action
# without an entry fails a test rather than rendering blank, which is the same direction
# PROTECTED_ALLOWED takes: forget it and something complains.
ACTION_DOCS: dict[Action, ActionDoc] = {
    Action.SKIP: ActionDoc(
        summary='do nothing, silently',
        runs=None,
        refuses=(),
        never=None,
        applies_to=frozenset(PrimaryState),
    ),
    Action.REPORT: ActionDoc(
        summary='surface the branch and change nothing',
        runs=None,
        refuses=(),
        never=None,
        applies_to=frozenset(PrimaryState),
    ),
    Action.PROMPT: ActionDoc(
        summary='ask before acting (not implemented; reports)',
        runs=None,
        refuses=(),
        never=None,
        applies_to=frozenset(PrimaryState),
    ),
    Action.FAST_FORWARD: ActionDoc(
        summary='advance a branch to its upstream, wherever it is checked out',
        runs='git merge --ff-only <upstream>  (here, or in the worktree)  ·  git update-ref (checked out nowhere)',
        refuses=(
            Refusal.NO_UPSTREAM,
            Refusal.DIRTY_TREE,
            Refusal.WORKTREE_DIRTY,
            Refusal.NOT_STRICTLY_BEHIND,
            Refusal.WORKTREE_CHECKOUT,
        ),
        never='creates a merge commit, or moves a branch the upstream is not strictly ahead of',
        applies_to=frozenset({PrimaryState.BEHIND}),
    ),
    Action.PULL_FF: ActionDoc(
        summary='fast-forward the checked-out branch (mechanism)',
        runs='git merge --ff-only <upstream>',
        refuses=(Refusal.NOT_CURRENT, Refusal.NO_UPSTREAM, Refusal.DIRTY_TREE, Refusal.NOT_STRICTLY_BEHIND),
        never='creates a merge commit — --ff-only means git refuses rather than merging',
        applies_to=frozenset({PrimaryState.BEHIND}),
    ),
    Action.FF_WORKTREE: ActionDoc(
        summary='fast-forward a branch a linked worktree holds, inside it (mechanism)',
        runs='git -C <worktree> merge --ff-only <upstream>',
        refuses=(Refusal.NO_WORKTREE, Refusal.WORKTREE_DIRTY, Refusal.NO_UPSTREAM, Refusal.NOT_STRICTLY_BEHIND),
        never='writes the ref from outside the worktree, which would leave that index describing a commit it no longer points at',
        applies_to=frozenset({PrimaryState.BEHIND}),
    ),
    Action.FF_REF: ActionDoc(
        summary='fast-forward a branch that is not checked out (mechanism)',
        runs='git update-ref refs/heads/<branch> <upstream>',
        refuses=(Refusal.IS_CURRENT, Refusal.WORKTREE_CHECKOUT, Refusal.NO_UPSTREAM, Refusal.NOT_STRICTLY_BEHIND),
        never='moves a ref any working tree has checked out — this one or a linked worktree, which git itself does not check here',
        applies_to=frozenset({PrimaryState.BEHIND}),
    ),
    Action.PUSH: ActionDoc(
        summary='publish local commits to the existing upstream',
        runs='git push origin <branch>:<branch>',
        refuses=(
            Refusal.DIRTY_TREE,
            Refusal.NO_UPSTREAM,
            Refusal.COUNTS_UNREADABLE,
            Refusal.NOTHING_TO_PUSH,
            Refusal.DIVERGED,
        ),
        never='force-pushes, or pushes a branch that is behind — no --force argv is ever constructed',
        applies_to=frozenset({PrimaryState.AHEAD}),
    ),
    Action.REBASE_PUSH: ActionDoc(
        summary='rebase onto the upstream, then publish',
        runs='git pull --rebase  →  git push origin <branch>:<branch>',
        refuses=(
            Refusal.NOT_CURRENT,
            Refusal.DIRTY_TREE,
            Refusal.NO_UPSTREAM,
            Refusal.COUNTS_UNREADABLE,
            Refusal.NOT_DIVERGED,
            Refusal.REBASE_CONFLICT,
        ),
        never='leaves a half-finished rebase — a conflict is aborted and downgraded to a refusal',
        applies_to=frozenset({PrimaryState.DIVERGED}),
    ),
    Action.SET_UPSTREAM_PUSH: ActionDoc(
        summary='publish a branch with no upstream, and set one',
        runs='git push -u origin <branch>',
        refuses=(Refusal.HAS_UPSTREAM, Refusal.DIRTY_TREE),
        never='retargets an upstream that already exists',
        applies_to=frozenset({PrimaryState.NO_UPSTREAM}),
    ),
    Action.DELETE_LOCAL: ActionDoc(
        summary='remove a local branch whose upstream is gone',
        runs='git branch -D <branch>',
        refuses=(
            Refusal.NOT_GONE,
            Refusal.DELETE_CURRENT,
            Refusal.WORKTREE_CHECKOUT,
            Refusal.DELETE_DEFAULT,
            Refusal.NO_MERGE_TARGET,
            Refusal.DELETE_MERGE_TARGET,
            Refusal.NOT_INTEGRATED,
        ),
        never=(
            'deletes work the merge target does not provably hold — proven by ancestry or patch '
            'equivalence, never inferred from the remote branch having been deleted'
        ),
        applies_to=frozenset({PrimaryState.GONE}),
    ),
}


def dirty_refusal(action: Action, state: BranchState, dirty: bool) -> Refusal | None:
    """Why a dirty working tree refuses `action`, or None if it admits it.

    Shared with the reporter for the same reason protection_refusal is: rendering the decided
    `push` alone promises a push that apply is never going to make, and a dirty tree is the
    commonest reason it will not. Invariant 2 is still enforced inside each mutator against a
    live `repo.is_dirty` — this only mirrors it for the report, and test_execute.py asserts the
    two agree for every action on the menu.
    """
    if not dirty or action not in MUTATING_ACTIONS:
        return None
    if action in _TREE_INDEPENDENT:
        return None
    # ff_worktree does write a tree, just never this one — `dirty` is the main checkout's, and the
    # point of a worktree is that the two are independent. Its own tree is gated by
    # worktree_refusal, which is the only gate that has measured it.
    if action is Action.FF_WORKTREE:
        return None
    # fast_forward dispatches on checkout state, so off the current branch it inherits whichever
    # of the two non-current mechanisms applies — and neither of them reads this tree.
    if action == Action.FAST_FORWARD and not state.is_current:
        return None
    return Refusal.DIRTY_TREE


def checkout_refusal(action: Action, state: BranchState) -> Refusal | None:
    """Why `action` is refused by which branch happens to be checked out, or None if it admits it.

    The mechanisms split on this and each guard's first line tests it, so it is as static as
    protection and as knowable without running anything. Missing from the mirror, `mirror`'s
    `*:diverged = rebase_push` printed a rebase arrow on every non-current diverged branch and
    refused all of them — the arrow promising an action apply never makes, which is the failure
    dirty_refusal and protection_refusal already exist to prevent.

    FAST_FORWARD is absent on purpose: it dispatches between the two mechanisms precisely so that
    neither side of the split refuses it.
    """
    if action in (Action.PULL_FF, Action.REBASE_PUSH) and not state.is_current:
        return Refusal.NOT_CURRENT
    if action is Action.FF_REF and state.is_current:
        return Refusal.IS_CURRENT
    return None


def worktree_refusal(action: Action, state: BranchState) -> Refusal | None:
    """How the branch's worktree situation bears on `action`, or None if it admits it.

    The third gate the reporter can evaluate without executing, for the same reason as the other
    two: an arrow apply will never follow is the part of a row that costs it its meaning.

    Three answers, because a worktree is not one fact. It *forbids* the actions that write the ref
    from outside — ff_ref and delete_local, which invariant 9 covers. It is *required* by
    ff_worktree, which has nothing to merge into without one. And it carries a tree of its own,
    which ff_worktree writes into and so must find clean; that is the only thing standing between
    a worktree-held branch and an ordinary fast-forward.

    delete_local is here because git's own refusal is not a substitute for one of ours. git returns
    it as a failure carrying prose, so the outcome is `failed` rather than `refused`, nothing can
    join on the reason, and the reporter — which never runs git — cannot predict it at all. Three
    merged branches printed `→ delete_local` and would each have hit that.
    """
    if action is Action.FF_WORKTREE and state.worktree is None:
        return Refusal.NO_WORKTREE
    if state.worktree is None:
        return None
    if action in (Action.FF_REF, Action.DELETE_LOCAL):
        return Refusal.WORKTREE_CHECKOUT
    if action in (Action.FF_WORKTREE, Action.FAST_FORWARD) and state.worktree_dirty:
        return Refusal.WORKTREE_DIRTY
    return None


def protection_refusal(action: Action, state: BranchState, policy: Policy) -> Refusal | None:
    """Why `action` is refused on a protected branch, or None if the branch admits it.

    Shared with the reporter rather than living inside execute(), because protection is static
    config and therefore knowable without running anything: a report-only run that printed the
    decided `push` alone would promise a push that `--apply` is never going to make.
    """
    if action in PROTECTED_ALLOWED:
        return None
    return None if matching_protection(state.branch, policy) is None else Refusal.PROTECTED


def blocking_refusal(action: Action, state: BranchState, policy: Policy, dirty: bool) -> Refusal | None:
    """The reason `apply` would refuse `action`, when that is knowable without running anything.

    The three static gates in one place, so a caller cannot consult two of them and miss the
    third. Anything a guard can only discover mid-write (a rebase conflict, unreadable counts)
    is deliberately absent — a row must not promise a refusal any more than it promises an action.
    """
    return (
        protection_refusal(action, state, policy)
        or checkout_refusal(action, state)
        or worktree_refusal(action, state)
        or dirty_refusal(action, state, dirty)
    )


def describe_block(reason: Refusal, action: Action, state: BranchState, policy: Policy) -> str:
    """A blocking refusal's wording, with the runtime values its template needs filled in.

    The action supplies `{verb}`. Left to _DOC_FIELDS it renders as 'this action', which is the
    right stand-in for a reference page listing refusals in the abstract and the wrong one on a
    row that already names the action two words earlier.
    """
    pattern = matching_protection(state.branch, policy)
    return REFUSAL_TEXT[reason].format_map({**_DOC_FIELDS, 'verb': action.value, 'pattern': pattern or '<glob>'})


def execute(action: Action, state: BranchState, repo: Repo, policy: Policy) -> Outcome:
    """Perform `action` on `state.branch`, enforcing the hard invariants. Never forces.

    `policy` is passed for the settings a guard has to consult live (`merge_target`,
    `protected`); it can never widen what an action is allowed to do — the invariants above hold
    whatever it says.
    """
    # Invariant 8, before dispatch so it covers every action, including any added later.
    if protection_refusal(action, state, policy) is not None:
        return _refused(state, action, Refusal.PROTECTED, pattern=matching_protection(state.branch, policy))

    if action == Action.SKIP:
        return Outcome(branch=state.branch, action=action, status='skipped')
    if action == Action.REPORT:
        return Outcome(branch=state.branch, action=action, status='reported')
    if action == Action.PROMPT:
        # Interactive traverse is deferred to a later slice; degrade to a report.
        return Outcome(branch=state.branch, action=action, status='reported', message='interactive prompt not implemented (v1)')
    return _MUTATORS[action](state, repo, policy)
