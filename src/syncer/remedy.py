"""Turn a state syncer will not resolve into the command that resolves it. Pure: no git, no FS.

The sync report is good at saying what is true and silent on what to do about it. For a row
syncer acts on that silence is right — `apply` clears it. For every other row the reader is left
to reconstruct a command from a branch name, an upstream and a checkout state that are all on
screen but never assembled, and the commonest ones are exactly the ones with a wrinkle: the
upstream is not `origin/<branch>`, or the branch lives in a worktree three directories away.

Kept out of the pipeline and free of I/O for the same reason policy.decide() is: what to suggest
is enumerable over (state x action x refusal), so it is a truth table rather than something to
discover by running git. Nothing here can change what an action does — `decide()` never sees it.

The honesty rules, which are testable statements rather than intentions:

1. **Never invent a remedy.** A combination with no honest answer yields nothing. A confident
   wrong command is worse than silence, because a report that has misled you once about what to
   run is one you stop pasting from — and pasting from it is the whole point.
2. **Never a command that can lose work.** No --force, no `reset --hard`, no `checkout <file>`,
   no `stash drop`. Invariant 1 governs what syncer runs; this governs what it tells you to run,
   and a tool whose entire claim is that it never forces must not hand you the argv that does.
   Nothing below needs one: rebasing a branch onto *its own upstream* leaves it strictly ahead,
   so the publish that follows is an ordinary push. A force is only ever wanted when rebasing
   onto some *other* base, which syncer neither does nor suggests.
3. **Name the directory the fact is actually about.** A branch checked out in a linked worktree is
   not fixed from the main checkout — a rebase run in the repo rebases whatever *that* has checked
   out. The inverse holds for anything measured on the repo rather than the branch: the dirty tree
   is the repo's own, and pointing `status` at the worktree printed nothing at all. So a remedy
   opens with the `cd` that puts every command under it somewhere it is true.
4. **Never tell anyone what to do with their uncommitted work.** syncer has no standing to decide
   between committing and stashing, which is why it has no action for either. It says where the
   changes are and stops.
"""

from __future__ import annotations

from dataclasses import dataclass

from syncer.execute import ACTION_DOCS
from syncer.execute import MUTATING_ACTIONS
from syncer.execute import Refusal
from syncer.policy import MECHANISM_ACTIONS
from syncer.policy import Action
from syncer.policy import BranchState
from syncer.policy import PrimaryState


@dataclass(frozen=True)
class Remedy:
    """What to run, and the fact that makes it make sense.

    Split because they render differently and one is optional: a command is copy-pasteable and
    highlighted, a note is the thing you would otherwise have to work out from three other lines
    of the report. Either may be empty; a Remedy with neither is falsy and never rendered.
    """

    commands: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.commands or self.notes)


def _in(directory: str, *commands: str) -> tuple[str, ...]:
    """The directory once, then the commands as you would type them standing in it.

    Honesty rule 3 wants every command anchored somewhere; `git -C <dir>` anchored each one
    separately and repeated the path on every line, which is what a reader has to look past to
    see the command. One `cd` says the same thing once.
    """
    return (f'cd {directory}', *commands)


def _tracks_own_remote(state: BranchState) -> bool:
    """Whether the upstream is this branch's own counterpart rather than some base branch.

    The difference decides both commands below. A branch tracking `origin/main` is diverged the
    moment main moves, and the fix is a rebase that needs no publish; a branch tracking
    `origin/<itself>` is diverged because two machines both committed, and the fix ends in a push.
    """
    return state.upstream is not None and state.upstream.rpartition('/')[2] == state.branch


def _diverged(state: BranchState, where: str) -> Remedy:
    upstream = state.upstream or ''
    if _tracks_own_remote(state):
        return Remedy(
            commands=_in(where, f'git rebase {upstream}', f'git push origin {state.branch}'),
            notes=(f'replays your {state.ahead} commit(s) onto {upstream}, leaving an ordinary push — no force needed.',),
        )
    return Remedy(
        commands=_in(where, f'git rebase {upstream}'),
        notes=(
            f'{state.branch} tracks {upstream}, not origin/{state.branch} — so this is your work sitting on a base that moved,',
            f'not two machines disagreeing. Publish it separately with: git push -u origin {state.branch}',
        ),
    )


