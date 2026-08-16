from __future__ import annotations

import pytest

from syncer.execute import ACTION_DOCS
from syncer.execute import MUTATING_ACTIONS
from syncer.execute import Refusal
from syncer.policy import MECHANISM_ACTIONS
from syncer.policy import Action
from syncer.policy import BranchState
from syncer.policy import PrimaryState
from syncer.remedy import NO_REMEDY
from syncer.remedy import Remedy
from syncer.remedy import _refusal_remedy
from syncer.remedy import remedy_for

REPO = '/home/u/repos/thing'
WORKTREE = '/home/u/.worktrees/thing/feature'

# Every argv a remedy must never contain. The point of the module is that a report you paste from
# cannot cost you work, and that only holds if it is asserted over the whole table rather than
# reviewed per string.
#
# Compared as whole argv tokens, never as substrings: `-f` is inside `--ff-only`, and a check that
# reads a fast-forward as a force is one that gets relaxed rather than trusted.
DESTRUCTIVE = frozenset({'--force', '-f', '--force-with-lease', 'reset', 'checkout', 'restore', 'stash', 'clean', '-D'})


def _state(primary: PrimaryState, **overrides) -> BranchState:
    base = {
        'branch': 'feature',
        'primary': primary,
        'upstream': 'origin/feature',
        'is_current': False,
        'ahead': 1 if primary in (PrimaryState.AHEAD, PrimaryState.DIVERGED) else 0,
        'behind': 1 if primary in (PrimaryState.BEHIND, PrimaryState.DIVERGED) else 0,
    }
    return BranchState(**{**base, **overrides})


def _text(remedy: Remedy) -> str:
    return '\n'.join(remedy.commands + remedy.notes)


class TestNothingSuggestedWhereSyncerActs:
    """A command beside queued work invites someone to race apply, and duplicates it at best."""

    @pytest.mark.parametrize('action', [action for action in Action if action in MUTATING_ACTIONS])
    def test_an_unblocked_mutating_action_gets_no_remedy(self, action):
        state = _state(PrimaryState.BEHIND)
        assert not remedy_for(state, action, REPO, blocked=None, refused=None)

    def test_a_synced_branch_gets_no_remedy(self):
        assert not remedy_for(_state(PrimaryState.SYNCED), Action.SKIP, REPO, None, None)

    def test_a_synced_branch_with_a_dirty_tree_says_only_where_the_changes_are(self):
        """syncer has no action that clears a dirty tree and no standing to choose between commit
        and stash, so the remedy names the tree and stops."""
        remedy = remedy_for(_state(PrimaryState.SYNCED), Action.SKIP, REPO, Refusal.DIRTY_TREE, None)
        assert remedy.commands == (f'git -C {REPO} status --short',)
        assert not any(word in _text(remedy) for word in ('stash', 'commit', 'reset'))


class TestDivergedNamesTheRightBase:
    """The case the whole module was written for: a report saying `1 ahead, 1 behind` on a branch
    whose upstream is not its own remote counterpart, which is what an unlanded worktree branch
    looks like and is not what the words 'diverged' lead you to run."""

    def test_a_branch_tracking_a_base_rebases_and_needs_no_force(self):
        state = _state(PrimaryState.DIVERGED, upstream='origin/main', worktree=WORKTREE)
        remedy = remedy_for(state, Action.REPORT, REPO, None, None)
        assert remedy.commands == (f'git -C {WORKTREE} rebase origin/main',)
        assert 'tracks origin/main, not origin/feature' in _text(remedy)

    def test_a_branch_tracking_its_own_remote_rebases_then_pushes_plainly(self):
        remedy = remedy_for(_state(PrimaryState.DIVERGED), Action.REPORT, REPO, None, None)
        assert remedy.commands == (f'git -C {REPO} rebase origin/feature', f'git -C {REPO} push origin feature')
        assert 'no force needed' in _text(remedy)

    def test_the_command_runs_where_the_branch_actually_is(self):
        """Honesty rule 3. `git -C <repo> rebase` in the main checkout rebases whatever *it* has
        checked out, which is a different branch entirely."""
        state = _state(PrimaryState.DIVERGED, upstream='origin/main', worktree=WORKTREE)
        assert all(WORKTREE in command for command in remedy_for(state, Action.REPORT, REPO, None, None).commands)


