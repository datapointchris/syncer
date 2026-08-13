"""Pure sync-policy core: state taxonomy, action catalog, and the decide() rules engine.

This module is intentionally free of any git or filesystem I/O. `decide()` is a pure
function of (BranchState, Policy), which is what makes the full cartesian product of
states x policies cheap to enumerate and exhaustively test (see tests/test_policy.py).
The impure half — turning real git state into a BranchState — lives in classify.py.
"""

from __future__ import annotations

import fnmatch
from enum import StrEnum

from pydantic import BaseModel
from pydantic import field_validator


class PrimaryState(StrEnum):
    """Mutually exclusive, exhaustive per-branch state, computed after fetch --prune."""

    SYNCED = 'synced'
    AHEAD = 'ahead'
    BEHIND = 'behind'
    DIVERGED = 'diverged'
    NO_UPSTREAM = 'no_upstream'
    GONE = 'gone'
    DETACHED = 'detached'


# What each state means, for `syncer policy rules`, which groups its table by state. Here rather
# than in the CLI because the taxonomy is this module's: a state added to the enum without a line
# here fails a test, which is the only thing that keeps the two in step.
STATE_DOCS: dict[PrimaryState, str] = {
    PrimaryState.SYNCED: 'the branch and its upstream point at the same commit',
    PrimaryState.AHEAD: 'the branch has commits the upstream does not',
    PrimaryState.BEHIND: 'the upstream has commits the branch does not',
    PrimaryState.DIVERGED: 'both sides have commits the other does not',
    PrimaryState.NO_UPSTREAM: 'the branch tracks nothing — it has never been pushed',
    PrimaryState.GONE: 'the upstream existed and no longer does, after fetch --prune',
    PrimaryState.DETACHED: 'HEAD is not on a branch',
}


class Action(StrEnum):
    """The safe menu. Every action here is pre-vetted safe; a policy can never opt into
    an unsafe primitive (force-push, dirty-tree mutation, history rewrite).

    FAST_FORWARD is the intent 'advance this branch to its upstream'; PULL_FF and FF_REF
    are the two mechanisms that implement it, and each refuses in the checkout state the
    other one handles. Rules should name the intent — a rule naming a mechanism is refused
    whenever the branch happens to be on the wrong side of that split.
    """

    SKIP = 'skip'
    REPORT = 'report'
    FAST_FORWARD = 'fast_forward'
    PULL_FF = 'pull_ff'
    FF_REF = 'ff_ref'
    PUSH = 'push'
    REBASE_PUSH = 'rebase_push'
    SET_UPSTREAM_PUSH = 'set_upstream_push'
    DELETE_LOCAL = 'delete_local'
    PROMPT = 'prompt'


class Scope(StrEnum):
    """Which branches a policy evaluates."""

    DEFAULT = 'default'
    CURRENT = 'current'
    TRACKED = 'tracked'
    ALL = 'all'


VALID_STATES = {state.value for state in PrimaryState}
VALID_ACTIONS = {action.value for action in Action}

# What a protected branch still permits. An allowlist, not a denylist, so a new Action defaults
# to refused on a protected branch — the safe direction. Everything here either does nothing or
# advances the branch to what its upstream already contains: it publishes nothing and destroys
# nothing, which is the whole test for whether protection should care.
PROTECTED_ALLOWED = frozenset(
    {
        Action.SKIP,
        Action.REPORT,
        Action.PROMPT,
        Action.FAST_FORWARD,
        Action.PULL_FF,
        Action.FF_REF,
    }
)

# The mechanism half of the fast-forward split, as opposed to the FAST_FORWARD intent that
# dispatches between them. Each is refused whenever the branch sits on the other side of the
# checkout split, so nothing may *suggest* one — TestBuiltinsNameIntentNotMechanism asserts the
# built-ins never decide one, and this is the same rule for anything that recommends an action.
MECHANISM_ACTIONS = frozenset({Action.PULL_FF, Action.FF_REF})

# Selector words that are roles, not literal branch names or globs.
ROLE_SELECTORS = {'default', 'current'}
GLOB_CHARS = set('*?[]')


class BranchState(BaseModel):
    """The classified state of a single branch. Produced by classify(), consumed by decide()."""

    branch: str
    primary: PrimaryState
    ahead: int = 0
    behind: int = 0
    is_current: bool = False
    is_default: bool = False
    dirty: bool = False
    stashed: bool = False
    upstream: str | None = None
    merged_into_target: bool = False
    # Path of a *linked* worktree holding this branch, when one does. `is_current` covers only
    # this checkout's HEAD, so without this a branch live in a worktree is indistinguishable from
    # one nothing has checked out — the distinction invariant 9 turns on. An execute-time gate
    # and a hint target, never a decision input: decide() is invariant to it.
    worktree: str | None = None


