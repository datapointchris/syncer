import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from syncer.repos import ALL_ICONS
from syncer.repos import ICON_ERR
from syncer.repos import ICON_OK
from syncer.repos import Repo
from syncer.repos import _display_width
from syncer.repos import _status_line
from syncer.repos import find_repo_in_search_paths


def _git(path: Path, *args: str) -> None:
    subprocess.run(['git', *args], cwd=path, capture_output=True, text=True)


@pytest.fixture
def git_repo(tmp_path):
    """Create a real git repo for testing."""
    repo_path = tmp_path / 'test-repo'
    repo_path.mkdir()
    subprocess.run(['git', 'init'], cwd=repo_path, capture_output=True)
    subprocess.run(['git', 'config', 'user.email', 'test@test.com'], cwd=repo_path, capture_output=True)
    subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=repo_path, capture_output=True)
    # Create initial commit so HEAD exists
    (repo_path / 'README.md').write_text('# Test\n')
    subprocess.run(['git', 'add', '.'], cwd=repo_path, capture_output=True)
    subprocess.run(['git', 'commit', '-m', 'init'], cwd=repo_path, capture_output=True)
    return repo_path


@pytest.fixture
def git_repo_with_remote(tmp_path):
    """Create a git repo cloned from a bare remote."""
    bare = tmp_path / 'remote' / 'test-repo.git'
    bare.mkdir(parents=True)
    subprocess.run(['git', 'init', '--bare', str(bare)], capture_output=True)

    repo_path = tmp_path / 'test-repo'
    subprocess.run(['git', 'clone', str(bare), str(repo_path)], capture_output=True)
    _git(repo_path, 'config', 'user.email', 'test@test.com')
    _git(repo_path, 'config', 'user.name', 'Test')
    (repo_path / 'README.md').write_text('# Test\n')
    _git(repo_path, 'add', '.')
    _git(repo_path, 'commit', '-m', 'init')
    _git(repo_path, 'push')
    return repo_path


@pytest.fixture
def master_repo_with_remote(tmp_path):
    """Create a git repo on master branch with a bare remote."""
    bare = tmp_path / 'remote' / 'test-repo.git'
    bare.mkdir(parents=True)
    subprocess.run(['git', 'init', '--bare', '--initial-branch=master', str(bare)], capture_output=True)

    repo_path = tmp_path / 'test-repo'
    subprocess.run(['git', 'clone', str(bare), str(repo_path)], capture_output=True)
    _git(repo_path, 'config', 'user.email', 'test@test.com')
    _git(repo_path, 'config', 'user.name', 'Test')
    (repo_path / 'README.md').write_text('# Test\n')
    _git(repo_path, 'add', '.')
    _git(repo_path, 'commit', '-m', 'init')
    _git(repo_path, 'push')
    return repo_path


def _make_repo(path: Path, **kwargs) -> Repo:
    return Repo(name='test-repo', path=path, owner='user', host='https://github.com', **kwargs)


class TestDisplayWidth:
    def test_plain_text(self):
        assert _display_width('hello') == 5

    def test_icon_counts_as_two(self):
        assert _display_width(ICON_OK) == 2

    def test_mixed_text_and_icon(self):
        text = f'{ICON_OK}  syncer '
        assert _display_width(text) == 2 + len('  syncer ')

    def test_all_icons_are_double_width(self):
        for icon in ALL_ICONS:
            assert _display_width(icon) == 2


class TestStatusLine:
    def test_without_branch(self):
        line = _status_line(ICON_OK, 'myrepo', 'synced', 'green')
        assert 'myrepo' in line
        assert 'synced' in line
        assert '[green]' in line
        assert '_' in line

    def test_with_branch(self):
        line = _status_line(ICON_OK, 'myrepo', 'synced', 'green', branch='main')
        assert 'myrepo' in line
        assert 'synced' in line
        assert '(main)' in line
        assert '[blue]' in line

    def test_padding_minimum_one(self):
        line = _status_line(ICON_OK, 'a' * 200, 'synced', 'green')
        assert '_' in line

    def test_no_branch_vs_branch_alignment(self):
        # Both lines should target the same LINE_WIDTH
        no_branch = _status_line(ICON_ERR, 'repo', 'no remote', 'red')
        with_branch = _status_line(ICON_OK, 'repo', 'synced', 'green', branch='main')
        # Both should contain padding
        assert '_' in no_branch
        assert '_' in with_branch


