import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from syncer.config import RepoConfig
from syncer.config import SyncerConfig
from syncer.config import ToolConfig
from syncer.fetch import repo_outcome
from syncer.fetch import run_fetch
from syncer.main import app
from syncer.output import ICON_OK
from syncer.report import RepoBranchReport
from syncer.report import Severity
from syncer.report import exit_code_for
from syncer.report import report_severity
from syncer.repos import RefMoves
from syncer.repos import Repo

runner = CliRunner()


def git(path: Path, *args: str) -> str:
    return subprocess.run(['git', *args], cwd=path, capture_output=True, text=True).stdout.strip()


def make_cloned_repo(tmp_path: Path, name: str) -> Path:
    bare = tmp_path / f'{name}.git'
    subprocess.run(['git', 'init', '--bare', '-b', 'main', str(bare)], capture_output=True)
    repo_path = tmp_path / name
    subprocess.run(['git', 'clone', str(bare), str(repo_path)], capture_output=True)
    git(repo_path, 'config', 'user.email', 'test@test.com')
    git(repo_path, 'config', 'user.name', 'Test')
    (repo_path / 'README.md').write_text('# Test\n')
    git(repo_path, 'add', '.')
    git(repo_path, 'commit', '-m', 'init')
    git(repo_path, 'push', '-u', 'origin', 'main')
    return repo_path


def push_from_another_clone(tmp_path: Path, name: str, branch: str = 'main') -> str:
    """Land a commit on origin the way a second machine would, and return its sha."""
    other = tmp_path / f'{name}-elsewhere'
    if not other.exists():
        subprocess.run(['git', 'clone', str(tmp_path / f'{name}.git'), str(other)], capture_output=True)
        git(other, 'config', 'user.email', 'test@test.com')
        git(other, 'config', 'user.name', 'Test')
    git(other, 'checkout', '-B', branch)
    git(other, 'commit', '--allow-empty', '-m', f'upstream on {branch}')
    git(other, 'push', 'origin', branch)
    return git(other, 'rev-parse', 'HEAD')


def tag_moved_upstream(tmp_path: Path, repo_path: Path, tag_refspec: bool) -> str:
    """Tag the clone's commit, push it, then move the tag to a newer commit upstream. Returns the old commit.

    `tag_refspec` sets on the clone what `fetch.pruneTags` adds: an explicit tag refspec, which is
    the only thing that makes git refuse a moved tag. Without it auto-follow skips any tag that
    already exists locally and exits 0. Stated here rather than inherited, because a developer's
    git config decides it otherwise and CI has none.
    """
    git(repo_path, 'config', 'fetch.pruneTags', 'true' if tag_refspec else 'false')
    first = git(repo_path, 'rev-parse', 'HEAD')
    git(repo_path, 'tag', 'v1')
    git(repo_path, 'push', 'origin', 'v1')
    git(tmp_path / f'{repo_path.name}.git', 'update-ref', 'refs/tags/v1', push_from_another_clone(tmp_path, repo_path.name))
    return first


def config_for(paths: list[Path]) -> SyncerConfig:
    return SyncerConfig(
        owner='demo', host='https://github.com', search_paths=[], repos=[RepoConfig(name=p.name, path=str(p)) for p in paths]
    )


def fetch_reports(paths: list[Path]) -> list[RepoBranchReport]:
    """The real run, rendered as JSON so nothing reaches the terminal."""
    return run_fetch(config_for(paths), ToolConfig(), jitter=0.0, as_json=True)


def by_name(reports: list[RepoBranchReport]) -> dict[str, RepoBranchReport]:
    return {report.name: report for report in reports}


def index_state(repo: Path) -> tuple[int, bytes]:
    index = repo / '.git' / 'index'
    return index.stat().st_mtime_ns, index.read_bytes()


def make_index_stale(repo: Path) -> None:
    """Age a tracked file's mtime, so any `git status` rewrites the index to refresh it."""
    os.utime(repo / 'README.md', (1_000_000_000, 1_000_000_000))


