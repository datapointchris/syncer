import subprocess
from pathlib import Path

import pytest

from syncer.classify import classify_branch
from syncer.classify import classify_repo
from syncer.policy import BUILTIN_POLICIES
from syncer.policy import Policy
from syncer.policy import PrimaryState
from syncer.policy import Scope
from syncer.repos import Repo


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(['git', *args], cwd=path, capture_output=True, text=True)


def _make_repo(path: Path) -> Repo:
    return Repo(name='test-repo', path=path, owner='user', host='https://github.com')


@pytest.fixture
def cloned_repo(tmp_path):
    """A working clone of a bare remote with one pushed commit on the default branch."""
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


def _commit(path: Path, filename: str, message: str) -> None:
    (path / filename).write_text(f'{filename}\n')
    _git(path, 'add', '.')
    _git(path, 'commit', '-m', message)


def _second_clone_pushes(tmp_path: Path, filename: str = 'remote.txt') -> None:
    """Push a commit to the bare remote from an independent clone (moves origin ahead)."""
    bare = tmp_path / 'remote.git'
    second = tmp_path / 'second'
    subprocess.run(['git', 'clone', str(bare), str(second)], capture_output=True)
    _git(second, 'config', 'user.email', 'test@test.com')
    _git(second, 'config', 'user.name', 'Test')
    _commit(second, filename, 'remote change')
    _git(second, 'push')


def _classify_main(repo: Repo) -> object:
    return classify_branch(repo, 'main', default='main', current='main', dirty_current=False, stashed=False)


class TestClassifyBranchStates:
    def test_synced(self, cloned_repo):
        repo = _make_repo(cloned_repo)
        state = _classify_main(repo)
        assert state.primary == PrimaryState.SYNCED
        assert state.ahead == 0
        assert state.behind == 0
        assert state.upstream == 'origin/main'

    def test_ahead(self, cloned_repo):
        _commit(cloned_repo, 'feature.py', 'feat')
        repo = _make_repo(cloned_repo)
        state = _classify_main(repo)
        assert state.primary == PrimaryState.AHEAD
        assert state.ahead == 1
        assert state.behind == 0

    def test_behind(self, cloned_repo, tmp_path):
        _second_clone_pushes(tmp_path)
        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        state = _classify_main(repo)
        assert state.primary == PrimaryState.BEHIND
        assert state.ahead == 0
        assert state.behind == 1

    def test_diverged(self, cloned_repo, tmp_path):
        _commit(cloned_repo, 'local.py', 'local work')
        _second_clone_pushes(tmp_path)
        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        state = _classify_main(repo)
        assert state.primary == PrimaryState.DIVERGED
        assert state.ahead == 1
        assert state.behind == 1

    def test_no_upstream(self, cloned_repo):
        _git(cloned_repo, 'checkout', '-b', 'feature/local-only')
        _commit(cloned_repo, 'wip.py', 'wip')
        repo = _make_repo(cloned_repo)
        state = classify_branch(
            repo, 'feature/local-only', default='main', current='feature/local-only', dirty_current=False, stashed=False
        )
        assert state.primary == PrimaryState.NO_UPSTREAM
        assert state.upstream is None

    def test_gone(self, cloned_repo):
        # Push a branch (sets upstream), then delete it on the remote and prune.
        _git(cloned_repo, 'checkout', '-b', 'feature/gone')
        _commit(cloned_repo, 'gone.py', 'gone')
        _git(cloned_repo, 'push', '-u', 'origin', 'feature/gone')
        _git(cloned_repo, 'push', 'origin', '--delete', 'feature/gone')
        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        state = classify_branch(repo, 'feature/gone', default='main', current='feature/gone', dirty_current=False, stashed=False)
        assert state.primary == PrimaryState.GONE
        # feature/gone was based on main + one commit, not merged into main
        assert state.merged_into_target is False

    def test_gone_merged_into_target(self, cloned_repo):
        # A branch pointing exactly at main (no extra commits) is an ancestor of main.
        _git(cloned_repo, 'checkout', '-b', 'feature/merged')
        _git(cloned_repo, 'push', '-u', 'origin', 'feature/merged')
        _git(cloned_repo, 'push', 'origin', '--delete', 'feature/merged')
        _git(cloned_repo, 'checkout', 'main')
        repo = _make_repo(cloned_repo)
        repo.fetch_prune()
        state = classify_branch(repo, 'feature/merged', default='main', current='main', dirty_current=False, stashed=False)
        assert state.primary == PrimaryState.GONE
        assert state.merged_into_target is True

    def test_merge_target_overrides_the_default_branch(self, cloned_repo):
        """The classified state has to agree with what the delete_local guard will decide,
        so classify resolves integration against the policy's merge_target too."""
        _git(cloned_repo, 'checkout', '-b', 'develop')
        _git(cloned_repo, 'push', '-u', 'origin', 'develop')
        _git(cloned_repo, 'checkout', '-b', 'feature/x')
        _commit(cloned_repo, 'feature.py', 'feat')
        _git(cloned_repo, 'push', '-u', 'origin', 'feature/x')
        _git(cloned_repo, 'checkout', 'develop')
        _git(cloned_repo, 'merge', '--no-ff', '-m', 'merge feature/x', 'feature/x')
        _git(cloned_repo, 'push')
        _git(cloned_repo, 'push', 'origin', '--delete', 'feature/x')
        repo = _make_repo(cloned_repo)
        repo.fetch_prune()

        args = {'default': 'main', 'current': 'develop', 'dirty_current': False, 'stashed': False}
        assert classify_branch(repo, 'feature/x', **args).merged_into_target is False
        assert classify_branch(repo, 'feature/x', merge_target='develop', **args).merged_into_target is True