class Policy(BaseModel):
    """A machine-local, user-named policy: a branch scope + a rule table + fetch options.

    Rule keys are '<selector>:<state>'. Unknown action names or states fail validation
    loudly here rather than silently no-op'ing at decide time.
    """

    name: str
    scope: Scope = Scope.TRACKED
    prune: bool = True
    rules: dict[str, str] = {}
    fallback: Action = Action.REPORT
    # Branch that a gone branch must be integrated into before delete_local will touch it.
    # None means the repo's default branch. Set it where the trunk you merge into is not the
    # default — a develop-centric flow never makes a feature branch an ancestor of main until
    # the release promotes it, so the default-branch guard refuses every merged branch forever.
    merge_target: str | None = None
    # Branch name patterns (fnmatch, the same grammar rule selectors use) that no action may
    # publish to or destroy. Enforced centrally in execute(), so protection is a hard guard
    # rather than a function of getting an exact-name rule right for every branch — forget one
    # under a fallback like '*:ahead = push' and a stray local commit reaches a shared branch.
    protected: list[str] = []
    # Branch name patterns (fnmatch) to report on even with no local copy. Opt-in and empty by
    # default: every repo has remote branches you will never care about, and a check that fires
    # on all of them forever is one that gets ignored. Purely informational — there is no local
    # branch to act on, so this never reaches decide() or the action menu.
    watch_remote: list[str] = []

    @field_validator('rules')
    @classmethod
    def validate_rules(cls, rules: dict[str, str]) -> dict[str, str]:
        for key, action in rules.items():
            selector, sep, state = key.partition(':')
            if not sep:
                raise ValueError(f"rule key {key!r} must be '<selector>:<state>'")
            if not selector:
                raise ValueError(f'rule key {key!r} has an empty selector')
            if state not in VALID_STATES:
                raise ValueError(f'rule key {key!r} has unknown state {state!r}; valid: {sorted(VALID_STATES)}')
            if action not in VALID_ACTIONS:
                raise ValueError(f'rule {key!r} maps to unknown action {action!r}; valid: {sorted(VALID_ACTIONS)}')
        return rules


def is_watched_remote(branch: str, policy: Policy) -> bool:
    """Whether a remote-only branch matches the policy's watch_remote patterns."""
    return any(fnmatch.fnmatch(branch, pattern) for pattern in policy.watch_remote)


def matching_protection(branch: str, policy: Policy) -> str | None:
    """The first protected pattern matching `branch`, or None if it is unprotected.

    Returns the pattern rather than a bool so a refusal can name the config entry responsible:
    `protected by 'release/*'` tells you what to edit, `protected` does not.
    """
    return next((pattern for pattern in policy.protected if fnmatch.fnmatch(branch, pattern)), None)


def _matching_globs(branch: str, policy: Policy) -> list[str]:
    """Glob selectors in the policy that match `branch`, most-specific first.

    Specificity tie-break is (longer pattern, then alphabetical) so precedence is
    deterministic when several globs match — no ordered-rule ambiguity.
    """
    matches = set()
    for key in policy.rules:
        selector = key.partition(':')[0]
        if selector == '*' or selector in ROLE_SELECTORS or selector == branch:
            continue
        if GLOB_CHARS & set(selector) and fnmatch.fnmatch(branch, selector):
            matches.add(selector)
    return sorted(matches, key=lambda selector: (-len(selector), selector))


def _selector_precedence(state: BranchState, policy: Policy):
    """Yield the selectors that apply to `state`, highest precedence first:

    exact-name  >  glob  >  role(default > current)  >  "*"
    """
    yield state.branch
    yield from _matching_globs(state.branch, policy)
    if state.is_default:
        yield 'default'
    if state.is_current:
        yield 'current'
    yield '*'


def deciding_selector(state: BranchState, policy: Policy) -> str | None:
    """Which selector's rule decides `state`, or None when nothing matches and the fallback applies.

    Split out of decide() rather than reimplemented beside it: `policy rules` reports where each
    decision came from, and a second copy of the precedence walk is a second thing to keep in step
    with the first.
    """
    for selector in _selector_precedence(state, policy):
        if f'{selector}:{state.primary.value}' in policy.rules:
            return selector
    return None


def decide(state: BranchState, policy: Policy) -> Action:
    """Pure: map a classified branch state to an action under the given policy.

    First selector (in precedence order) that has a rule for this primary state wins;
    if none match, the policy fallback applies. Note this depends only on the primary
    state plus the branch's role/name — the dirty/stashed modifiers are execute-time
    gates, never decision inputs, so decide() is invariant to them.
    """
    selector = deciding_selector(state, policy)
    if selector is None:
        return policy.fallback
    return Action(policy.rules[f'{selector}:{state.primary.value}'])


# ---------- Built-in policies ---------- #
# These work with no [policies.*] block in config.toml. `standard` reproduces roughly
# today's default-branch behavior; `observe` is report-only; `mirror` is the opt-in,
# always-on-box policy that auto-does everything safe.

STANDARD_POLICY = Policy(
    name='standard',
    scope=Scope.TRACKED,
    prune=True,
    fallback=Action.REPORT,
    rules={
        'default:synced': 'skip',
        'default:ahead': 'push',
        'default:diverged': 'rebase_push',
        'default:gone': 'report',
        '*:behind': 'fast_forward',
        '*:ahead': 'report',
        '*:diverged': 'report',
        '*:no_upstream': 'report',
        '*:gone': 'report',
    },
)

OBSERVE_POLICY = Policy(
    name='observe',
    scope=Scope.ALL,
    prune=True,
    fallback=Action.REPORT,
    rules={},
)

MIRROR_POLICY = Policy(
    name='mirror',
    scope=Scope.ALL,
    prune=True,
    fallback=Action.REPORT,
    rules={
        '*:behind': 'fast_forward',
        '*:ahead': 'push',
        '*:diverged': 'rebase_push',
        '*:gone': 'delete_local',
    },
)

BUILTIN_POLICIES = {policy.name: policy for policy in (STANDARD_POLICY, OBSERVE_POLICY, MIRROR_POLICY)}