class TestStateRemedies:
    def test_no_upstream_publishes_and_sets_one(self):
        state = _state(PrimaryState.NO_UPSTREAM, upstream=None)
        assert remedy_for(state, Action.REPORT, REPO, None, None).commands == (f'git -C {REPO} push -u origin feature',)

    def test_gone_uses_lowercase_d(self):
        """syncer's own -D is safe only behind a guard proving the target holds the work. Run by
        hand there is no such guard, and -d is git's version of the same check."""
        remedy = remedy_for(_state(PrimaryState.GONE), Action.REPORT, REPO, None, None)
        assert remedy.commands == (f'git -C {REPO} branch -d feature',)

    def test_ahead_on_a_base_upstream_explains_the_retarget(self):
        state = _state(PrimaryState.AHEAD, upstream='origin/main')
        remedy = remedy_for(state, Action.REPORT, REPO, None, None)
        assert remedy.commands == (f'git -C {REPO} push -u origin feature',)
        assert 'under its own name' in _text(remedy)

    def test_detached_returns_to_a_branch(self):
        state = _state(PrimaryState.DETACHED, branch='(detached)', upstream=None, is_current=True)
        assert remedy_for(state, Action.REPORT, REPO, None, None).commands == (f'git -C {REPO} switch -',)


class TestPolicyNote:
    """The other half of 'why did syncer not do this'. Most unresolved rows are a policy saying
    report, not a limit, and the rule key is what makes that editable."""

    def test_it_names_the_rule_and_an_action_that_applies(self):
        note = _text(remedy_for(_state(PrimaryState.DIVERGED), Action.REPORT, REPO, None, None))
        assert '*:diverged = rebase_push' in note

    def test_the_default_branch_gets_the_default_selector(self):
        state = _state(PrimaryState.AHEAD, is_default=True, branch='main', upstream='origin/main')
        assert 'default:ahead = push' in _text(remedy_for(state, Action.REPORT, REPO, None, None))

    def test_it_never_suggests_a_mechanism_action(self):
        """A rule naming pull_ff or ff_ref is refused for half of all checkout states, which is
        why the built-ins name the intent. A suggestion is under the same rule."""
        note = _text(remedy_for(_state(PrimaryState.BEHIND), Action.REPORT, REPO, None, None))
        assert 'fast_forward' in note
        assert not any(action.value in note.split('= ')[-1] for action in MECHANISM_ACTIONS)

    def test_no_note_where_no_safe_action_exists(self):
        state = _state(PrimaryState.DETACHED, branch='(detached)', upstream=None, is_current=True)
        assert 'a policy could do it' not in _text(remedy_for(state, Action.REPORT, REPO, None, None))

    def test_a_state_with_a_mutator_always_has_a_candidate(self):
        """Guards the note against ACTION_DOCS drifting: every state some mutator applies to must
        still produce a suggestion, or the report quietly stops explaining itself."""
        actionable = {
            state
            for action, doc in ACTION_DOCS.items()
            if action in MUTATING_ACTIONS and action not in MECHANISM_ACTIONS
            for state in doc.applies_to
        }
        for primary in actionable:
            note = _text(remedy_for(_state(primary, upstream='origin/feature'), Action.REPORT, REPO, None, None))
            assert f':{primary.value} = ' in note, primary