def _state_remedy(state: BranchState, where: str) -> Remedy:
    """What to run for a branch state syncer has decided not to act on."""
    if state.primary is PrimaryState.DIVERGED:
        return _diverged(state, where)
    if state.primary is PrimaryState.AHEAD:
        if _tracks_own_remote(state):
            return Remedy(commands=_in(where, f'git push origin {state.branch}'))
        return Remedy(
            commands=_in(where, f'git push -u origin {state.branch}'),
            notes=(f'{state.branch} tracks {state.upstream}, so this publishes it under its own name and retargets it there.',),
        )
    if state.primary is PrimaryState.BEHIND:
        return Remedy(commands=_in(where, f'git merge --ff-only {state.upstream or ""}'))
    if state.primary is PrimaryState.NO_UPSTREAM:
        return Remedy(commands=_in(where, f'git push -u origin {state.branch}'))
    if state.primary is PrimaryState.GONE:
        # -d, never -D. syncer uses -D behind a guard that proves the target holds the work by
        # ancestry or patch equivalence; run by hand there is no such guard, and -d is git's own.
        return Remedy(
            commands=_in(where, f'git branch -d {state.branch}'),
            notes=('-d refuses unless the work is merged, which is the check syncer runs for itself before using -D.',),
        )
    if state.primary is PrimaryState.DETACHED:
        return Remedy(
            commands=_in(where, 'git switch -'),
            notes=('a commit made here belongs to no branch — `git switch -` returns to the last one you were on.',),
        )
    return Remedy()


# Refusals with deliberately nothing to suggest, listed rather than left to fall through. Each is
# either syncer describing its own dispatch (the branch was not in the state this action is for)
# or a git read that failed, where the actionable text is git's own stderr and the failure summary
# already carries it. A new Refusal in neither this set nor _refusal_remedy fails a test rather
# than silently rendering blank — the direction ACTION_DOCS and PROTECTED_ALLOWED both take.
NO_REMEDY = frozenset(
    {
        Refusal.NOT_CURRENT,
        Refusal.IS_CURRENT,
        Refusal.NO_WORKTREE,
        Refusal.NO_UPSTREAM,
        Refusal.HAS_UPSTREAM,
        Refusal.NOT_STRICTLY_BEHIND,
        Refusal.COUNTS_UNREADABLE,
        Refusal.NOTHING_TO_PUSH,
        Refusal.DIVERGED,
        Refusal.NOT_DIVERGED,
        Refusal.NOT_GONE,
        Refusal.DELETE_CURRENT,
        Refusal.DELETE_DEFAULT,
        Refusal.DELETE_MERGE_TARGET,
    }
)


def _worktree_checkout(state: BranchState, action: Action, repo_path: str) -> Remedy:
    """What a linked worktree holding the branch leaves the reader to do, per action.

    The two are opposite errands and one command cannot serve both. Advancing the branch happens
    *inside* the worktree, where the tree moves with the ref. Deleting it happens from the main
    checkout and has to dispose of the worktree first — git will not delete a ref a worktree holds.
    """
    if action is Action.DELETE_LOCAL:
        return Remedy(
            commands=_in(repo_path, f'git worktree remove {state.worktree or ""}', f'git branch -d {state.branch}'),
            notes=(
                f'{state.branch} is merged, but the worktree at {state.worktree} still holds the ref, which git will not delete.',
                'Neither command can lose work: `git worktree remove` refuses a tree with modified or untracked files,',
                'and `git branch -d` refuses a branch whose work is unmerged.',
            ),
        )
    return Remedy(
        commands=_in(state.worktree or repo_path, f'git merge --ff-only {state.upstream or ""}'),
        notes=(f'{state.branch} is checked out at {state.worktree} — run it there, where the tree moves with the ref.',),
    )