class TestClassifyModifiers:
    def test_dirty_only_on_current_branch(self, cloned_repo):
        (cloned_repo / 'README.md').write_text('# changed\n')
        repo = _make_repo(cloned_repo)
        # main is current → dirty flows through
        current = classify_branch(repo, 'main', default='main', current='main', dirty_current=True, stashed=False)
        assert current.dirty is True
        # if some other branch were current, main's ref isn't dirty
        noncurrent = classify_branch(repo, 'main', default='main', current='other', dirty_current=True, stashed=False)
        assert noncurrent.dirty is False

    def test_is_default_and_is_current_flags(self, cloned_repo):
        repo = _make_repo(cloned_repo)
        state = classify_branch(repo, 'main', default='main', current='main', dirty_current=False, stashed=False)
        assert state.is_default is True
        assert state.is_current is True


class TestClassifyRepoScope:
    def test_scope_default_only_returns_default(self, cloned_repo):
        _git(cloned_repo, 'checkout', '-b', 'feature/extra')
        _git(cloned_repo, 'checkout', 'main')
        repo = _make_repo(cloned_repo)
        policy = Policy(name='p', scope=Scope.DEFAULT, prune=True)
        states = classify_repo(repo, policy)
        assert [s.branch for s in states] == ['main']

    def test_scope_all_returns_every_local_branch(self, cloned_repo):
        _git(cloned_repo, 'checkout', '-b', 'feature/extra')
        _git(cloned_repo, 'checkout', 'main')
        repo = _make_repo(cloned_repo)
        policy = Policy(name='p', scope=Scope.ALL, prune=True)
        branches = {s.branch for s in classify_repo(repo, policy)}
        assert branches == {'main', 'feature/extra'}

    def test_scope_tracked_excludes_untracked_branch(self, cloned_repo):
        _git(cloned_repo, 'checkout', '-b', 'feature/local-only')
        _git(cloned_repo, 'checkout', 'main')
        repo = _make_repo(cloned_repo)
        policy = Policy(name='p', scope=Scope.TRACKED, prune=True)
        branches = {s.branch for s in classify_repo(repo, policy)}
        assert branches == {'main'}  # feature/local-only has no upstream

    def test_detached_head_emits_detached_state(self, cloned_repo):
        _commit(cloned_repo, 'second.py', 'second')
        head = _git(cloned_repo, 'rev-parse', 'HEAD').stdout.strip()
        _git(cloned_repo, 'checkout', head)
        repo = _make_repo(cloned_repo)
        assert repo.is_detached is True
        states = classify_repo(repo, BUILTIN_POLICIES['observe'])  # scope=all
        assert any(s.primary == PrimaryState.DETACHED for s in states)


