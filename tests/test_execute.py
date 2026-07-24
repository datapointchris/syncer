import subprocess
from itertools import chain
from pathlib import Path

import pytest

from syncer.classify import classify_branch
from syncer.execute import Outcome
from syncer.execute import execute
from syncer.policy import Action
from syncer.policy import BranchState
from syncer.policy import PrimaryState
from syncer.repos import Repo

FORCE_FLAGS = ('--force', '-f', '--force-with-lease')


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

    def _record(self, *args: str):
        self.calls.append(args)
        return self._original(*args)

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
        outcome = execute(Action.PULL_FF, state, repo)
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
        outcome = execute(Action.FF_REF, state, repo)
        assert outcome.status == 'done'
        assert _state_for(repo, 'feature').primary == PrimaryState.SYNCED

    def test_push_publishes_ahead_commits(self, cloned_repo):
        _commit(cloned_repo, 'new.py', 'feat')
        repo = _make_repo(cloned_repo)
        state = _state_for(repo, 'main')
        assert state.primary == PrimaryState.AHEAD
        outcome = execute(Action.PUSH, state, repo)
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
        outcome = execute(Action.REBASE_PUSH, state, repo)
        assert outcome.status == 'done'
        assert _state_for(repo, 'main').primary == PrimaryState.SYNCED

    def test_set_upstream_push_sets_tracking(self, cloned_repo):
        _git(cloned_repo, 'checkout', '-b', 'feature/new')
        _commit(cloned_repo, 'x.py', 'wip')
        repo = _make_repo(cloned_repo)
        state = _state_for(repo, 'feature/new')
        assert state.primary == PrimaryState.NO_UPSTREAM
        outcome = execute(Action.SET_UPSTREAM_PUSH, state, repo)
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
        assert state.merged_into_default is True
        outcome = execute(Action.DELETE_LOCAL, state, repo)
        assert outcome.status == 'done'
        assert 'feature/merged' not in repo.local_branches()


# ---------- L3: refusals (out-of-precondition → no mutation) ---------- #


class TestExecuteRefusals:
    def test_pull_ff_refused_when_dirty(self, cloned_repo, tmp_path):
        _second_clone_pushes(tmp_path)
        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        (cloned_repo / 'README.md').write_text('# dirty edit\n')
        before = _head(repo)
        state = _state_for(repo, 'main')
        outcome = execute(Action.PULL_FF, state, repo)
        assert outcome.status == 'refused'
        assert 'dirty' in outcome.message
        assert _head(repo) == before  # invariant 2: tree/HEAD untouched

    def test_pull_ff_refused_when_diverged(self, cloned_repo, tmp_path):
        _commit(cloned_repo, 'local.py', 'local')
        _second_clone_pushes(tmp_path, 'remote.py')
        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        before = _head(repo)
        state = _state_for(repo, 'main')
        outcome = execute(Action.PULL_FF, state, repo)
        assert outcome.status == 'refused'  # invariant 3: not strictly behind
        assert _head(repo) == before

    def test_ff_ref_refused_on_current_branch(self, cloned_repo, tmp_path):
        _second_clone_pushes(tmp_path)
        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        state = _state_for(repo, 'main')  # main is current and behind
        outcome = execute(Action.FF_REF, state, repo)
        assert outcome.status == 'refused'

    def test_push_refused_when_diverged(self, cloned_repo, tmp_path):
        _commit(cloned_repo, 'local.py', 'local')
        _second_clone_pushes(tmp_path, 'remote.py')
        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        state = _state_for(repo, 'main')
        outcome = execute(Action.PUSH, state, repo)
        assert outcome.status == 'refused'

    def test_push_refused_when_dirty(self, cloned_repo):
        _commit(cloned_repo, 'new.py', 'feat')
        (cloned_repo / 'dirty.txt').write_text('uncommitted\n')
        _git(cloned_repo, 'add', 'dirty.txt')
        repo = _make_repo(cloned_repo)
        state = _state_for(repo, 'main')
        outcome = execute(Action.PUSH, state, repo)
        assert outcome.status == 'refused'
        assert 'dirty' in outcome.message

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
        outcome = execute(Action.DELETE_LOCAL, state, repo)
        assert outcome.status == 'refused'
        assert 'feature/unmerged' in repo.local_branches()  # not deleted

    def test_delete_local_refused_on_default_branch(self, cloned_repo):
        repo = _make_repo(cloned_repo)
        # Hand-build a GONE state for main to prove the ¬default guard holds.
        state = BranchState(branch='main', primary=PrimaryState.GONE, is_default=True, merged_into_default=True)
        outcome = execute(Action.DELETE_LOCAL, state, repo)
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
        outcome = execute(Action.REBASE_PUSH, state, repo)
        assert outcome.status == 'refused'
        assert 'conflict' in outcome.message
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
                execute(action, state, repo)

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
        for action in (Action.PULL_FF, Action.REBASE_PUSH, Action.PUSH):
            outcome = execute(action, state, repo)
            assert outcome.status == 'refused'
        assert _head(repo) == before_head
        assert repo.uncommitted_changes == before_dirty


# ---------- non-mutating actions ---------- #


class TestNonMutatingActions:
    def test_skip(self, cloned_repo):
        repo = _make_repo(cloned_repo)
        outcome = execute(Action.SKIP, _state_for(repo, 'main'), repo)
        assert outcome.status == 'skipped'

    def test_report(self, cloned_repo):
        repo = _make_repo(cloned_repo)
        outcome = execute(Action.REPORT, _state_for(repo, 'main'), repo)
        assert outcome.status == 'reported'

    def test_prompt_degrades_to_report(self, cloned_repo):
        repo = _make_repo(cloned_repo)
        outcome = execute(Action.PROMPT, _state_for(repo, 'main'), repo)
        assert outcome.status == 'reported'
        assert isinstance(outcome, Outcome)
