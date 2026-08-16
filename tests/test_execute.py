import subprocess
from itertools import chain
from pathlib import Path

import pytest

from syncer.classify import classify_branch
from syncer.execute import ACTION_DOCS
from syncer.execute import MUTATING_ACTIONS
from syncer.execute import REFUSAL_TEXT
from syncer.execute import Outcome
from syncer.execute import Refusal
from syncer.execute import checkout_refusal
from syncer.execute import describe_refusal
from syncer.execute import dirty_refusal
from syncer.execute import execute
from syncer.execute import worktree_refusal
from syncer.policy import PROTECTED_ALLOWED
from syncer.policy import STATE_DOCS
from syncer.policy import Action
from syncer.policy import BranchState
from syncer.policy import Policy
from syncer.policy import PrimaryState
from syncer.repos import Repo

FORCE_FLAGS = ('--force', '-f', '--force-with-lease')

# Executor tests exercise actions directly, so the policy only supplies guard settings
# (merge_target); the default of None means 'the repo's default branch', as in the built-ins.
POLICY = Policy(name='test')


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(['git', *args], cwd=path, capture_output=True, text=True)


def _make_repo(path: Path) -> Repo:
    return Repo(name='test-repo', path=path, owner='user', host='https://github.com')


def _commit(path: Path, filename: str, message: str, content: str | None = None) -> None:
    (path / filename).write_text(content if content is not None else f'{filename}\n')
    _git(path, 'add', '.')
    _git(path, 'commit', '-m', message)


def _head(repo: Repo) -> str:
    return repo._git('rev-parse', 'HEAD').stdout.strip()


def _state_for(repo: Repo, branch: str) -> BranchState:
    return classify_branch(
        repo,
        branch,
        default=repo.default_branch,
        current=repo.current_branch,
        dirty_current=bool(repo.uncommitted_changes),
        stashed=repo.stash_count > 0,
    )


@pytest.fixture
def cloned_repo(tmp_path):
    """A working clone of a bare remote with one pushed commit on main."""
    bare = tmp_path / 'remote.git'
    subprocess.run(['git', 'init', '--bare', '-b', 'main', str(bare)], capture_output=True)
    repo_path = tmp_path / 'repo'
    subprocess.run(['git', 'clone', str(bare), str(repo_path)], capture_output=True)
    _git(repo_path, 'config', 'user.email', 'test@test.com')
    _git(repo_path, 'config', 'user.name', 'Test')
    (repo_path / 'README.md').write_text('# Test\n')
    _git(repo_path, 'add', '.')
    _git(repo_path, 'commit', '-m', 'init')
    _git(repo_path, 'push', '-u', 'origin', 'main')
    return repo_path


def _second_clone_pushes(tmp_path: Path, filename: str = 'remote.txt', content: str | None = None) -> None:
    bare = tmp_path / 'remote.git'
    second = tmp_path / 'second'
    if not second.exists():
        subprocess.run(['git', 'clone', str(bare), str(second)], capture_output=True)
        _git(second, 'config', 'user.email', 'test@test.com')
        _git(second, 'config', 'user.name', 'Test')
    _commit(second, filename, 'remote change', content)
    _git(second, 'push')


class GitSpy:
    """Record every git argv issued through a Repo, so invariant tests can assert no
    --force* ever reaches git."""

    def __init__(self, repo: Repo):
        self.calls: list[tuple[str, ...]] = []
        self._original = repo._git
        repo._git = self._record  # type: ignore[method-assign]

    def _record(self, *args: str, **kwargs):
        # **kwargs so the spy stays transparent to _git's keyword-only options (probe=), rather
        # than needing an edit every time one is added.
        self.calls.append(args)
        return self._original(*args, **kwargs)

    @property
    def flat_args(self) -> list[str]:
        return list(chain.from_iterable(self.calls))


# ---------- L3: effects (in-precondition) ---------- #


class TestExecuteEffects:
    def test_pull_ff_fast_forwards_current(self, cloned_repo, tmp_path):
        _second_clone_pushes(tmp_path)
        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        state = _state_for(repo, 'main')
        assert state.primary == PrimaryState.BEHIND
        outcome = execute(Action.PULL_FF, state, repo, POLICY)
        assert outcome.status == 'done'
        assert _state_for(repo, 'main').primary == PrimaryState.SYNCED

    def test_ff_ref_advances_noncurrent_branch(self, cloned_repo, tmp_path):
        # Create and push a feature branch, move it ahead on the remote, then leave main current.
        _git(cloned_repo, 'checkout', '-b', 'feature')
        _git(cloned_repo, 'push', '-u', 'origin', 'feature')
        bare = tmp_path / 'remote.git'
        second = tmp_path / 'second'
        subprocess.run(['git', 'clone', '-b', 'feature', str(bare), str(second)], capture_output=True)
        _git(second, 'config', 'user.email', 'test@test.com')
        _git(second, 'config', 'user.name', 'Test')
        _commit(second, 'f.txt', 'feature work')
        _git(second, 'push')
        _git(cloned_repo, 'checkout', 'main')
        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        state = _state_for(repo, 'feature')
        assert state.primary == PrimaryState.BEHIND
        assert state.is_current is False
        outcome = execute(Action.FF_REF, state, repo, POLICY)
        assert outcome.status == 'done'
        assert _state_for(repo, 'feature').primary == PrimaryState.SYNCED

    def test_push_publishes_ahead_commits(self, cloned_repo):
        _commit(cloned_repo, 'new.py', 'feat')
        repo = _make_repo(cloned_repo)
        state = _state_for(repo, 'main')
        assert state.primary == PrimaryState.AHEAD
        outcome = execute(Action.PUSH, state, repo, POLICY)
        assert outcome.status == 'done'
        # After a successful push the remote-tracking ref advances → back to synced.
        assert _state_for(repo, 'main').primary == PrimaryState.SYNCED

    def test_rebase_push_resolves_diverged_without_conflict(self, cloned_repo, tmp_path):
        _commit(cloned_repo, 'local.py', 'local work')  # local ahead on its own file
        _second_clone_pushes(tmp_path, 'remote.py')  # remote ahead on a different file
        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        state = _state_for(repo, 'main')
        assert state.primary == PrimaryState.DIVERGED
        outcome = execute(Action.REBASE_PUSH, state, repo, POLICY)
        assert outcome.status == 'done'
        assert _state_for(repo, 'main').primary == PrimaryState.SYNCED

    def test_set_upstream_push_sets_tracking(self, cloned_repo):
        _git(cloned_repo, 'checkout', '-b', 'feature/new')
        _commit(cloned_repo, 'x.py', 'wip')
        repo = _make_repo(cloned_repo)
        state = _state_for(repo, 'feature/new')
        assert state.primary == PrimaryState.NO_UPSTREAM
        outcome = execute(Action.SET_UPSTREAM_PUSH, state, repo, POLICY)
        assert outcome.status == 'done'
        assert repo.branch_upstream('feature/new')[0] == 'origin/feature/new'

    def test_delete_local_removes_merged_gone_branch(self, cloned_repo):
        _git(cloned_repo, 'checkout', '-b', 'feature/merged')
        _git(cloned_repo, 'push', '-u', 'origin', 'feature/merged')
        _git(cloned_repo, 'push', 'origin', '--delete', 'feature/merged')
        _git(cloned_repo, 'checkout', 'main')
        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        state = _state_for(repo, 'feature/merged')
        assert state.primary == PrimaryState.GONE
        assert state.merged_into_target is True
        outcome = execute(Action.DELETE_LOCAL, state, repo, POLICY)
        assert outcome.status == 'done'
        assert 'feature/merged' not in repo.local_branches()