class TestRefMoves:
    def test_a_new_or_moved_remote_ref_is_updated(self):
        before = {'refs/remotes/origin/main': 'a'}
        after = {'refs/remotes/origin/main': 'b', 'refs/remotes/origin/feature': 'c'}
        assert RefMoves.between(before, after).updated == ('origin/feature', 'origin/main')

    def test_a_vanished_remote_ref_is_pruned(self):
        moves = RefMoves.between({'refs/remotes/origin/main': 'a', 'refs/remotes/origin/old': 'b'}, {'refs/remotes/origin/main': 'a'})
        assert moves.pruned == ('origin/old',)
        assert moves.updated == ()

    def test_a_new_tag_is_named_without_its_prefix(self):
        assert RefMoves.between({}, {'refs/tags/v1.2.0': 'a'}).tags == ('v1.2.0',)

    def test_identical_snapshots_are_nothing_moved(self):
        refs = {'refs/remotes/origin/main': 'a', 'refs/tags/v1': 'b'}
        assert not RefMoves.between(refs, dict(refs))


class TestRemoteRefs:
    def test_origin_head_is_left_out_so_one_moved_branch_counts_once(self, tmp_path):
        repo_path = make_cloned_repo(tmp_path, 'api')
        git(repo_path, 'remote', 'set-head', 'origin', 'main')
        refs = Repo(name='api', path=repo_path, owner='demo', host='https://github.com').remote_refs()
        assert refs is not None
        assert 'refs/remotes/origin/HEAD' not in refs
        assert refs['refs/remotes/origin/main'] == git(repo_path, 'rev-parse', 'HEAD')


class TestFetchMovesOnlyRemoteRefs:
    def test_a_clone_behind_origin_gets_origin_main_and_nothing_else(self, tmp_path):
        repo_path = make_cloned_repo(tmp_path, 'api')
        local_main = git(repo_path, 'rev-parse', 'main')
        (repo_path / 'notes.txt').write_text('uncommitted\n')
        upstream = push_from_another_clone(tmp_path, 'api')

        report = fetch_reports([repo_path])[0]

        assert report.ref_moves == RefMoves(updated=('origin/main',))
        assert git(repo_path, 'rev-parse', 'origin/main') == upstream
        assert git(repo_path, 'rev-parse', 'main') == local_main
        assert git(repo_path, 'symbolic-ref', 'HEAD') == 'refs/heads/main'
        assert (repo_path / 'notes.txt').read_text() == 'uncommitted\n'

    def test_a_second_run_finds_nothing_new(self, tmp_path):
        repo_path = make_cloned_repo(tmp_path, 'api')
        push_from_another_clone(tmp_path, 'api')
        assert repo_outcome(fetch_reports([repo_path])[0]) == 'updated'
        assert repo_outcome(fetch_reports([repo_path])[0]) == 'current'

    def test_a_new_and_a_deleted_branch_are_named(self, tmp_path):
        repo_path = make_cloned_repo(tmp_path, 'api')
        push_from_another_clone(tmp_path, 'api', branch='doomed')
        fetch_reports([repo_path])
        git(tmp_path / 'api.git', 'branch', '-D', 'doomed')
        push_from_another_clone(tmp_path, 'api', branch='feature')

        moves = fetch_reports([repo_path])[0].ref_moves

        assert moves == RefMoves(updated=('origin/feature',), pruned=('origin/doomed',))

    def test_local_commits_are_never_pushed(self, tmp_path):
        repo_path = make_cloned_repo(tmp_path, 'api')
        origin_main = git(tmp_path / 'api.git', 'rev-parse', 'main')
        git(repo_path, 'commit', '--allow-empty', '-m', 'local only')
        fetch_reports([repo_path])
        assert git(tmp_path / 'api.git', 'rev-parse', 'main') == origin_main

    def test_the_index_is_never_rewritten_even_when_the_fetch_fails(self, tmp_path):
        """A failed fetch is where a report reaches for local counts, and `git status` rewrites a stale index."""
        fetched = make_cloned_repo(tmp_path, 'api')
        failing = make_cloned_repo(tmp_path, 'web')
        shutil.rmtree(tmp_path / 'web.git')
        for repo_path in (fetched, failing):
            make_index_stale(repo_path)
        before = {repo_path: index_state(repo_path) for repo_path in (fetched, failing)}

        fetch_reports([fetched, failing])

        assert {repo_path: index_state(repo_path) for repo_path in (fetched, failing)} == before


class TestTagsAreNeverForced:
    @pytest.mark.parametrize(('tag_refspec', 'named'), [(True, ['v1']), (False, [])])
    def test_a_moved_tag_is_kept_while_a_new_tag_arrives(self, tmp_path, tag_refspec, named):
        """Only a tag refspec makes git refuse the moved tag, so only then can it be named."""
        repo_path = make_cloned_repo(tmp_path, 'api')
        first = tag_moved_upstream(tmp_path, repo_path, tag_refspec)
        bare = tmp_path / 'api.git'
        git(bare, 'update-ref', 'refs/tags/v2', git(bare, 'rev-parse', 'main'))

        report = fetch_reports([repo_path])[0]

        assert report.moved_tags == named
        assert report.ref_moves is not None
        assert report.ref_moves.tags == ('v2',)
        assert git(repo_path, 'rev-parse', 'v1') == first
        assert report_severity(report) is Severity.SYNCED