class TestClassifyRepoRemediation:
    def test_stale_origin_head_repointed_after_rename(self, cloned_repo, tmp_path):
        """The original incident: origin/HEAD points at a renamed-away default. classify_repo
        runs set-head --auto so default resolves to the real remote default, not the stale ref."""
        # Point origin/HEAD at a branch that doesn't exist on the remote.
        _git(cloned_repo, 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/master')
        repo = _make_repo(cloned_repo)
        classify_repo(repo, BUILTIN_POLICIES['standard'])
        # After set-head --auto, default resolves to the real default (main), never 'master'.
        assert repo.default_branch == 'main'

    def test_original_master_incident_end_to_end(self, cloned_repo, tmp_path):
        """L5 regression — the 2026-07 incident that motivated this feature.

        A clone is left on a local `master` tracking `origin/master`; the remote's `master`
        was renamed away (deleted) so `origin/master` is stale, `origin/HEAD` still points at
        it, `origin/main` is ahead, and an untracked file is present. The old logic resolved
        the default to `master`, compared master↔origin/master (both frozen), and reported a
        false "synced". Assert classify_repo now (a) prunes the stale ref and repoints
        origin/HEAD, (b) resolves the real default, (c) surfaces the orphaned master as GONE.
        """
        # Local master tracking origin/master; origin/HEAD points at master (pre-rename default).
        _git(cloned_repo, 'checkout', '-b', 'master')
        _git(cloned_repo, 'push', '-u', 'origin', 'master')
        _git(cloned_repo, 'remote', 'set-head', 'origin', 'master')

        # A second clone advances origin/main and deletes master on the remote, so THIS clone's
        # origin/master stays stale until it prunes (deleting via this clone would clean it now).
        second = tmp_path / 'second'
        subprocess.run(['git', 'clone', '-b', 'main', str(tmp_path / 'remote.git'), str(second)], capture_output=True)
        _git(second, 'config', 'user.email', 'test@test.com')
        _git(second, 'config', 'user.name', 'Test')
        _commit(second, 'ahead.txt', 'ahead on main')
        _git(second, 'push')
        _git(second, 'push', 'origin', '--delete', 'master')

        # Clone left on master with an untracked file.
        (cloned_repo / 'uv.lock').write_text('lock\n')
        _git(cloned_repo, 'checkout', 'master')
        repo = _make_repo(cloned_repo)

        # Precondition: the stale ref and stale origin/HEAD are present before remediation.
        assert repo._git('rev-parse', '--verify', 'refs/remotes/origin/master').returncode == 0
        assert repo._git('symbolic-ref', 'refs/remotes/origin/HEAD').stdout.strip().endswith('origin/master')

        states = classify_repo(repo, BUILTIN_POLICIES['standard'])

        # (a) stale ref pruned
        assert repo._git('rev-parse', '--verify', 'refs/remotes/origin/master').returncode != 0
        # (b) real default resolved (never the stale master)
        assert repo.default_branch == 'main'
        # (c) orphaned master surfaced as GONE, and main is behind — not a false "synced"
        by_branch = {state.branch: state for state in states}
        assert by_branch['master'].primary == PrimaryState.GONE
        assert by_branch['main'].primary == PrimaryState.BEHIND
        assert by_branch['main'].is_default is True
