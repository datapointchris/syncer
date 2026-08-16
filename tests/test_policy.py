import itertools

import pytest

from syncer.policy import BUILTIN_POLICIES
from syncer.policy import MECHANISM_ACTIONS
from syncer.policy import PROTECTED_ALLOWED
from syncer.policy import Action
from syncer.policy import BranchState
from syncer.policy import Policy
from syncer.policy import PrimaryState
from syncer.policy import Scope
from syncer.policy import decide
from syncer.policy import matching_protection

ALL_STATES = list(PrimaryState)


def _state(primary, *, branch='feature/x', is_default=False, is_current=False, dirty=False, stashed=False):
    return BranchState(
        branch=branch,
        primary=primary,
        is_default=is_default,
        is_current=is_current,
        dirty=dirty,
        stashed=stashed,
    )


# Expected action for the built-in `standard` policy, indexed by (is_default, primary).
# This is the reference truth table asserted against decide() below.
_STANDARD_DEFAULT = {
    PrimaryState.SYNCED: Action.SKIP,
    PrimaryState.AHEAD: Action.PUSH,
    PrimaryState.BEHIND: Action.FAST_FORWARD,
    PrimaryState.DIVERGED: Action.REBASE_PUSH,
    PrimaryState.NO_UPSTREAM: Action.REPORT,  # no default:no_upstream rule → falls to *:no_upstream
    PrimaryState.GONE: Action.REPORT,
    PrimaryState.DETACHED: Action.REPORT,  # no rule anywhere → fallback
}
_STANDARD_FEATURE = {
    PrimaryState.SYNCED: Action.REPORT,  # no *:synced rule → fallback
    PrimaryState.AHEAD: Action.REPORT,
    PrimaryState.BEHIND: Action.FAST_FORWARD,
    PrimaryState.DIVERGED: Action.REPORT,
    PrimaryState.NO_UPSTREAM: Action.REPORT,
    PrimaryState.GONE: Action.REPORT,
    PrimaryState.DETACHED: Action.REPORT,
}
_MIRROR_DEFAULT = {
    PrimaryState.SYNCED: Action.REPORT,  # no default:synced and no *:synced → fallback
    PrimaryState.AHEAD: Action.PUSH,  # falls through to *:ahead
    PrimaryState.BEHIND: Action.FAST_FORWARD,
    PrimaryState.DIVERGED: Action.REBASE_PUSH,
    PrimaryState.NO_UPSTREAM: Action.REPORT,
    PrimaryState.GONE: Action.DELETE_LOCAL,
    PrimaryState.DETACHED: Action.REPORT,
}
_MIRROR_FEATURE = {
    PrimaryState.SYNCED: Action.REPORT,
    PrimaryState.AHEAD: Action.PUSH,
    PrimaryState.BEHIND: Action.FAST_FORWARD,
    PrimaryState.DIVERGED: Action.REBASE_PUSH,
    PrimaryState.NO_UPSTREAM: Action.REPORT,
    PrimaryState.GONE: Action.DELETE_LOCAL,
    PrimaryState.DETACHED: Action.REPORT,
}


class TestDecideStandard:
    @pytest.mark.parametrize('primary', ALL_STATES)
    def test_default_branch(self, primary):
        state = _state(primary, branch='main', is_default=True, is_current=True)
        assert decide(state, BUILTIN_POLICIES['standard']) == _STANDARD_DEFAULT[primary]

    @pytest.mark.parametrize('primary', ALL_STATES)
    def test_feature_branch(self, primary):
        state = _state(primary)
        assert decide(state, BUILTIN_POLICIES['standard']) == _STANDARD_FEATURE[primary]


class TestDecideMirror:
    @pytest.mark.parametrize('primary', ALL_STATES)
    def test_default_branch(self, primary):
        state = _state(primary, branch='main', is_default=True, is_current=True)
        assert decide(state, BUILTIN_POLICIES['mirror']) == _MIRROR_DEFAULT[primary]

    @pytest.mark.parametrize('primary', ALL_STATES)
    def test_feature_branch(self, primary):
        state = _state(primary)
        assert decide(state, BUILTIN_POLICIES['mirror']) == _MIRROR_FEATURE[primary]