class TestRepo:
    def test_init(self):
        repo = Repo(name='myrepo', path=Path('/tmp/myrepo'), owner='user', host='https://github.com')
        assert repo.name == 'myrepo'
        assert repo.url == 'https://github.com/user/myrepo'

    def test_exists(self, tmp_path):
        repo = _make_repo(tmp_path)
        assert repo.exists is True

    def test_not_exists(self, tmp_path):
        repo = _make_repo(tmp_path / 'nope')
        assert repo.exists is False

    def test_is_git_repo(self, git_repo):
        repo = _make_repo(git_repo)
        assert repo.is_git_repo is True

    def test_is_not_git_repo(self, tmp_path):
        repo = _make_repo(tmp_path)
        assert repo.is_git_repo is False

    def test_has_no_remote(self, git_repo):
        repo = _make_repo(git_repo)
        assert repo.has_remote is False

    def test_has_remote(self, git_repo_with_remote):
        repo = _make_repo(git_repo_with_remote)
        assert repo.has_remote is True

    def test_current_branch(self, git_repo):
        repo = _make_repo(git_repo)
        branch = repo.current_branch
        assert branch in ('main', 'master')

    def test_uncommitted_changes_empty(self, git_repo):
        repo = _make_repo(git_repo)
        assert repo.uncommitted_changes == []

    def test_uncommitted_changes_detected(self, git_repo):
        (git_repo / 'newfile.txt').write_text('hello')
        repo = _make_repo(git_repo)
        assert len(repo.uncommitted_changes) > 0

    def test_stash_count_zero(self, git_repo):
        repo = _make_repo(git_repo)
        assert repo.stash_count == 0

    def test_stash_count_nonzero(self, git_repo):
        (git_repo / 'wip.txt').write_text('work\n')
        _git(git_repo, 'add', '.')
        _git(git_repo, 'stash', 'push', '-m', 'save')
        repo = _make_repo(git_repo)
        assert repo.stash_count == 1

    def test_default_branch_detection(self, git_repo):
        repo = _make_repo(git_repo)
        branch = repo.default_branch
        assert branch in ('main', 'master')

    def test_unpushed_commits_with_remote(self, git_repo_with_remote):
        (git_repo_with_remote / 'new.txt').write_text('new\n')
        _git(git_repo_with_remote, 'add', '.')
        _git(git_repo_with_remote, 'commit', '-m', 'new commit')
        repo = _make_repo(git_repo_with_remote)
        assert repo.unpushed_commits == 1

    def test_behind_remote(self, tmp_path):
        """Create a repo that is behind its remote."""
        bare = tmp_path / 'remote' / 'repo.git'
        bare.mkdir(parents=True)
        subprocess.run(['git', 'init', '--bare', str(bare)], capture_output=True)

        repo_path = tmp_path / 'repo'
        subprocess.run(['git', 'clone', str(bare), str(repo_path)], capture_output=True)
        _git(repo_path, 'config', 'user.email', 'test@test.com')
        _git(repo_path, 'config', 'user.name', 'Test')
        (repo_path / 'README.md').write_text('# Test\n')
        _git(repo_path, 'add', '.')
        _git(repo_path, 'commit', '-m', 'init')
        _git(repo_path, 'push')

        # Push from a second clone to get the repo behind
        second = tmp_path / 'second'
        subprocess.run(['git', 'clone', str(bare), str(second)], capture_output=True)
        _git(second, 'config', 'user.email', 'test@test.com')
        _git(second, 'config', 'user.name', 'Test')
        (second / 'update.txt').write_text('update\n')
        _git(second, 'add', '.')
        _git(second, 'commit', '-m', 'remote update')
        _git(second, 'push')

        _git(repo_path, 'fetch')
        repo = _make_repo(repo_path)
        assert repo.behind_remote == 1