class TestRefusalRemedies:
    def test_a_worktree_refusal_points_at_the_worktree(self):
        state = _state(PrimaryState.BEHIND, worktree=WORKTREE)
        remedy = remedy_for(state, Action.FAST_FORWARD, REPO, Refusal.WORKTREE_CHECKOUT, None)
        assert remedy.commands == (f'git -C {WORKTREE} merge --ff-only origin/feature',)
        assert WORKTREE in _text(remedy)

    def test_a_worktree_refusal_on_a_delete_disposes_of_the_worktree_first(self):
        """The same refusal, the opposite errand. `merge --ff-only` on a branch whose remote is
        gone advances nothing, and the reader is left with the row unexplained."""
        state = _state(PrimaryState.GONE, worktree=WORKTREE)
        remedy = remedy_for(state, Action.DELETE_LOCAL, REPO, Refusal.WORKTREE_CHECKOUT, None)
        assert remedy.commands == (
            f'git -C {REPO} worktree remove {WORKTREE}',
            f'git -C {REPO} branch -d feature',
        )

    def test_a_dirty_refusal_names_the_repo_even_when_the_branch_is_elsewhere(self):
        """Honesty rule 3 inverts for a repo-scoped fact. The tree syncer measured is the repo's
        own, so pointing `status` at the worktree sent the reader somewhere it prints nothing —
        under a line telling them their tree was dirty."""
        state = _state(PrimaryState.GONE, worktree=WORKTREE)
        remedy = remedy_for(state, Action.DELETE_LOCAL, REPO, Refusal.DIRTY_TREE, None)
        assert remedy.commands == (f'git -C {REPO} status --short',)

    def test_protection_names_the_file_to_edit_and_offers_no_command(self):
        remedy = remedy_for(_state(PrimaryState.AHEAD), Action.PUSH, REPO, Refusal.PROTECTED, None)
        assert remedy.commands == ()
        assert 'config.toml' in _text(remedy)

    def test_an_executed_refusal_wins_over_the_predicted_block(self):
        """apply has the more specific answer, and it is the one the reader just watched happen."""
        state = _state(PrimaryState.DIVERGED, is_current=True)
        remedy = remedy_for(state, Action.REBASE_PUSH, REPO, None, Refusal.REBASE_CONFLICT)
        assert 'aborted the rebase' in _text(remedy)

    def test_a_refusal_with_no_remedy_falls_through_to_the_state(self):
        """NOT_CURRENT is syncer describing its own dispatch, not something to act on — but the
        branch underneath it is still diverged, and that is what the reader needs."""
        state = _state(PrimaryState.DIVERGED, upstream='origin/main', worktree=WORKTREE)
        remedy = remedy_for(state, Action.REBASE_PUSH, REPO, Refusal.NOT_CURRENT, None)
        assert remedy.commands == (f'git -C {WORKTREE} rebase origin/main',)


class TestHonestyRules:
    def test_no_remedy_can_lose_work(self):
        """Honesty rule 2, over the whole table rather than string by string. Invariant 1 governs
        what syncer runs; nothing is gained by that if the report tells you to force by hand."""
        for primary in PrimaryState:
            for upstream in ('origin/feature', 'origin/main', None):
                for reason in (None, *Refusal):
                    for action in Action:
                        remedy = remedy_for(
                            _state(primary, upstream=upstream, worktree=WORKTREE),
                            action,
                            REPO,
                            reason,
                            None,
                        )
                        for command in remedy.commands:
                            assert not DESTRUCTIVE & set(command.split()), (primary, action, reason, command)

    def test_every_refusal_is_answered_or_deliberately_silent(self):
        """A new Refusal must be routed or listed, never left to render blank — the direction
        ACTION_DOCS and PROTECTED_ALLOWED both take."""
        state = _state(PrimaryState.BEHIND, worktree=WORKTREE)
        for reason in Refusal:
            handled = bool(_refusal_remedy(reason, state, Action.REPORT, REPO))
            assert handled is (reason not in NO_REMEDY), reason

    def test_no_remedy_set_and_the_handled_set_do_not_overlap(self):
        state = _state(PrimaryState.BEHIND, worktree=WORKTREE)
        assert not any(_refusal_remedy(reason, state, Action.REPORT, REPO) for reason in NO_REMEDY)

    def test_every_command_names_a_directory(self):
        """Honesty rule 3, structurally: a bare `git rebase` is right only if you happen to be
        standing in the repo, and the report is read from somewhere else."""
        for primary in PrimaryState:
            remedy = remedy_for(_state(primary, upstream='origin/main'), Action.REPORT, REPO, None, None)
            for command in remedy.commands:
                assert command.startswith('git -C /'), command
