import subprocess
from pathlib import Path

from syncer.config import RepoConfig
from syncer.config import SyncerConfig
from syncer.config import ToolConfig
from syncer.config import resolve_policies
from syncer.policy import Action
from syncer.policy import PrimaryState
from syncer.report import _build_repo_report
from syncer.report import report_branches


def _git(path: Path, *args: str) -> None:
    subprocess.run(['git', *args], cwd=path, capture_output=True, text=True)


def _make_cloned_repo(tmp_path: Path, name: str) -> Path:
    bare = tmp_path / f'{name}.git'
    subprocess.run(['git', 'init', '--bare', '-b', 'main', str(bare)], capture_output=True)
    repo_path = tmp_path / name
    subprocess.run(['git', 'clone', str(bare), str(repo_path)], capture_output=True)
    _git(repo_path, 'config', 'user.email', 'test@test.com')
    _git(repo_path, 'config', 'user.name', 'Test')
    (repo_path / 'README.md').write_text('# Test\n')
    _git(repo_path, 'add', '.')
    _git(repo_path, 'commit', '-m', 'init')
    _git(repo_path, 'push', '-u', 'origin', 'main')
    return repo_path


def _config_for(paths: list[Path]) -> SyncerConfig:
    return SyncerConfig(
        owner='demo',
        host='https://github.com',
        search_paths=[],
        repos=[RepoConfig(name=p.name, path=str(p)) for p in paths],
    )


class TestBuildRepoReport:
    def test_returns_rows_for_real_repo(self, tmp_path):
        repo_path = _make_cloned_repo(tmp_path, 'alpha')
        config = _config_for([repo_path])
        report = _build_repo_report(config.repos[0], config, ToolConfig(), resolve_policies(ToolConfig()), 'observe', False, 0.0)
        assert report is not None
        assert report.policy_name == 'observe'
        assert any(row.state.branch == 'main' for row in report.rows)

    def test_skips_missing_repo(self, tmp_path):
        config = _config_for([tmp_path / 'does-not-exist'])
        report = _build_repo_report(config.repos[0], config, ToolConfig(), resolve_policies(ToolConfig()), None, False, 0.0)
        assert report is None

    def test_apply_attaches_outcomes(self, tmp_path):
        repo_path = _make_cloned_repo(tmp_path, 'beta')
        _git(repo_path, 'commit', '--allow-empty', '-m', 'unpushed')
        config = _config_for([repo_path])
        report = _build_repo_report(config.repos[0], config, ToolConfig(), resolve_policies(ToolConfig()), 'mirror', True, 0.0)
        assert report is not None
        main_row = next(row for row in report.rows if row.state.branch == 'main')
        assert main_row.state.primary == PrimaryState.AHEAD
        assert main_row.action == Action.PUSH
        assert main_row.outcome is not None
        assert main_row.outcome.status == 'done'


class TestReportOrdering:
    def test_output_preserves_config_order(self, tmp_path, capsys):
        # Deliberately out of alphabetical order to prove pool.map preserves submission order
        # (real configs are path-sorted at load time, so this yields directory order).
        names = ['charlie', 'alpha', 'bravo']
        paths = [_make_cloned_repo(tmp_path, name) for name in names]
        config = _config_for(paths)
        report_branches(config, ToolConfig(default_policy='observe'), jitter=0.0)
        out = capsys.readouterr().out
        positions = [out.index(name) for name in names]
        assert positions == sorted(positions)  # appear in the order submitted

    def test_all_repos_processed_concurrently(self, tmp_path, capsys):
        paths = [_make_cloned_repo(tmp_path, f'repo{i}') for i in range(5)]
        config = _config_for(paths)
        report_branches(config, ToolConfig(default_policy='observe'), jobs=4, jitter=0.0)
        out = capsys.readouterr().out
        for i in range(5):
            assert f'repo{i}' in out