class TestFastForwardDispatchesOnCheckoutState:
    """fast_forward is the intent behind pull_ff and ff_ref; it must succeed in both
    checkout states, which is precisely where naming either mechanism directly fails."""

    def test_advances_the_current_branch(self, cloned_repo, tmp_path):
        _second_clone_pushes(tmp_path)
        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        state = _state_for(repo, 'main')
        assert state.is_current is True
        assert state.primary == PrimaryState.BEHIND
        outcome = execute(Action.FAST_FORWARD, state, repo, POLICY)
        assert outcome.status == 'done'
        assert outcome.action == Action.FAST_FORWARD
        assert _state_for(repo, 'main').primary == PrimaryState.SYNCED

    def test_advances_a_non_current_branch(self, cloned_repo, tmp_path):
        _git(cloned_repo, 'checkout', '-b', 'feature')
        _git(cloned_repo, 'push', '-u', 'origin', 'feature')
        bare = tmp_path / 'remote.git'
        second = tmp_path / 'second'
        subprocess.run(['git', 'clone', '-b', 'feature', str(bare), str(second)], capture_output=True)
        _git(second, 'config', 'user.email', 'test@test.com')
        _git(second, 'config', 'user.name', 'Test')
        _commit(second, 'f.txt', 'feature work')
        _git(second, 'push')
        _git(cloned_repo, 'checkout', 'main')
        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        state = _state_for(repo, 'feature')
        assert state.is_current is False
        assert state.primary == PrimaryState.BEHIND
        outcome = execute(Action.FAST_FORWARD, state, repo, POLICY)
        assert outcome.status == 'done'
        assert outcome.action == Action.FAST_FORWARD
        assert _state_for(repo, 'feature').primary == PrimaryState.SYNCED

    def test_refuses_a_dirty_current_branch(self, cloned_repo, tmp_path):
        _second_clone_pushes(tmp_path)
        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        (cloned_repo / 'README.md').write_text('# dirty edit\n')
        before = _head(repo)
        outcome = execute(Action.FAST_FORWARD, _state_for(repo, 'main'), repo, POLICY)
        assert outcome.status == 'refused'
        assert outcome.reason is Refusal.DIRTY_TREE
        assert _head(repo) == before

    def test_refuses_when_not_strictly_behind(self, cloned_repo, tmp_path):
        _commit(cloned_repo, 'local.py', 'local')
        _second_clone_pushes(tmp_path, 'remote.py')
        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        before = _head(repo)
        outcome = execute(Action.FAST_FORWARD, _state_for(repo, 'main'), repo, POLICY)
        assert outcome.status == 'refused'
        assert _head(repo) == before