class TestGitStats:
    def test_total_commits(self, git_repo):
        repo = _make_repo(git_repo)
        assert repo.total_commits == 1

    def test_total_commits_multiple(self, git_repo):
        (git_repo / 'file2.txt').write_text('content\n')
        _git(git_repo, 'add', '.')
        _git(git_repo, 'commit', '-m', 'second')
        repo = _make_repo(git_repo)
        assert repo.total_commits == 2

    def test_last_commit_date(self, git_repo):
        repo = _make_repo(git_repo)
        date = repo.last_commit_date
        assert date is not None
        # ISO 8601 format includes timezone offset
        assert 'T' in date

    def test_first_commit_date(self, git_repo):
        repo = _make_repo(git_repo)
        date = repo.first_commit_date
        assert date is not None
        assert 'T' in date

    def test_first_and_last_differ_with_multiple_commits(self, git_repo):
        import time

        time.sleep(1)  # ensure different timestamps
        (git_repo / 'file2.txt').write_text('content\n')
        _git(git_repo, 'add', '.')
        _git(git_repo, 'commit', '-m', 'second')
        repo = _make_repo(git_repo)
        assert repo.first_commit_date != repo.last_commit_date

    def test_stats_on_non_git_dir(self, tmp_path):
        repo = _make_repo(tmp_path)
        assert repo.total_commits == 0
        assert repo.last_commit_date is None
        assert repo.first_commit_date is None


class TestDefaultBranchStaleRef:
    def test_stale_origin_head_falls_back_to_local(self, git_repo_with_remote):
        """If origin/HEAD points to a deleted branch, fall back to local branch detection."""
        # Point origin/HEAD to a non-existent branch
        _git(git_repo_with_remote, 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/nonexistent')
        repo = _make_repo(git_repo_with_remote)
        branch = repo.default_branch
        # Should not return 'nonexistent', should fall back
        assert branch != 'nonexistent'
        assert branch in ('main', 'master')

    def test_valid_origin_head_is_trusted(self, git_repo_with_remote):
        """If origin/HEAD points to a valid branch, use it."""
        repo = _make_repo(git_repo_with_remote)
        branch = repo.default_branch
        assert branch in ('main', 'master')

    def test_no_remote_falls_back_to_local(self, git_repo):
        """Repo with no remote should still detect default branch from local refs."""
        repo = _make_repo(git_repo)
        assert repo.default_branch in ('main', 'master')


class TestSetOriginHead:
    def test_updates_ref(self, git_repo_with_remote):
        repo = _make_repo(git_repo_with_remote)
        current = repo.default_branch
        assert repo.set_origin_head(current) is True
        # Verify it's set correctly
        result = subprocess.run(
            ['git', 'symbolic-ref', 'refs/remotes/origin/HEAD'],
            cwd=git_repo_with_remote,
            capture_output=True,
            text=True,
        )
        assert current in result.stdout


class TestRenameMasterToMain:
    def test_full_rename(self, master_repo_with_remote):
        """Full rename from clean master state."""
        repo = _make_repo(master_repo_with_remote)
        assert repo.current_branch == 'master'

        with patch.object(repo, 'set_default_branch_on_github', return_value=True):
            ok, steps = repo.rename_master_to_main()

        assert ok is True
        assert 'renamed local' in steps
        assert repo.current_branch == 'main'

    def test_already_renamed_locally(self, master_repo_with_remote):
        """If local is already main but remote still has master, handle it."""
        repo = _make_repo(master_repo_with_remote)
        # Rename locally first
        _git(master_repo_with_remote, 'branch', '-m', 'master', 'main')
        assert repo.current_branch == 'main'

        with patch.object(repo, 'set_default_branch_on_github', return_value=True):
            ok, steps = repo.rename_master_to_main()

        assert ok is True
        assert 'renamed local' not in steps  # Was already renamed
        assert 'pushed main' in steps

    def test_fully_done_is_idempotent(self, master_repo_with_remote):
        """Running rename on an already-completed rename should succeed."""
        repo = _make_repo(master_repo_with_remote)

        # Do the full rename first
        with patch.object(repo, 'set_default_branch_on_github', return_value=True):
            ok1, _ = repo.rename_master_to_main()
        assert ok1 is True

        # Run again — should be idempotent
        with patch.object(repo, 'set_default_branch_on_github', return_value=True):
            ok2, steps = repo.rename_master_to_main()
        assert ok2 is True
        assert 'renamed local' not in steps
        assert 'pushed main' not in steps

    def test_no_master_or_main(self, git_repo_with_remote):
        """Repo with neither master nor main branch fails gracefully."""
        # Create a branch with a different name and delete the default
        current = subprocess.run(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            cwd=git_repo_with_remote,
            capture_output=True,
            text=True,
        ).stdout.strip()
        _git(git_repo_with_remote, 'checkout', '-b', 'develop')
        _git(git_repo_with_remote, 'branch', '-D', current)

        repo = _make_repo(git_repo_with_remote)
        with patch.object(repo, 'set_default_branch_on_github', return_value=True):
            ok, steps = repo.rename_master_to_main()
        assert ok is False
        assert 'no master or main branch found' in steps

    def test_github_default_failure_stops(self, master_repo_with_remote):
        """If setting GitHub default fails, stop and report."""
        repo = _make_repo(master_repo_with_remote)

        with patch.object(repo, 'set_default_branch_on_github', return_value=False):
            ok, steps = repo.rename_master_to_main()
        assert ok is False
        assert 'set GitHub default failed' in steps


class TestIsFork:
    def test_is_fork_true(self, git_repo):
        repo = _make_repo(git_repo)
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout='true\n', stderr='')
        with patch('subprocess.run', return_value=result):
            assert repo.is_fork is True

    def test_is_fork_false(self, git_repo):
        repo = _make_repo(git_repo)
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout='false\n', stderr='')
        with patch('subprocess.run', return_value=result):
            assert repo.is_fork is False

    def test_is_fork_gh_fails(self, git_repo):
        repo = _make_repo(git_repo)
        result = subprocess.CompletedProcess(args=[], returncode=1, stdout='', stderr='error')
        with patch('subprocess.run', return_value=result):
            assert repo.is_fork is False


