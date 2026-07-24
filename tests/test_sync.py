import subprocess
from pathlib import Path

from syncer.config import RepoConfig
from syncer.config import SyncerConfig
from syncer.config import ToolConfig
from syncer.report import RepoBranchReport
from syncer.sync import _repo_status
from syncer.sync import _snapshot
from syncer.sync import run_sync
from syncer.tracking import read_events


def _git(path: Path, *args: str) -> None:
    subprocess.run(['git', *args], cwd=path, capture_output=True, text=True)


def _make_cloned_repo(tmp_path: Path, name: str) -> Path:
    bare = tmp_path / f'{name}.git'
    subprocess.run(['git', 'init', '--bare', '-b', 'main', str(bare)], capture_output=True)
    repo_path = tmp_path / name
    subprocess.run(['git', 'clone', str(bare), str(repo_path)], capture_output=True)
    _git(repo_path, 'config', 'user.email', 't@t.com')
    _git(repo_path, 'config', 'user.name', 'T')
    (repo_path / 'README.md').write_text('# t\n')
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


def _lifecycle_report(status: str) -> RepoBranchReport:
    return RepoBranchReport(label='r', path='~/r', name='r', lifecycle=status)


class TestRepoStatus:
    def test_lifecycle_maps_to_status(self):
        assert _repo_status(_lifecycle_report('not_git')) == 'not_git'
        assert _repo_status(_lifecycle_report('no_remote')) == 'no_remote'
        assert _repo_status(_lifecycle_report('would_clone')) == 'missing'
        assert _repo_status(_lifecycle_report('cloned')) == 'cloned'
        assert _repo_status(_lifecycle_report('path_mismatch')) == 'path_mismatch'

    def test_error_report_is_issues(self):
        assert _repo_status(RepoBranchReport(label='r', path='~/r', error='unknown policy')) == 'issues'


class TestSnapshot:
    def test_snapshot_carries_per_branch_detail(self, tmp_path):
        repo_path = _make_cloned_repo(tmp_path, 'alpha')
        _git(repo_path, 'checkout', '-b', 'feature/x')
        _git(repo_path, 'commit', '--allow-empty', '-m', 'wip')
        _git(repo_path, 'checkout', 'main')
        config = _config_for([repo_path])
        from syncer.report import gather_reports

        report = gather_reports(config, ToolConfig(default_policy='observe'), jitter=0.0)[0]
        snap = _snapshot(report)
        assert snap.policy == 'observe'
        branch_names = {b.branch for b in snap.branches}
        assert branch_names == {'main', 'feature/x'}
        main_branch = next(b for b in snap.branches if b.branch == 'main')
        assert main_branch.is_default is True
        assert main_branch.primary == 'synced'


class TestRunSync:
    def test_emits_event_with_per_branch_snapshots(self, tmp_path):
        events_file = tmp_path / 'events.jsonl'
        repo_path = _make_cloned_repo(tmp_path, 'alpha')
        config = _config_for([repo_path])
        run_sync(config, ToolConfig(default_policy='observe'), jitter=0.0, events_file=events_file)
        events = read_events(events_file)
        assert len(events) == 1
        snap = events[0].repos[0]
        assert snap.branches  # per-branch detail present
        assert snap.branches[0].branch == 'main'

    def test_report_only_does_not_mutate(self, tmp_path, capsys):
        events_file = tmp_path / 'events.jsonl'
        repo_path = _make_cloned_repo(tmp_path, 'alpha')
        _git(repo_path, 'commit', '--allow-empty', '-m', 'unpushed')
        config = _config_for([repo_path])
        run_sync(config, ToolConfig(default_policy='mirror'), apply=False, jitter=0.0, events_file=events_file)
        # Still ahead — nothing was pushed in report-only mode.
        result = subprocess.run(['git', 'rev-list', '--count', 'origin/main..main'], cwd=repo_path, capture_output=True, text=True)
        assert result.stdout.strip() == '1'

    def test_apply_mutates(self, tmp_path):
        events_file = tmp_path / 'events.jsonl'
        repo_path = _make_cloned_repo(tmp_path, 'alpha')
        _git(repo_path, 'commit', '--allow-empty', '-m', 'unpushed')
        config = _config_for([repo_path])
        run_sync(config, ToolConfig(default_policy='mirror'), apply=True, jitter=0.0, events_file=events_file)
        result = subprocess.run(['git', 'rev-list', '--count', 'origin/main..main'], cwd=repo_path, capture_output=True, text=True)
        assert result.stdout.strip() == '0'  # pushed

    def test_errors_render_after_synced(self, tmp_path, capsys):
        good = _make_cloned_repo(tmp_path, 'aaa-good')
        no_remote = tmp_path / 'zzz-noremote'
        no_remote.mkdir()
        subprocess.run(['git', 'init', str(no_remote)], capture_output=True)
        config = _config_for([good, no_remote])
        run_sync(config, ToolConfig(default_policy='observe'), jitter=0.0, events_file=tmp_path / 'e.jsonl')
        out = capsys.readouterr().out
        # The no-remote error repo sorts below the synced one (nearest the prompt).
        assert out.index('aaa-good') < out.index('zzz-noremote')