class TestDeleteLocalIntegrationProof:
    """A develop-centric flow never makes a feature branch an ancestor of the default branch,
    so the guard has to check the branch the work is actually integrated into — and has to
    accept the squash merges that Bitbucket and GitHub perform by default."""

    def _develop_flow(self, cloned_repo) -> None:
        _git(cloned_repo, 'checkout', '-b', 'develop')
        _git(cloned_repo, 'push', '-u', 'origin', 'develop')
        _git(cloned_repo, 'checkout', '-b', 'feature/x')
        _commit(cloned_repo, 'feature.py', 'feat')
        _git(cloned_repo, 'push', '-u', 'origin', 'feature/x')
        _git(cloned_repo, 'checkout', 'develop')

    def test_merged_into_target_but_not_into_default(self, cloned_repo):
        self._develop_flow(cloned_repo)
        _git(cloned_repo, 'merge', '--no-ff', '-m', 'merge feature/x', 'feature/x')
        _git(cloned_repo, 'push')
        _git(cloned_repo, 'push', 'origin', '--delete', 'feature/x')
        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        state = _state_for(repo, 'feature/x')
        assert state.primary == PrimaryState.GONE
        # Default target is the repo default (main), which the branch never reaches in this flow.
        assert execute(Action.DELETE_LOCAL, state, repo, POLICY).status == 'refused'
        assert 'feature/x' in repo.local_branches()
        outcome = execute(Action.DELETE_LOCAL, state, repo, Policy(name='work', merge_target='develop'))
        assert outcome.status == 'done'
        assert 'feature/x' not in repo.local_branches()

    def test_squash_merged_into_target(self, cloned_repo):
        self._develop_flow(cloned_repo)
        _git(cloned_repo, 'merge', '--squash', 'feature/x')
        _git(cloned_repo, 'commit', '-m', 'squash feature/x')
        _git(cloned_repo, 'push')
        _git(cloned_repo, 'push', 'origin', '--delete', 'feature/x')
        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        assert repo.is_merged_into('feature/x', 'develop') is False  # ancestry cannot see a squash
        state = _state_for(repo, 'feature/x')
        outcome = execute(Action.DELETE_LOCAL, state, repo, Policy(name='work', merge_target='develop'))
        assert outcome.status == 'done'
        assert 'feature/x' not in repo.local_branches()

    def test_refused_when_absent_from_the_target(self, cloned_repo):
        self._develop_flow(cloned_repo)
        _git(cloned_repo, 'push', 'origin', '--delete', 'feature/x')  # deleted without merging
        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        state = _state_for(repo, 'feature/x')
        outcome = execute(Action.DELETE_LOCAL, state, repo, Policy(name='work', merge_target='develop'))
        assert outcome.status == 'refused'
        assert outcome.reason is Refusal.NOT_INTEGRATED and 'develop' in outcome.message
        assert 'feature/x' in repo.local_branches()

    def test_refused_for_the_merge_target_itself(self, cloned_repo):
        """contains_branch(develop, develop) is trivially true, so without an explicit guard a
        deleted origin/develop would take the local trunk with it."""
        self._develop_flow(cloned_repo)
        _git(cloned_repo, 'checkout', 'main')
        _git(cloned_repo, 'push', 'origin', '--delete', 'develop')
        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        state = _state_for(repo, 'develop')
        assert state.primary == PrimaryState.GONE
        outcome = execute(Action.DELETE_LOCAL, state, repo, Policy(name='work', merge_target='develop'))
        assert outcome.status == 'refused'
        assert outcome.reason is Refusal.DELETE_MERGE_TARGET
        assert 'develop' in repo.local_branches()


# ---------- L3: refusals (out-of-precondition → no mutation) ---------- #


class TestExecuteRefusals:
    def test_pull_ff_refused_when_dirty(self, cloned_repo, tmp_path):
        _second_clone_pushes(tmp_path)
        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        (cloned_repo / 'README.md').write_text('# dirty edit\n')
        before = _head(repo)
        state = _state_for(repo, 'main')
        outcome = execute(Action.PULL_FF, state, repo, POLICY)
        assert outcome.status == 'refused'
        assert outcome.reason is Refusal.DIRTY_TREE
        assert _head(repo) == before  # invariant 2: tree/HEAD untouched

    def test_pull_ff_refused_when_diverged(self, cloned_repo, tmp_path):
        _commit(cloned_repo, 'local.py', 'local')
        _second_clone_pushes(tmp_path, 'remote.py')
        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        before = _head(repo)
        state = _state_for(repo, 'main')
        outcome = execute(Action.PULL_FF, state, repo, POLICY)
        assert outcome.status == 'refused'  # invariant 3: not strictly behind
        assert _head(repo) == before

    def test_ff_ref_refused_on_current_branch(self, cloned_repo, tmp_path):
        _second_clone_pushes(tmp_path)
        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        state = _state_for(repo, 'main')  # main is current and behind
        outcome = execute(Action.FF_REF, state, repo, POLICY)
        assert outcome.status == 'refused'

    def test_push_refused_when_diverged(self, cloned_repo, tmp_path):
        _commit(cloned_repo, 'local.py', 'local')
        _second_clone_pushes(tmp_path, 'remote.py')
        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        state = _state_for(repo, 'main')
        outcome = execute(Action.PUSH, state, repo, POLICY)
        assert outcome.status == 'refused'

    def test_push_refused_when_dirty(self, cloned_repo):
        _commit(cloned_repo, 'new.py', 'feat')
        (cloned_repo / 'dirty.txt').write_text('uncommitted\n')
        _git(cloned_repo, 'add', 'dirty.txt')
        repo = _make_repo(cloned_repo)
        state = _state_for(repo, 'main')
        outcome = execute(Action.PUSH, state, repo, POLICY)
        assert outcome.status == 'refused'
        assert outcome.reason is Refusal.DIRTY_TREE

    def test_delete_local_refused_when_not_merged(self, cloned_repo):
        _git(cloned_repo, 'checkout', '-b', 'feature/unmerged')
        _commit(cloned_repo, 'extra.py', 'extra')  # diverges from main
        _git(cloned_repo, 'push', '-u', 'origin', 'feature/unmerged')
        _git(cloned_repo, 'push', 'origin', '--delete', 'feature/unmerged')
        _git(cloned_repo, 'checkout', 'main')
        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        state = _state_for(repo, 'feature/unmerged')
        assert state.primary == PrimaryState.GONE
        outcome = execute(Action.DELETE_LOCAL, state, repo, POLICY)
        assert outcome.status == 'refused'
        assert 'feature/unmerged' in repo.local_branches()  # not deleted

    def test_delete_local_refused_on_default_branch(self, cloned_repo):
        repo = _make_repo(cloned_repo)
        # Hand-build a GONE state for main to prove the ¬default guard holds.
        state = BranchState(branch='main', primary=PrimaryState.GONE, is_default=True, merged_into_target=True)
        outcome = execute(Action.DELETE_LOCAL, state, repo, POLICY)
        assert outcome.status == 'refused'
        assert 'main' in repo.local_branches()


# ---------- L3/L4: hard-invariant enforcement ---------- #