class TestFindRepoInSearchPaths:
    def test_find_direct(self, tmp_path):
        repo_dir = tmp_path / 'code' / 'myrepo'
        repo_dir.mkdir(parents=True)
        (repo_dir / '.git').mkdir()

        result = find_repo_in_search_paths('myrepo', [tmp_path / 'code'])
        assert result == repo_dir

    def test_find_nested(self, tmp_path):
        repo_dir = tmp_path / 'code' / 'subdir' / 'myrepo'
        repo_dir.mkdir(parents=True)
        (repo_dir / '.git').mkdir()

        result = find_repo_in_search_paths('myrepo', [tmp_path / 'code'])
        assert result == repo_dir

    def test_not_found(self, tmp_path):
        search = tmp_path / 'code'
        search.mkdir()

        result = find_repo_in_search_paths('myrepo', [search])
        assert result is None

    def test_skip_nonexistent_search_path(self, tmp_path):
        result = find_repo_in_search_paths('myrepo', [tmp_path / 'nonexistent'])
        assert result is None

    def test_skip_claimed_path_direct(self, tmp_path):
        repo_dir = tmp_path / 'code' / 'myrepo'
        repo_dir.mkdir(parents=True)
        (repo_dir / '.git').mkdir()

        result = find_repo_in_search_paths('myrepo', [tmp_path / 'code'], claimed_paths={repo_dir})
        assert result is None

    def test_skip_claimed_path_nested(self, tmp_path):
        repo_dir = tmp_path / 'code' / 'subdir' / 'myrepo'
        repo_dir.mkdir(parents=True)
        (repo_dir / '.git').mkdir()

        result = find_repo_in_search_paths('myrepo', [tmp_path / 'code'], claimed_paths={repo_dir})
        assert result is None