class TestFailuresAreNamedAndTheRunCarriesOn:
    def test_an_unreachable_remote_fails_that_repo_alone(self, tmp_path):
        reachable = make_cloned_repo(tmp_path, 'api')
        unreachable = make_cloned_repo(tmp_path, 'web')
        push_from_another_clone(tmp_path, 'api')
        shutil.rmtree(tmp_path / 'web.git')

        reports = by_name(fetch_reports([reachable, unreachable]))

        assert repo_outcome(reports['api']) == 'updated'
        assert reports['web'].error == 'fetch failed'
        assert reports['web'].failures
        assert exit_code_for(list(reports.values())) == 1

    def test_a_missing_clone_is_named_and_never_cloned(self, tmp_path):
        missing = tmp_path / 'absent'
        report = fetch_reports([missing])[0]
        assert report.lifecycle == 'not_cloned'
        assert not missing.exists()
        assert exit_code_for([report]) == 1


class TestCli:
    def _run(self, tmp_path, monkeypatch, paths, *args):
        registry = tmp_path / 'repos.json'
        repos = [{'name': p.name, 'path': str(p)} for p in paths]
        registry.write_text(json.dumps({'owner': 'me', 'host': 'https://github.com', 'search_paths': [], 'repos': repos}))
        monkeypatch.setattr('syncer.main.notify', lambda *_: None)
        monkeypatch.setattr('syncer.config.TOOL_CONFIG_PATH', tmp_path / 'absent.toml')
        monkeypatch.setattr('syncer.tracking.STATE_DIR', tmp_path / 'state')
        return runner.invoke(app, ['fetch', *args, '-c', str(registry)])

    def test_a_clean_run_prints_one_line_and_exits_zero(self, tmp_path, monkeypatch):
        repo_path = make_cloned_repo(tmp_path, 'api')
        result = self._run(tmp_path, monkeypatch, [repo_path])
        assert result.exit_code == 0
        assert result.stdout.splitlines() == [f'{ICON_OK}  1 current']

    def test_a_stale_tag_is_counted_on_the_one_line_and_named_under_verbose(self, tmp_path, monkeypatch):
        """A floating tag moves on every release, so listing it would put a line in every unattended run."""
        repo_path = make_cloned_repo(tmp_path, 'api')
        tag_moved_upstream(tmp_path, repo_path, tag_refspec=True)
        self._run(tmp_path, monkeypatch, [repo_path])

        quiet = self._run(tmp_path, monkeypatch, [repo_path])
        verbose = self._run(tmp_path, monkeypatch, [repo_path], '-v')

        assert quiet.exit_code == 0
        assert len(quiet.stdout.splitlines()) == 1
        assert '1 stale tag origin moved' in quiet.stdout
        assert 'tag v1 — origin moved it' in verbose.stdout

    def test_no_run_history_is_recorded(self, tmp_path, monkeypatch):
        repo_path = make_cloned_repo(tmp_path, 'api')
        self._run(tmp_path, monkeypatch, [repo_path])
        assert not (tmp_path / 'state').exists()

    def test_verbose_names_what_moved(self, tmp_path, monkeypatch):
        repo_path = make_cloned_repo(tmp_path, 'api')
        push_from_another_clone(tmp_path, 'api')
        result = self._run(tmp_path, monkeypatch, [repo_path], '-v')
        assert 'api — origin/main' in result.stdout

    def test_json_carries_each_repo_and_exits_one_on_a_failure(self, tmp_path, monkeypatch):
        repo_path = make_cloned_repo(tmp_path, 'api')
        push_from_another_clone(tmp_path, 'api')
        missing = tmp_path / 'absent'

        result = self._run(tmp_path, monkeypatch, [repo_path, missing], '--json')

        payload = json.loads(result.stdout)  # nothing else may be on stdout
        assert result.exit_code == 1
        assert payload['summary'] == {'current': 0, 'updated': 1, 'not_fetched': 1, 'stale_tags': 0}
        statuses = {repo['name']: repo for repo in payload['repos']}
        assert statuses['api']['status'] == 'updated'
        assert statuses['api']['updated'] == ['origin/main']
        assert statuses['absent']['status'] == 'not_cloned'