class TestInvariants:
    def test_rebase_push_conflict_aborts_clean(self, cloned_repo, tmp_path):
        # Local and remote edit the SAME file differently → rebase must conflict.
        _commit(cloned_repo, 'README.md', 'local edit', content='local\n')
        _second_clone_pushes(tmp_path, 'README.md', content='remote\n')
        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        state = _state_for(repo, 'main')
        assert state.primary == PrimaryState.DIVERGED
        outcome = execute(Action.REBASE_PUSH, state, repo, POLICY)
        assert outcome.status == 'refused'
        assert outcome.reason is Refusal.REBASE_CONFLICT
        # invariant 4: no half-rebase, tree left clean
        assert repo.uncommitted_changes == []
        assert not (cloned_repo / '.git' / 'rebase-merge').exists()
        assert not (cloned_repo / '.git' / 'rebase-apply').exists()

    def test_no_force_flag_ever_issued(self, cloned_repo, tmp_path):
        """Sweep every action against a battery of real states; assert git never sees --force*."""
        # Build a diverged main and a gone feature branch in one repo.
        _git(cloned_repo, 'checkout', '-b', 'feature/gone')
        _git(cloned_repo, 'push', '-u', 'origin', 'feature/gone')
        _git(cloned_repo, 'push', 'origin', '--delete', 'feature/gone')
        _git(cloned_repo, 'checkout', 'main')
        _commit(cloned_repo, 'README.md', 'local', content='local\n')
        _second_clone_pushes(tmp_path, 'README.md', content='remote\n')
        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        spy = GitSpy(repo)

        for branch in ('main', 'feature/gone'):
            state = _state_for(repo, branch)
            for action in Action:
                execute(action, state, repo, POLICY)

        for flag in FORCE_FLAGS:
            assert flag not in spy.flat_args, f'{flag} was issued to git'
        assert not any(arg.startswith('--force') for arg in spy.flat_args)

    def test_dirty_tree_never_mutated_across_actions(self, cloned_repo, tmp_path):
        """With a dirty current tree, no mutating action may change HEAD or the tree."""
        _second_clone_pushes(tmp_path)
        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        (cloned_repo / 'README.md').write_text('# dirty\n')
        before_head = _head(repo)
        before_dirty = repo.uncommitted_changes
        state = _state_for(repo, 'main')
        for action in (Action.FAST_FORWARD, Action.PULL_FF, Action.REBASE_PUSH, Action.PUSH):
            outcome = execute(action, state, repo, POLICY)
            assert outcome.status == 'refused'
        assert _head(repo) == before_head
        assert repo.uncommitted_changes == before_dirty


# ---------- non-mutating actions ---------- #


class TestNonMutatingActions:
    def test_skip(self, cloned_repo):
        repo = _make_repo(cloned_repo)
        outcome = execute(Action.SKIP, _state_for(repo, 'main'), repo, POLICY)
        assert outcome.status == 'skipped'

    def test_report(self, cloned_repo):
        repo = _make_repo(cloned_repo)
        outcome = execute(Action.REPORT, _state_for(repo, 'main'), repo, POLICY)
        assert outcome.status == 'reported'

    def test_prompt_degrades_to_report(self, cloned_repo):
        repo = _make_repo(cloned_repo)
        outcome = execute(Action.PROMPT, _state_for(repo, 'main'), repo, POLICY)
        assert outcome.status == 'reported'
        assert isinstance(outcome, Outcome)


class TestDirtyRefusalMatchesExecute:
    """dirty_refusal() mirrors invariant 2 for the reporter, so a report-only run does not
    promise a push that a dirty tree is going to make apply decline.

    A mirror can drift from what it mirrors, and this is the drift that matters: the report is
    only worth reading if the arrow it prints is what the next command does. Both directions are
    asserted here rather than trusting the two to be edited together.
    """

    def _dirty(self, repo_path: Path) -> None:
        (repo_path / 'README.md').write_text('# Test\nuncommitted\n')

    def test_no_action_refuses_for_dirt_the_reporter_would_not_predict(self, cloned_repo):
        repo = _make_repo(cloned_repo)
        self._dirty(cloned_repo)
        state = _state_for(repo, 'main')
        for action in MUTATING_ACTIONS:
            outcome = execute(action, state, repo, POLICY)
            if outcome.reason is Refusal.DIRTY_TREE:
                assert dirty_refusal(action, state, dirty=True) is not None, (
                    f'{action.value} refuses on a dirty tree but the report shows it unblocked'
                )

    def test_ff_ref_really_is_indifferent_to_the_tree(self, cloned_repo, tmp_path):
        """The one exemption in _WORKTREE_SAFE, proved rather than asserted: update-ref moves a
        ref that is not checked out, so it succeeds on a tree every other mutator refuses."""
        _git(cloned_repo, 'checkout', '-b', 'feature')
        _git(cloned_repo, 'push', '-u', 'origin', 'feature')
        _git(cloned_repo, 'checkout', 'main')
        second = tmp_path / 'second'
        subprocess.run(['git', 'clone', '-b', 'feature', str(tmp_path / 'remote.git'), str(second)], capture_output=True)
        _git(second, 'config', 'user.email', 'test@test.com')
        _git(second, 'config', 'user.name', 'Test')
        _commit(second, 'remote.txt', 'remote change')
        _git(second, 'push')

        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        self._dirty(cloned_repo)
        state = _state_for(repo, 'feature')
        assert state.primary == PrimaryState.BEHIND
        assert repo.is_dirty

        assert dirty_refusal(Action.FF_REF, state, dirty=True) is None
        assert dirty_refusal(Action.FAST_FORWARD, state, dirty=True) is None  # dispatches to ff_ref
        assert execute(Action.FAST_FORWARD, state, repo, POLICY).status == 'done'

    def test_delete_local_really_is_indifferent_to_the_tree(self, cloned_repo):
        """The second exemption in _TREE_INDEPENDENT, proved the same way. Uncommitted changes
        belong to a tree, never to a branch, so a dirty tree is no evidence about the ref being
        deleted — and refusing on it left every merged branch in a repo with work in progress
        uncollectable, which on a repo somebody works in daily is permanently."""
        _git(cloned_repo, 'checkout', '-b', 'merged-then-deleted')
        _git(cloned_repo, 'push', '-u', 'origin', 'merged-then-deleted')
        _git(cloned_repo, 'checkout', 'main')
        _git(cloned_repo, 'push', 'origin', '--delete', 'merged-then-deleted')

        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        self._dirty(cloned_repo)
        state = _state_for(repo, 'merged-then-deleted')
        assert state.primary == PrimaryState.GONE
        assert repo.is_dirty
        before_dirty = repo.uncommitted_changes

        assert dirty_refusal(Action.DELETE_LOCAL, state, dirty=True) is None
        assert execute(Action.DELETE_LOCAL, state, repo, POLICY).status == 'done'
        assert 'merged-then-deleted' not in repo.local_branches()
        assert repo.uncommitted_changes == before_dirty  # the work in the tree is untouched

    def test_a_clean_tree_blocks_nothing(self, cloned_repo):
        repo = _make_repo(cloned_repo)
        state = _state_for(repo, 'main')
        assert all(dirty_refusal(action, state, dirty=False) is None for action in Action)

    def test_non_mutating_actions_are_never_blocked_by_dirt(self, cloned_repo):
        """skip/report/prompt write nothing, so a dirty tree has no bearing on them — marking
        them 'would refuse' would put a refusal on the row of every dirty repo that is otherwise
        perfectly synced."""
        repo = _make_repo(cloned_repo)
        self._dirty(cloned_repo)
        state = _state_for(repo, 'main')
        for action in set(Action) - MUTATING_ACTIONS:
            assert dirty_refusal(action, state, dirty=True) is None, action