def _refusal_remedy(reason: Refusal, state: BranchState, action: Action, repo_path: str) -> Remedy:
    where = state.worktree or repo_path
    if reason is Refusal.DIRTY_TREE:
        # Honesty rule 4: where the changes are, and nothing about what to do with them. Rule 3
        # inverts here — the tree syncer measured is the repo's own, never the worktree's, so
        # naming `where` sent the reader to run `status` somewhere it prints nothing.
        return Remedy(
            commands=_in(repo_path, 'git status --short'),
            notes=('syncer refuses every action that touches a dirty tree, and has no action that would clear one.',),
        )
    if reason is Refusal.WORKTREE_CHECKOUT:
        return _worktree_checkout(state, action, repo_path)
    if reason is Refusal.WORKTREE_DIRTY:
        # Honesty rule 4 again, and rule 3 the other way from DIRTY_TREE: this tree really is the
        # worktree's, and it is the one thing between the branch and an ordinary fast-forward.
        return Remedy(
            commands=_in(state.worktree or repo_path, 'git status --short'),
            notes=(
                f'syncer would fast-forward {state.branch} in {state.worktree}, but will not merge into a tree with changes in it.',
                'Deal with them however you like and the next run clears the branch.',
            ),
        )
    if reason is Refusal.PROTECTED:
        return Remedy(
            notes=(
                f'{state.branch} matches a `protected` pattern in config.toml, which admits nothing that publishes or destroys.',
                'Run the command yourself, or narrow the pattern: syncer config edit config',
            ),
        )
    if reason is Refusal.REBASE_CONFLICT:
        return Remedy(
            commands=_in(where, f'git rebase {state.upstream or ""}'),
            notes=('syncer aborted the rebase rather than leave it half-finished, so the tree is exactly as it was.',),
        )
    if reason is Refusal.NOT_INTEGRATED:
        # No command: the target is the policy's merge_target or the repo's default branch, and
        # naming the wrong one here would be a `git log` whose empty output reads as "nothing to
        # lose". The refusal message carries the target it actually used.
        return Remedy(
            notes=(
                f'the merge target does not provably hold {state.branch}, by ancestry or by patch equivalence.',
                f'in {where}, `git log --oneline <target>..{state.branch}` is what deleting it would lose.',
            ),
        )
    if reason is Refusal.NO_MERGE_TARGET:
        return Remedy(notes=('set `merge_target` on the policy in config.toml — the default branch could not be resolved.',))
    return Remedy()


def _policy_note(state: BranchState, action: Action) -> tuple[str, ...]:
    """The rule that decided this row, when a safe action exists for the state and was not chosen.

    The other half of "why did syncer not do this": not every unresolved row is a limitation, and
    most are a policy saying report. Naming the rule key turns the report into something editable
    — the alternative is deriving it from `policy rules` by hand, per row.

    Candidates come from ACTION_DOCS.applies_to, which is proven against the mutators themselves,
    so this cannot advertise an action that would only be refused.
    """
    if action in MUTATING_ACTIONS:
        return ()
    candidates = sorted(
        act.value
        for act, doc in ACTION_DOCS.items()
        if act in MUTATING_ACTIONS and act not in MECHANISM_ACTIONS and state.primary in doc.applies_to
    )
    if not candidates:
        return ()
    selector = 'default' if state.is_default else '*'
    return (f'syncer chose `{action.value}` here — a policy could do it for you: {selector}:{state.primary.value} = {candidates[0]}',)


def remedy_for(state: BranchState, action: Action, repo_path: str, blocked: Refusal | None, refused: Refusal | None) -> Remedy:
    """What the reader can run for one row, or an empty Remedy when syncer has this covered.

    `blocked` is the gate a report-only run already knows about; `refused` is what an executed one
    actually hit. Refusals win — an apply that ran has the more specific answer, and it is the one
    the reader just watched happen.

    Nothing is returned for a row syncer will clear itself. `apply` is the remedy there, and
    printing a git command beside a queued action invites someone to race it.
    """
    where = state.worktree or repo_path
    reason = refused or blocked
    if reason is not None:
        remedy = _refusal_remedy(reason, state, action, repo_path)
        if remedy:
            return remedy
    if action in MUTATING_ACTIONS and blocked is None and refused is None:
        return Remedy()
    if state.primary is PrimaryState.SYNCED:
        return Remedy()
    remedy = _state_remedy(state, where)
    return Remedy(commands=remedy.commands, notes=remedy.notes + _policy_note(state, action))
