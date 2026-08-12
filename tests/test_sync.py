import subprocess
from pathlib import Path

from syncer.breaker import Trip
from syncer.config import RepoConfig
from syncer.config import SyncerConfig
from syncer.config import ToolConfig
from syncer.diagnose import Cause
from syncer.output import Tally
from syncer.report import LIFECYCLE_STYLE
from syncer.report import RepoBranchReport
from syncer.report import attention_tally
from syncer.report import gather_reports
from syncer.repos import GitFailure
from syncer.sync import _FAILED_STATUSES
from syncer.sync import _ISSUE_STATUSES
from syncer.sync import _LIFECYCLE_TO_STATUS
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

    def test_a_rejected_clone_is_not_recorded_as_never_tried(self):
        """Both mapped to 'missing' before, so history could never distinguish 'git said no'
        from 'nobody has got round to it' — and by then the run is long gone."""
        assert _repo_status(_lifecycle_report('clone_failed')) == 'clone_failed'
        assert _repo_status(_lifecycle_report('would_clone')) == 'missing'

    def test_an_unreadable_repo_is_unverified_not_issues(self):
        """'issues' means the state was read and is untidy; this is the state not being read."""
        report = RepoBranchReport(
            label='r',
            path='~/r',
            error='fetch failed',
            failures=[GitFailure(argv=('fetch',), returncode=128, stderr='boom')],
        )
        assert _repo_status(report) == 'unverified'

    def test_a_check_run_records_pending_not_a_mutation_it_never_made(self, tmp_path):
        """severity OPERATION now happens in `check` too — the actions are decided and nothing
        would refuse them. Reusing 'pulled' there would write a mutation into the history that
        `stats` reads back as fact."""
        repo_path = _make_cloned_repo(tmp_path, 'behind')
        second = tmp_path / 'second'
        subprocess.run(['git', 'clone', str(tmp_path / 'behind.git'), str(second)], capture_output=True)
        _git(second, 'config', 'user.email', 't@t.com')
        _git(second, 'config', 'user.name', 'T')
        _git(second, 'commit', '--allow-empty', '-m', 'remote work')
        _git(second, 'push')
        from syncer.report import gather_reports

        # clone_url named explicitly: origin here is a tmp_path bare repo, and the registry's
        # default github.com URL would trip origin_mismatch and lift the repo to WARNING for a
        # reason that has nothing to do with what is being asserted.
        config = SyncerConfig(
            owner='demo',
            host='https://github.com',
            search_paths=[],
            repos=[RepoConfig(name='behind', path=str(repo_path), clone_url=str(tmp_path / 'behind.git'))],
        )
        report = gather_reports(config, ToolConfig(), jitter=0.0)[0]
        assert report.origin_mismatch is None
        assert _repo_status(report) == 'pending'

    def test_every_lifecycle_status_has_a_tracking_status(self):
        """A key in one map and not the other is a KeyError at _repo_status, on a run that has
        already done all its git work."""
        assert set(LIFECYCLE_STYLE) == set(_LIFECYCLE_TO_STATUS)


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


def _config_matching_origin(paths: list[Path]) -> SyncerConfig:
    """A registry whose clone URL is each clone's real origin, so a synced repo is severity SYNCED
    rather than a WARNING for pointing somewhere the registry never named."""
    return SyncerConfig(
        owner='demo',
        host='https://github.com',
        search_paths=[],
        repos=[RepoConfig(name=p.name, path=str(p), clone_url=str(p.parent / f'{p.name}.git')) for p in paths],
    )


class TestASkippedRepoIsUnverifiedNotUntidy:
    """A skipped repo has no failures of its own — that is the point of skipping it — so the
    error branch would file it under `issues`, i.e. as a repo somebody looked at and found
    wanting. Nobody looked at it."""

    def _skipped(self) -> RepoBranchReport:
        return RepoBranchReport(label='r', path='~/r', name='r', error='not checked', skipped=Trip(host='h', cause=Cause.AUTH))

    def test_status_is_unverified(self):
        assert _repo_status(self._skipped()) == 'unverified'

    def test_the_snapshot_carries_it_into_the_run_history(self):
        """`N need you` reads as 'there is work to do'. The truth is syncer could not find out,
        and the summary counts that separately in red."""
        assert _snapshot(self._skipped()).status == 'unverified'