# ---------- linked worktrees (invariant 9) ---------- #


def _worktree_behind(cloned_repo: Path, tmp_path: Path, branch: str = 'wt-branch') -> tuple[Repo, Path]:
    """A branch checked out in a linked worktree, one commit behind its upstream.

    The exact shape the fast-forward path meets on a machine that uses worktrees at all: from the
    main checkout the branch is not current, which is the condition _ff_ref reads as 'no tree to
    disturb'.
    """
    _git(cloned_repo, 'push', 'origin', f'main:refs/heads/{branch}')
    _git(cloned_repo, 'fetch')
    _git(cloned_repo, 'branch', branch, f'origin/{branch}')
    worktree = tmp_path / 'linked'
    _git(cloned_repo, 'worktree', 'add', str(worktree), branch)

    second = tmp_path / 'second'
    subprocess.run(['git', 'clone', '-b', branch, str(tmp_path / 'remote.git'), str(second)], capture_output=True)
    _git(second, 'config', 'user.email', 'test@test.com')
    _git(second, 'config', 'user.name', 'Test')
    _commit(second, 'remote.txt', 'remote change')
    _git(second, 'push')

    repo = _make_repo(cloned_repo)
    repo.fetch_prune()
    return repo, worktree


def _worktree_gone(cloned_repo: Path, tmp_path: Path, branch: str = 'wt-gone') -> tuple[Repo, Path]:
    """A merged branch whose remote is deleted, still checked out in a linked worktree.

    The shape a worktree-per-branch workflow leaves behind on every merged PR: nothing is wrong
    with the branch, delete_local's whole guard passes, and the only thing standing in the way is
    the worktree still holding the ref.
    """
    _git(cloned_repo, 'push', 'origin', f'main:refs/heads/{branch}')
    _git(cloned_repo, 'fetch')
    _git(cloned_repo, 'branch', branch, f'origin/{branch}')
    worktree = tmp_path / 'linked-gone'
    _git(cloned_repo, 'worktree', 'add', str(worktree), branch)
    _git(cloned_repo, 'push', 'origin', '--delete', branch)

    repo = _make_repo(cloned_repo)
    repo.fetch_prune()
    return repo, worktree