class TestDecideObserve:
    @pytest.mark.parametrize('primary', ALL_STATES)
    @pytest.mark.parametrize('is_default', [True, False])
    def test_everything_is_report(self, primary, is_default):
        state = _state(primary, is_default=is_default, is_current=is_default)
        assert decide(state, BUILTIN_POLICIES['observe']) == Action.REPORT


class TestBuiltinsNameIntentNotMechanism:
    """Each mechanism covers one place a branch can be checked out, so a built-in naming one is
    refused everywhere the branch is somewhere else. Both built-ins did exactly that:
    `default:behind = pull_ff` never ran unless the default branch happened to be checked out,
    and `*:behind = ff_ref` never ran when it was.

    Asserted against MECHANISM_ACTIONS rather than a list of members, so a mechanism added later
    is covered without anyone remembering this test — which is how ff_worktree arrived."""

    @pytest.mark.parametrize('policy_name', list(BUILTIN_POLICIES))
    @pytest.mark.parametrize('primary', ALL_STATES)
    def test_no_builtin_decides_a_mechanism_action(self, policy_name, primary):
        policy = BUILTIN_POLICIES[policy_name]
        for is_default, is_current in itertools.product([True, False], repeat=2):
            state = _state(primary, branch='main', is_default=is_default, is_current=is_current)
            assert decide(state, policy) not in MECHANISM_ACTIONS


class TestDecideModifierInvariance:
    """decide() is a function of primary state + role/name only; dirty/stashed are
    execute-time gates and must never change the decision (design invariant)."""

    @pytest.mark.parametrize('policy_name', list(BUILTIN_POLICIES))
    @pytest.mark.parametrize('primary', ALL_STATES)
    def test_dirty_stashed_do_not_affect_decision(self, policy_name, primary):
        policy = BUILTIN_POLICIES[policy_name]
        for is_default, is_current in itertools.product([True, False], repeat=2):
            baseline = decide(_state(primary, branch='main', is_default=is_default, is_current=is_current), policy)
            for dirty, stashed in itertools.product([True, False], repeat=2):
                state = _state(
                    primary,
                    branch='main',
                    is_default=is_default,
                    is_current=is_current,
                    dirty=dirty,
                    stashed=stashed,
                )
                assert decide(state, policy) == baseline

    @pytest.mark.parametrize('policy_name', list(BUILTIN_POLICIES))
    @pytest.mark.parametrize('primary', ALL_STATES)
    def test_a_linked_worktree_does_not_affect_the_decision(self, policy_name, primary):
        """`worktree` joined dirty and stashed as an execute-time gate, so it is under the same
        rule: it decides whether an action is refused, never which action is chosen. A policy
        that decided differently for a worktree branch would be deciding on where a checkout
        happens to live, which is not a property of the branch."""
        policy = BUILTIN_POLICIES[policy_name]
        for is_default, is_current in itertools.product([True, False], repeat=2):
            state = _state(primary, branch='main', is_default=is_default, is_current=is_current)
            assert decide(state.model_copy(update={'worktree': '/elsewhere/wt'}), policy) == decide(state, policy)