class TestTheDefaultRunShowsOnlyWhatNeedsSomething:
    def test_synced_repos_are_counted_but_not_listed(self, tmp_path, capsys):
        paths = [_make_cloned_repo(tmp_path, name) for name in ('alpha', 'bravo')]
        run_sync(_config_matching_origin(paths), ToolConfig(default_policy='observe'), tmp_path / 'events.jsonl', jitter=0.0)
        out = capsys.readouterr().out
        assert '2 synced' in out
        assert 'alpha' not in out
        assert '2 repos not shown' in out

    def test_verbose_lists_them(self, tmp_path, capsys):
        paths = [_make_cloned_repo(tmp_path, name) for name in ('alpha', 'bravo')]
        run_sync(_config_matching_origin(paths), ToolConfig(default_policy='observe'), tmp_path / 'events.jsonl', jitter=0.0, verbose=True)
        out = capsys.readouterr().out
        assert 'alpha' in out
        assert 'repos not shown' not in out

    def test_a_repo_needing_something_is_never_hidden(self, tmp_path, capsys):
        paths = [_make_cloned_repo(tmp_path, name) for name in ('alpha', 'bravo')]
        _git(paths[1], 'commit', '--allow-empty', '-m', 'unpushed')
        run_sync(_config_matching_origin(paths), ToolConfig(default_policy='observe'), tmp_path / 'events.jsonl', jitter=0.0)
        out = capsys.readouterr().out
        assert 'bravo' in out
        assert 'alpha' not in out

    def test_the_event_stream_still_records_every_repo(self, tmp_path):
        """Hiding is a rendering decision. History that only held the repos worth printing would
        make `stats` a report on the bad days."""
        paths = [_make_cloned_repo(tmp_path, name) for name in ('alpha', 'bravo')]
        events_file = tmp_path / 'events.jsonl'
        run_sync(_config_matching_origin(paths), ToolConfig(default_policy='observe'), events_file, jitter=0.0)
        event = read_events(events_file)[-1]
        assert {repo.name for repo in event.repos} == {'alpha', 'bravo'}
        assert event.summary.total == 2


def _summary_tally(status: str) -> Tally | None:
    """Which counter the summary line prints a repo under, read from sync.py's own status sets.

    Joined on the sets rather than on the printed words: a test that matches an English sentence
    fails whenever someone rewrites it, which teaches you to loosen the assertion.
    """
    if status == 'pending':
        return Tally.TO_SYNC
    if status in _ISSUE_STATUSES:
        return Tally.NEEDS_YOU
    if status in _FAILED_STATUSES:
        return Tally.UNVERIFIED
    return None


class TestTheLiveTallyAndTheSummaryLineAgree:
    """Both count the same run, seconds apart, so a repo must land in one band under both.

    This is the join that was missing. The display printed `failed` where the summary printed
    `unverified`, and the test asserting the invariant read the display's own copy of the words —
    so the divergence was green on the commit that introduced it.
    """

    def _lifecycle(self, status: str) -> RepoBranchReport:
        return RepoBranchReport(label='r', path='~/r', name='r', lifecycle=status)

    def _cases(self) -> dict[str, RepoBranchReport]:
        failure = GitFailure(argv=('fetch',), returncode=128, stderr='Authentication failed')
        return {
            'unreadable': RepoBranchReport(label='r', path='~/r', name='r', error='fetch failed', failures=[failure]),
            'unknown policy': RepoBranchReport(label='r', path='~/r', name='r', error='unknown policy'),
            'skipped': RepoBranchReport(label='r', path='~/r', name='r', error='not checked', skipped=Trip(host='h', cause=Cause.AUTH)),
            'clone failed': self._lifecycle('clone_failed'),
            'never cloned': self._lifecycle('would_clone'),
            'not a repo': self._lifecycle('not_git'),
            'no remote': self._lifecycle('no_remote'),
            'moved': self._lifecycle('path_mismatch'),
        }

    def test_each_report_lands_in_the_same_band_on_both_surfaces(self):
        for name, report in self._cases().items():
            assert attention_tally(report) == _summary_tally(_repo_status(report)), name

    def test_a_synced_repo_gets_no_counter_on_either(self, tmp_path):
        paths = [_make_cloned_repo(tmp_path, 'alpha')]
        config = _config_matching_origin(paths)
        report = gather_reports(config, ToolConfig(default_policy='observe'), jitter=0.0)[0]
        assert attention_tally(report) is None
        assert _repo_status(report) == 'synced'

    def test_a_completed_action_is_counted_live_under_the_band_it_belongs_to(self, tmp_path):
        """The green `N pulled` counters are the past tense of the same band, and only an executed
        run can know them. A live display cannot, so it counts the band and says `to sync`."""
        report = self._lifecycle('cloned')
        assert _repo_status(report) == 'cloned'
        assert _summary_tally('cloned') is None  # it has its own green counter
        assert attention_tally(report) is Tally.TO_SYNC