class TestWorktreeCheckout:
    """Invariant 9. `is_current` is this checkout's HEAD alone, so a branch a linked worktree
    holds reads as 'not checked out' — and update-ref is the mechanism git does not protect at
    all, unlike `branch -f` and `branch -D` which refuse for themselves.

    Left unguarded this moved a live worktree's HEAD while its index stayed at the old commit,
    so files added by the new commit showed up staged for deletion in a tree syncer never
    measured the dirtiness of. Proved against real worktrees rather than a mocked ref, because
    the whole failure was a wrong belief about what git does here.
    """

    def test_ff_ref_refuses_and_the_worktree_is_untouched(self, cloned_repo, tmp_path):
        repo, worktree = _worktree_behind(cloned_repo, tmp_path)
        state = _state_for(repo, 'wt-branch')
        assert state.primary == PrimaryState.BEHIND
        before = _git(worktree, 'rev-parse', 'HEAD').stdout.strip()

        outcome = execute(Action.FF_REF, state, repo, POLICY)

        assert outcome.status == 'refused'
        assert outcome.reason is Refusal.WORKTREE_CHECKOUT
        assert _git(worktree, 'rev-parse', 'HEAD').stdout.strip() == before
        # The real damage was never the ref — it was the tree left describing a commit it no
        # longer pointed at, which a bare HEAD check would not have caught.
        assert _git(worktree, 'status', '--porcelain').stdout == ''

    def test_fast_forward_inherits_the_refusal(self, cloned_repo, tmp_path):
        """The intent dispatches to _ff_ref for a non-current branch, so it must not be a way
        round the guard the mechanism enforces."""
        repo, worktree = _worktree_behind(cloned_repo, tmp_path)
        state = _state_for(repo, 'wt-branch')
        before = _git(worktree, 'rev-parse', 'HEAD').stdout.strip()

        outcome = execute(Action.FAST_FORWARD, state, repo, POLICY)

        assert outcome.status == 'refused'
        assert outcome.reason is Refusal.WORKTREE_CHECKOUT
        assert _git(worktree, 'rev-parse', 'HEAD').stdout.strip() == before

    def test_the_report_predicts_the_refusal(self, cloned_repo, tmp_path):
        """The mirror, for the same reason dirty_refusal has one: `1 behind → fast_forward` on a
        branch apply will always refuse is an arrow nobody can act on."""
        repo, _ = _worktree_behind(cloned_repo, tmp_path)
        state = _state_for(repo, 'wt-branch')
        assert state.worktree is not None
        assert worktree_refusal(Action.FF_REF, state) is Refusal.WORKTREE_CHECKOUT
        assert worktree_refusal(Action.FAST_FORWARD, state) is Refusal.WORKTREE_CHECKOUT

    def test_an_ordinary_non_current_branch_still_fast_forwards(self, cloned_repo, tmp_path):
        """The guard must cost nothing where no worktree is involved — otherwise it would refuse
        every non-current branch and disable ff_ref outright."""
        _git(cloned_repo, 'checkout', '-b', 'feature')
        _git(cloned_repo, 'push', '-u', 'origin', 'feature')
        _git(cloned_repo, 'checkout', 'main')
        _second_clone_pushes(tmp_path)
        second = tmp_path / 'second'
        _git(second, 'checkout', '-b', 'feature', 'origin/feature')
        _commit(second, 'more.txt', 'on feature')
        _git(second, 'push', 'origin', 'feature')

        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        state = _state_for(repo, 'feature')
        assert state.worktree is None
        assert worktree_refusal(Action.FF_REF, state) is None
        assert execute(Action.FF_REF, state, repo, POLICY).status == 'done'

    def test_unreadable_worktree_list_refuses_rather_than_proceeds(self, cloned_repo, tmp_path):
        """Cannot-verify has to answer the way that refuses, the same polarity as is_dirty. The
        opposite reading is what made a failed `git status` mean permission to mutate."""
        repo, worktree = _worktree_behind(cloned_repo, tmp_path)
        state = _state_for(repo, 'wt-branch')
        before = _git(worktree, 'rev-parse', 'HEAD').stdout.strip()
        repo.__dict__['linked_worktree_branches'] = None  # as if `git worktree list` had failed

        outcome = execute(Action.FF_REF, state, repo, POLICY)

        assert outcome.reason is Refusal.WORKTREE_CHECKOUT
        assert _git(worktree, 'rev-parse', 'HEAD').stdout.strip() == before

    def test_git_refuses_the_destructive_paths_too(self, cloned_repo, tmp_path):
        """git's own refusal, established rather than assumed. It is why the ref never moves —
        and, on its own, not why syncer refuses: see the delete_local tests below."""
        repo, worktree = _worktree_behind(cloned_repo, tmp_path)
        assert _git(cloned_repo, 'branch', '-D', 'wt-branch').returncode != 0
        assert _git(cloned_repo, 'branch', '-f', 'wt-branch', 'origin/wt-branch').returncode != 0
        assert _git(worktree, 'rev-parse', '--abbrev-ref', 'HEAD').stdout.strip() == 'wt-branch'

    def test_delete_local_refuses_a_branch_a_worktree_holds(self, cloned_repo, tmp_path):
        """Inheriting git's refusal is not the same as making one. git returns it as a failure
        carrying prose, so the outcome was `failed` with no key on it — and the reporter, which
        runs no git at all, could not predict it, printing `gone → delete_local` on a row apply
        was always going to bounce."""
        repo, worktree = _worktree_gone(cloned_repo, tmp_path)
        state = _state_for(repo, 'wt-gone')
        assert state.primary == PrimaryState.GONE

        outcome = execute(Action.DELETE_LOCAL, state, repo, POLICY)

        assert outcome.status == 'refused'
        assert outcome.reason is Refusal.WORKTREE_CHECKOUT
        assert 'wt-gone' in repo.local_branches()
        assert _git(worktree, 'rev-parse', '--abbrev-ref', 'HEAD').stdout.strip() == 'wt-gone'

    def test_the_report_predicts_the_delete_refusal(self, cloned_repo, tmp_path):
        repo, _ = _worktree_gone(cloned_repo, tmp_path)
        state = _state_for(repo, 'wt-gone')
        assert worktree_refusal(Action.DELETE_LOCAL, state) is Refusal.WORKTREE_CHECKOUT

    def test_removing_the_worktree_is_all_that_stands_in_the_way(self, cloned_repo, tmp_path):
        """The rest of the guard passes, which is what makes the refusal worth a remedy rather
        than a full stop: the branch is merged and the remote is gone."""
        repo, worktree = _worktree_gone(cloned_repo, tmp_path)
        assert _git(cloned_repo, 'worktree', 'remove', str(worktree)).returncode == 0

        repo = _make_repo(cloned_repo)
        outcome = execute(Action.DELETE_LOCAL, _state_for(repo, 'wt-gone'), repo, POLICY)

        assert outcome.status == 'done'
        assert 'wt-gone' not in repo.local_branches()


class TestCheckoutRefusal:
    """The static current/non-current gate, mirrored for the report. Absent it, mirror's
    `*:diverged = rebase_push` printed a rebase arrow on every non-current diverged branch and
    refused all of them at execute time."""

    def test_rebase_push_is_predicted_refused_off_the_current_branch(self, cloned_repo):
        repo = _make_repo(cloned_repo)
        state = _state_for(repo, 'main').model_copy(update={'is_current': False})
        assert checkout_refusal(Action.REBASE_PUSH, state) is Refusal.NOT_CURRENT
        assert checkout_refusal(Action.PULL_FF, state) is Refusal.NOT_CURRENT

    def test_ff_ref_is_predicted_refused_on_it(self, cloned_repo):
        repo = _make_repo(cloned_repo)
        state = _state_for(repo, 'main')
        assert state.is_current
        assert checkout_refusal(Action.FF_REF, state) is Refusal.IS_CURRENT

    def test_fast_forward_is_never_gated_on_checkout(self, cloned_repo):
        """It dispatches between the mechanisms precisely so neither side of the split refuses
        it, which is why policies name the intent."""
        repo = _make_repo(cloned_repo)
        current = _state_for(repo, 'main')
        assert checkout_refusal(Action.FAST_FORWARD, current) is None
        assert checkout_refusal(Action.FAST_FORWARD, current.model_copy(update={'is_current': False})) is None

    def test_the_mirror_agrees_with_execute_for_every_action(self, cloned_repo):
        """Both directions, as the dirty mirror does: a gate that over-predicts puts a refusal on
        a row apply would have cleared."""
        repo = _make_repo(cloned_repo)
        for state in (_state_for(repo, 'main'), _state_for(repo, 'main').model_copy(update={'is_current': False})):
            for action in MUTATING_ACTIONS:
                predicted = checkout_refusal(action, state)
                if predicted is not None:
                    assert execute(action, state, repo, POLICY).reason is predicted, action


