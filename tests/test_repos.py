import subprocess
from pathlib import Path

import pytest

from syncer.repos import Repo
from syncer.repos import find_repo_in_search_paths


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


class TestRepo:
    def test_init(self):
        repo = Repo(name='myrepo', path=Path('/tmp/myrepo'), owner='user', host='https://github.com')
        assert repo.name == 'myrepo'
        assert repo.url == 'https://github.com/user/myrepo'

    def test_exists(self, tmp_path):
        repo = Repo(name='exists', path=tmp_path, owner='user', host='https://github.com')
        assert repo.exists is True

    def test_not_exists(self, tmp_path):
        repo = Repo(name='gone', path=tmp_path / 'nope', owner='user', host='https://github.com')
        assert repo.exists is False

    def test_is_git_repo(self, git_repo):
        repo = Repo(name='test-repo', path=git_repo, owner='user', host='https://github.com')
        assert repo.is_git_repo is True

    def test_is_not_git_repo(self, tmp_path):
        repo = Repo(name='not-git', path=tmp_path, owner='user', host='https://github.com')
        assert repo.is_git_repo is False

    def test_has_no_remote(self, git_repo):
        repo = Repo(name='test-repo', path=git_repo, owner='user', host='https://github.com')
        assert repo.has_remote is False

    def test_current_branch(self, git_repo):
        repo = Repo(name='test-repo', path=git_repo, owner='user', host='https://github.com')
        branch = repo.current_branch
        assert branch in ('main', 'master')

    def test_uncommitted_changes_empty(self, git_repo):
        repo = Repo(name='test-repo', path=git_repo, owner='user', host='https://github.com')
        assert repo.uncommitted_changes == []

    def test_uncommitted_changes_detected(self, git_repo):
        (git_repo / 'newfile.txt').write_text('hello')
        repo = Repo(name='test-repo', path=git_repo, owner='user', host='https://github.com')
        assert len(repo.uncommitted_changes) > 0

    def test_stash_count_zero(self, git_repo):
        repo = Repo(name='test-repo', path=git_repo, owner='user', host='https://github.com')
        assert repo.stash_count == 0

    def test_default_branch_detection(self, git_repo):
        repo = Repo(name='test-repo', path=git_repo, owner='user', host='https://github.com')
        branch = repo.default_branch
        assert branch in ('main', 'master')


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
