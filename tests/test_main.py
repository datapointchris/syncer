import json
import subprocess

from typer.testing import CliRunner

from syncer.main import app
from syncer.main import find_untracked_repos

runner = CliRunner()


class TestFindUntrackedRepos:
    """The untracked scan is the safety net that catches repos falling out of a
    registry. Scanning only direct children silently missed twenty of them."""

    def _repo(self, path):
        path.mkdir(parents=True, exist_ok=True)
        (path / '.git').mkdir(exist_ok=True)
        return path

    def test_finds_repos_nested_below_the_search_path(self, tmp_path):
        # ~/code/refs/<name> and ~/code/python-projects/<name> are the real shapes.
        nested = self._repo(tmp_path / 'refs' / 'bubbletea')
        found = find_untracked_repos(tmp_path, known_paths=set())
        assert nested in found

    def test_registered_repos_are_not_reported(self, tmp_path):
        known = self._repo(tmp_path / 'tools' / 'forge')
        found = find_untracked_repos(tmp_path, known_paths={known.resolve()})
        assert found == []

    def test_excluded_subtree_is_skipped_entirely(self, tmp_path):
        self._repo(tmp_path / 'refs' / 'fastapi')
        kept = self._repo(tmp_path / 'tools' / 'syncer')
        found = find_untracked_repos(tmp_path, known_paths=set(), excluded={(tmp_path / 'refs').resolve()})
        assert found == [kept]

    def test_does_not_descend_into_a_repo(self, tmp_path):
        outer = self._repo(tmp_path / 'outer')
        self._repo(outer / 'vendored')
        found = find_untracked_repos(tmp_path, known_paths=set())
        assert found == [outer]

    def test_hidden_directories_are_skipped(self, tmp_path):
        self._repo(tmp_path / '.cache' / 'thing')
        assert find_untracked_repos(tmp_path, known_paths=set()) == []


class TestIssuesMasterCheck:
    """A company's default-branch naming is not ours to change, so flagging master across
    thirty work repos is noise that can never be actioned."""

    def _registry(self, tmp_path, **extra):
        repo = tmp_path / 'work-repo'
        subprocess.run(['git', 'init', '-b', 'master', str(repo)], capture_output=True)
        subprocess.run(['git', 'config', 'user.email', 'test@test.com'], cwd=repo, capture_output=True)
        subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=repo, capture_output=True)
        (repo / 'README.md').write_text('# work\n')
        subprocess.run(['git', 'add', '.'], cwd=repo, capture_output=True)
        subprocess.run(['git', 'commit', '-m', 'init'], cwd=repo, capture_output=True)
        registry_data = {
            'owner': 'myworkspace',
            'host': 'https://bitbucket.org',
            'search_paths': [],
            'repos': [{'name': 'work-repo', 'path': str(repo)}],
        }
        registry = tmp_path / 'work-repos.json'
        registry.write_text(json.dumps(registry_data | extra))
        return registry

    def _run_issues(self, registry, monkeypatch, tmp_path):
        monkeypatch.setattr('syncer.main.notify', lambda *_: None)
        monkeypatch.setattr('syncer.config.TOOL_CONFIG_PATH', tmp_path / 'absent.toml')
        return runner.invoke(app, ['issues', '-c', str(registry)])

    def test_flagged_when_branch_naming_is_ours(self, tmp_path, monkeypatch):
        result = self._run_issues(self._registry(tmp_path), monkeypatch, tmp_path)
        assert result.exit_code == 0
        assert 'issue(s) found' in result.stdout

    def test_silent_when_branch_naming_is_not_ours(self, tmp_path, monkeypatch):
        registry = self._registry(tmp_path, owns_branch_naming=False)
        result = self._run_issues(registry, monkeypatch, tmp_path)
        assert result.exit_code == 0
        assert 'All repos healthy' in result.stdout