# ---------- protected branches (invariant 8) ---------- #


class TestProtectedBranches:
    """Protecting a shared branch used to rely on remembering an exact-name rule for it; forget
    one under a fallback like '*:ahead = push' and a stray local commit reaches it. This is the
    guard that makes it structural, checked centrally before any action dispatches."""

    def test_push_is_refused_and_the_remote_is_untouched(self, cloned_repo, tmp_path):
        _git(cloned_repo, 'checkout', '-b', 'develop')
        _git(cloned_repo, 'push', '-u', 'origin', 'develop')
        _commit(cloned_repo, 'local.txt', 'local work')
        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        state = _state_for(repo, 'develop')
        assert state.primary == PrimaryState.AHEAD
        remote_head_before = _git(tmp_path / 'remote.git', 'rev-parse', 'develop').stdout.strip()

        outcome = execute(Action.PUSH, state, repo, Policy(name='p', protected=['develop']))
        assert outcome.status == 'refused'
        assert outcome.reason is Refusal.PROTECTED and 'develop' in outcome.message
        assert _git(tmp_path / 'remote.git', 'rev-parse', 'develop').stdout.strip() == remote_head_before

    def test_fast_forward_still_runs(self, cloned_repo, tmp_path):
        """Protection stops publishing and destroying, not catching up. Advancing to what the
        upstream already contains does neither — and refusing it would make the setting useless
        for exactly the long-lived branches it exists to protect."""
        _second_clone_pushes(tmp_path)
        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        state = _state_for(repo, 'main')
        assert state.primary == PrimaryState.BEHIND

        outcome = execute(Action.FAST_FORWARD, state, repo, Policy(name='p', protected=['main']))
        assert outcome.status == 'done'
        assert _state_for(repo, 'main').primary == PrimaryState.SYNCED

    def test_delete_local_is_refused_on_a_protected_branch(self, cloned_repo, tmp_path):
        _git(cloned_repo, 'checkout', '-b', 'release/1.0')
        _git(cloned_repo, 'push', '-u', 'origin', 'release/1.0')
        _git(cloned_repo, 'push', 'origin', '--delete', 'release/1.0')
        _git(cloned_repo, 'checkout', 'main')
        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        state = _state_for(repo, 'release/1.0')
        assert state.primary == PrimaryState.GONE

        outcome = execute(Action.DELETE_LOCAL, state, repo, Policy(name='p', protected=['release/*']))
        assert outcome.status == 'refused'
        assert outcome.reason is Refusal.PROTECTED and 'release/*' in outcome.message
        assert 'release/1.0' in repo.local_branches()

    def test_glob_patterns_match_like_rule_selectors(self, cloned_repo):
        repo = _make_repo(cloned_repo)
        state = _state_for(repo, 'main')
        assert execute(Action.PUSH, state, repo, Policy(name='p', protected=['ma*'])).reason is Refusal.PROTECTED
        # A non-matching pattern leaves the action to its own preconditions, whatever they say.
        assert execute(Action.PUSH, state, repo, Policy(name='p', protected=['release/*'])).reason is not Refusal.PROTECTED

    def test_every_publishing_action_is_refused(self, cloned_repo, tmp_path):
        """Iterate Action rather than listing the mutators: PROTECTED_ALLOWED is an allowlist, so
        an action added later is refused by default and this test proves it stays that way."""
        _git(cloned_repo, 'checkout', '-b', 'develop')
        _commit(cloned_repo, 'local.txt', 'local work')
        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        spy = GitSpy(repo)
        state = _state_for(repo, 'develop')
        policy = Policy(name='p', protected=['develop'])
        head_before = _head(repo)

        for action in Action:
            outcome = execute(action, state, repo, policy)
            if action in PROTECTED_ALLOWED:
                assert outcome.reason is not Refusal.PROTECTED
            else:
                assert outcome.status == 'refused', action
                assert outcome.reason is Refusal.PROTECTED, action

        assert _head(repo) == head_before
        # A refusal is decided before dispatch, so no mutating git command is even attempted.
        assert not any(arg in ('push', 'branch', 'update-ref', 'rebase') for arg in spy.flat_args)

    def test_an_unprotected_policy_changes_nothing(self, cloned_repo, tmp_path):
        _second_clone_pushes(tmp_path)
        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        state = _state_for(repo, 'main')
        assert execute(Action.FAST_FORWARD, state, repo, POLICY).status == 'done'