class TestSelectorPrecedence:
    def test_exact_name_beats_glob_and_star(self):
        policy = Policy(
            name='p',
            rules={
                'release/1.0:ahead': 'push',
                'release/*:ahead': 'ff_ref',
                '*:ahead': 'report',
            },
        )
        state = _state(PrimaryState.AHEAD, branch='release/1.0')
        assert decide(state, policy) == Action.PUSH

    def test_glob_beats_star(self):
        policy = Policy(name='p', rules={'release/*:ahead': 'ff_ref', '*:ahead': 'report'})
        state = _state(PrimaryState.AHEAD, branch='release/1.0')
        assert decide(state, policy) == Action.FF_REF

    def test_more_specific_glob_wins(self):
        policy = Policy(name='p', rules={'release/*:ahead': 'report', 'release/v*:ahead': 'push'})
        # 'release/v*' is longer/more specific than 'release/*'
        state = _state(PrimaryState.AHEAD, branch='release/v2')
        assert decide(state, policy) == Action.PUSH

    def test_role_default_beats_star(self):
        policy = Policy(name='p', rules={'default:ahead': 'push', '*:ahead': 'report'})
        state = _state(PrimaryState.AHEAD, branch='main', is_default=True)
        assert decide(state, policy) == Action.PUSH

    def test_default_role_beats_current_role(self):
        policy = Policy(name='p', rules={'default:ahead': 'push', 'current:ahead': 'report'})
        state = _state(PrimaryState.AHEAD, branch='main', is_default=True, is_current=True)
        assert decide(state, policy) == Action.PUSH

    def test_falls_through_to_fallback(self):
        policy = Policy(name='p', rules={'default:behind': 'pull_ff'}, fallback=Action.SKIP)
        state = _state(PrimaryState.AHEAD)
        assert decide(state, policy) == Action.SKIP


class TestPolicyValidation:
    def test_unknown_action_rejected(self):
        with pytest.raises(ValueError, match='unknown action'):
            Policy(name='p', rules={'*:ahead': 'nuke'})

    def test_unknown_state_rejected(self):
        with pytest.raises(ValueError, match='unknown state'):
            Policy(name='p', rules={'*:teleported': 'report'})

    def test_missing_colon_rejected(self):
        with pytest.raises(ValueError, match='selector.*:.*state'):
            Policy(name='p', rules={'default-behind': 'report'})

    def test_empty_selector_rejected(self):
        with pytest.raises(ValueError, match='empty selector'):
            Policy(name='p', rules={':behind': 'report'})

    def test_unknown_scope_rejected(self):
        with pytest.raises(ValueError):
            Policy(name='p', scope='everything')

    def test_unknown_fallback_rejected(self):
        with pytest.raises(ValueError):
            Policy(name='p', fallback='nuke')

    def test_valid_policy_accepts_all_scopes(self):
        for scope in Scope:
            assert Policy(name='p', scope=scope).scope == scope


class TestProtectedBranches:
    """`protected` is machine-local, like every other policy setting: it lives on a Policy, and
    policies only ever come from config.toml. The registry carries no policy fields at all
    beyond the portable `sync_policy` name hint, so protection cannot be set for every machine
    at once."""

    def test_no_builtin_protects_anything(self):
        """A built-in with a protected list would be a global default that every machine
        inherits — which is exactly what the setting must not be."""
        assert all(policy.protected == [] for policy in BUILTIN_POLICIES.values())

    def test_defaults_to_protecting_nothing(self):
        assert Policy(name='p').protected == []

    def test_matching_returns_the_pattern_responsible(self):
        policy = Policy(name='p', protected=['develop', 'release/*'])
        assert matching_protection('develop', policy) == 'develop'
        assert matching_protection('release/2.0', policy) == 'release/*'
        assert matching_protection('feature/x', policy) is None

    def test_allowlist_covers_only_actions_that_neither_publish_nor_destroy(self):
        """An allowlist, so a new Action is refused on a protected branch by default. Anything
        added here has to be provably incapable of publishing local work or losing it."""
        assert set(PROTECTED_ALLOWED) == {
            Action.SKIP,
            Action.REPORT,
            Action.PROMPT,
            Action.FAST_FORWARD,
            Action.PULL_FF,
            Action.FF_WORKTREE,
            Action.FF_REF,
        }
        assert not PROTECTED_ALLOWED & {Action.PUSH, Action.REBASE_PUSH, Action.SET_UPSTREAM_PUSH, Action.DELETE_LOCAL}

    def test_decide_is_invariant_to_protection(self):
        """Protection is an execute-time gate, never a decision input — the same split that
        keeps dirty/stashed out of decide(). The reporter surfaces the refusal separately."""
        bare = Policy(name='p', rules={'*:ahead': 'push'})
        guarded = Policy(name='p', rules={'*:ahead': 'push'}, protected=['develop'])
        state = BranchState(branch='develop', primary=PrimaryState.AHEAD)
        assert decide(state, bare) == decide(state, guarded) == Action.PUSH