class TestActionDocs:
    """ACTION_DOCS is user-facing reference material for a tool whose product is trust, so every
    claim in it is checked against the guards rather than maintained alongside them."""

    def test_every_action_is_documented(self):
        """A new Action fails here rather than rendering a blank record in `policy actions show`."""
        assert set(ACTION_DOCS) == set(Action)

    def test_every_state_is_documented(self):
        """Same, for the state meanings `policy rules` groups its table by."""
        assert set(STATE_DOCS) == set(PrimaryState)

    def test_only_mutators_declare_what_they_run(self):
        for action, doc in ACTION_DOCS.items():
            assert (doc.runs is not None) == (action in MUTATING_ACTIONS), action

    def _clone(self, tmp_path: Path, tag: str) -> Path:
        path = tmp_path / f'c-{tag}'
        subprocess.run(['git', 'clone', str(tmp_path / 'remote.git'), str(path)], capture_output=True)
        _git(path, 'config', 'user.email', 'test@test.com')
        _git(path, 'config', 'user.name', 'Test')
        return path

    def _advance_origin(self, path: Path, tag: str) -> str:
        """Push one commit to origin/main from this clone; return the sha main sat at before.

        Self-contained rather than using the shared second-clone helper: as soon as any case
        pushes main from a different clone, that helper is behind and its push is rejected — with
        output captured, so it fails silently and the next fixture is quietly the wrong state.
        """
        _git(path, 'fetch', 'origin')
        _git(path, 'checkout', '-B', 'main', 'origin/main')
        before = _git(path, 'rev-parse', 'HEAD').stdout.strip()
        _commit(path, f'adv-{tag}.txt', 'advance')
        _git(path, 'push', 'origin', 'main')
        return before

    def _case(self, state: PrimaryState, tmp_path: Path, tag: str) -> tuple[Repo, str]:
        """A repo genuinely in `state`, freshly built. The guards read live git, so a BranchState
        merely asserting a state would prove nothing — and a successful action mutates the repo,
        so every (state, action) pair needs its own."""
        path = self._clone(tmp_path, tag)
        if state is PrimaryState.NO_UPSTREAM:
            _git(path, 'checkout', '-b', 'orphan')
            _commit(path, 'orphan.txt', 'orphan')
            return _make_repo(path), 'orphan'
        if state is PrimaryState.GONE:
            _git(path, 'checkout', '-b', 'feature')
            _commit(path, 'feature.txt', 'feature')
            _git(path, 'push', '-u', 'origin', 'feature')
            _git(path, 'checkout', 'main')
            _git(path, 'merge', 'feature')
            _git(path, 'push', 'origin', 'main')
            _git(path, 'push', 'origin', '--delete', 'feature')
            _git(path, 'fetch', '--prune', 'origin')
            return _make_repo(path), 'feature'
        if state in (PrimaryState.BEHIND, PrimaryState.DIVERGED):
            before = self._advance_origin(path, tag)
            _git(path, 'checkout', '-B', 'main', before)
        if state in (PrimaryState.AHEAD, PrimaryState.DIVERGED):
            _commit(path, f'local-{tag}.txt', 'local')
        return _make_repo(path), 'main'

    def _behind_noncurrent(self, tmp_path: Path, tag: str) -> tuple[Repo, str]:
        """A behind branch that is *not* checked out — the only shape ff_ref can act on, and
        without it applies_to={behind} would look unreachable for it."""
        path = self._clone(tmp_path, tag)
        before = self._advance_origin(path, tag)
        _git(path, 'branch', 'stale', before)
        _git(path, 'branch', '--set-upstream-to=origin/main', 'stale')
        return _make_repo(path), 'stale'

    # Every state, plus the one checkout variant that changes which mechanism can act.
    CASES = (*(state for state in PrimaryState if state is not PrimaryState.DETACHED), 'behind-noncurrent')

    def _build(self, case, tmp_path: Path, tag: str) -> tuple[Repo, str, PrimaryState]:
        if case == 'behind-noncurrent':
            repo, branch = self._behind_noncurrent(tmp_path, tag)
            return repo, branch, PrimaryState.BEHIND
        repo, branch = self._case(case, tmp_path, tag)
        return repo, branch, case

    def test_applies_to_is_exactly_where_an_action_can_act(self, cloned_repo, tmp_path):
        """Both directions. Outside applies_to nothing ever mutates — the safety claim, and the
        reason the set can be declared rather than derived. Inside it, every state is genuinely
        reachable, so a doc widened to states the guard rejects fails here instead of sending
        someone to set a rule that would refuse forever."""
        reachable: dict[Action, set[PrimaryState]] = {action: set() for action in MUTATING_ACTIONS}
        for case in self.CASES:
            for action in MUTATING_ACTIONS:
                repo, branch, state = self._build(case, tmp_path, f'{case}-{action.value}')
                classified = _state_for(repo, branch)
                assert classified.primary == state, f'fixture {case} classified as {classified.primary}'
                outcome = execute(action, classified, repo, POLICY)
                if outcome.status != 'refused':
                    reachable[action].add(state)
                    assert state in ACTION_DOCS[action].applies_to, f'{action} acted on undocumented state {state}'
        for action in MUTATING_ACTIONS:
            assert reachable[action] == set(ACTION_DOCS[action].applies_to), (
                f'{action}: reachable {sorted(s.value for s in reachable[action])} '
                f'!= documented {sorted(s.value for s in ACTION_DOCS[action].applies_to)}'
            )

    def test_every_refusal_is_documented(self, cloned_repo, tmp_path):
        """Whatever a guard refuses on, `policy actions show` already lists it.

        Compared by Refusal key, never by message text: the wording lives once in REFUSAL_TEXT,
        so rewriting a sentence is not supposed to fail anything, and a test that pinned the
        prose would fail with nothing wrong.
        """
        for case in self.CASES:
            for action in MUTATING_ACTIONS:
                repo, branch, _ = self._build(case, tmp_path, f'r-{case}-{action.value}')
                outcome = execute(action, _state_for(repo, branch), repo, POLICY)
                if outcome.status != 'refused':
                    continue
                assert outcome.reason in ACTION_DOCS[action].refuses, f'{action} on {case}: undocumented refusal {outcome.reason}'

    def test_every_refusal_key_renders(self):
        """Every key has wording, and no template is missing a field it needs at render time."""
        assert set(REFUSAL_TEXT) == set(Refusal)
        # describe_refusal fills every template field from _DOC_FIELDS, so a template growing a
        # new placeholder without a stand-in raises KeyError here rather than in the CLI.
        for reason in Refusal:
            assert describe_refusal(reason)
