import shutil
import subprocess
from pathlib import Path

from syncer.config import RepoConfig
from syncer.config import SyncerConfig
from syncer.config import ToolConfig
from syncer.config import resolve_policies
from syncer.execute import Outcome
from syncer.execute import protection_refusal
from syncer.policy import Action
from syncer.policy import BranchState
from syncer.policy import Policy
from syncer.policy import PrimaryState
from syncer.report import BranchRow
from syncer.report import RepoBranchReport
from syncer.report import Severity
from syncer.report import _branch_line
from syncer.report import _build_repo_report
from syncer.report import _row_severity
from syncer.report import gather_reports
from syncer.report import render_failure_summary
from syncer.report import report_branches
from syncer.report import report_severity
from syncer.repos import GitFailure


def _build(repo_config, config, *, cli_policy=None, apply=False, include_lifecycle=True):
    """Call the worker with sensible test defaults for its many keyword args."""
    return _build_repo_report(
        repo_config,
        config=config,
        tool_config=ToolConfig(),
        policies=resolve_policies(ToolConfig()),
        cli_policy=cli_policy,
        apply=apply,
        jitter=0.0,
        include_lifecycle=include_lifecycle,
        search_paths=[],
        claimed_paths=set(),
    )


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
        report = _build(config.repos[0], config, cli_policy='observe')
        assert report is not None
        assert report.policy_name == 'observe'
        assert any(row.state.branch == 'main' for row in report.rows)

    def test_skips_missing_repo_without_lifecycle(self, tmp_path):
        config = _config_for([tmp_path / 'does-not-exist'])
        report = _build(config.repos[0], config, include_lifecycle=False)
        assert report is None

    def test_surfaces_missing_repo_as_lifecycle(self, tmp_path):
        config = _config_for([tmp_path / 'does-not-exist'])
        report = _build(config.repos[0], config)  # _build defaults include_lifecycle=True
        assert report is not None
        assert report.lifecycle == 'would_clone'

    def test_clone_failure_carries_the_url_and_gits_error(self, tmp_path):
        """The work-box report: every repo said 'clone failed' and nothing else, so auth, a bad
        url_template and a dead network were one indistinguishable line."""
        config = SyncerConfig(
            owner='demo',
            host='https://github.com',
            search_paths=[],
            repos=[RepoConfig(name='ghost', path=str(tmp_path / 'ghost'), clone_url=str(tmp_path / 'no-such-repo.git'))],
        )
        report = _build(config.repos[0], config, apply=True)
        assert report is not None
        assert report.lifecycle == 'clone_failed'
        assert report.lifecycle_detail
        assert str(tmp_path / 'no-such-repo.git') in report.lifecycle_detail
        assert len(report.lifecycle_detail.splitlines()) > 1  # the URL, then git's own words

    def test_apply_attaches_outcomes(self, tmp_path):
        repo_path = _make_cloned_repo(tmp_path, 'beta')
        _git(repo_path, 'commit', '--allow-empty', '-m', 'unpushed')
        config = _config_for([repo_path])
        report = _build(config.repos[0], config, cli_policy='mirror', apply=True)
        assert report is not None
        main_row = next(row for row in report.rows if row.state.branch == 'main')
        assert main_row.state.primary == PrimaryState.AHEAD
        assert main_row.action == Action.PUSH
        assert main_row.outcome is not None
        assert main_row.outcome.status == 'done'


class TestUnverifiableRepoIsNotSynced:
    """The deepest bug this tool had: classify_repo did not even bind the fetch's result, and
    ahead_behind returned (0, 0) when rev-list failed — which _primary_from_counts reads as
    SYNCED. So a repo that had never once reached its remote reported as fully in sync, which
    is the exact opposite of the one question syncer exists to answer.
    """

    def _repo_with_dead_remote(self, tmp_path):
        repo_path = _make_cloned_repo(tmp_path, 'orphan')
        shutil.rmtree(tmp_path / 'orphan.git')  # origin is gone; every fetch now fails
        return _config_for([repo_path])

    def test_a_dead_fetch_is_an_error_not_synced(self, tmp_path):
        config = self._repo_with_dead_remote(tmp_path)
        report = _build(config.repos[0], config, cli_policy='observe')
        assert report is not None
        assert report.error is not None
        assert report_severity(report) == Severity.ERROR
        assert not any(row.state.primary == PrimaryState.SYNCED for row in report.rows)

    def test_it_carries_gits_own_reason(self, tmp_path):
        config = self._repo_with_dead_remote(tmp_path)
        report = _build(config.repos[0], config, cli_policy='observe')
        assert report.error_detail
        assert report.failures

    def test_no_branch_rows_are_claimed(self, tmp_path):
        """A row is a claim about a branch, and the point is that no such claim can be made."""
        config = self._repo_with_dead_remote(tmp_path)
        report = _build(config.repos[0], config, cli_policy='observe')
        assert report.rows == []

    def test_apply_executes_nothing(self, tmp_path):
        """Returning before build_branch_rows is what refuses execution: no execute() call is
        ever constructed for a repo whose state could not be established."""
        config = self._repo_with_dead_remote(tmp_path)
        report = _build(config.repos[0], config, cli_policy='mirror', apply=True)
        assert report.rows == []
        assert report.error is not None

    def test_branches_view_reports_it_too(self, tmp_path):
        """A branch view built on unverified refs tells the same lie the default run did."""
        config = self._repo_with_dead_remote(tmp_path)
        report = _build(config.repos[0], config, cli_policy='observe', include_lifecycle=False)
        assert report is not None
        assert report.error is not None

    def test_local_counts_survive_for_the_stale_warnings(self, tmp_path):
        config = self._repo_with_dead_remote(tmp_path)
        (Path(config.repos[0].path) / 'dirty.txt').write_text('x\n')
        report = _build(config.repos[0], config, cli_policy='observe')
        assert report.uncommitted == 1


class TestFailureSummary:
    """hint() existed from the start and was used only by the config commands, so nothing on the
    sync surface ever told you what to *do* about a failure."""

    def _report(self, name, url, stderr):
        return RepoBranchReport(
            label=name,
            path=f'~/{name}',
            name=name,
            expected_url=url,
            lifecycle='clone_failed',
            failures=[GitFailure(argv=('clone',), returncode=128, stderr=stderr)],
        )

    def test_one_cause_across_many_repos_prints_one_block(self, capsys):
        stderr = 'ssh: connect to host git.corp port 22: Network is unreachable'
        reports = [self._report(f'repo{i}', 'git@git.corp:p/r.git', stderr) for i in range(4)]
        render_failure_summary(reports)
        out = capsys.readouterr().err
        assert out.count('Network is unreachable') == 1
        assert 'repo0, repo1, repo2, repo3' in out

    def test_the_hint_names_the_real_host_not_a_vendor(self, capsys):
        stderr = 'git@bitbucket.corp: Permission denied (publickey).'
        render_failure_summary([self._report('api', 'git@bitbucket.corp:p/api.git', stderr)])
        out = capsys.readouterr().err
        assert 'bitbucket.corp' in out
        assert 'gh auth login' not in out

    def test_an_unrecognised_failure_gets_no_invented_cause(self, capsys):
        render_failure_summary([self._report('api', 'https://git.corp/p/api.git', 'error: novel thing')])
        out = capsys.readouterr().err
        assert 'error: novel thing' in out  # raw output always survives
        assert '→' not in out  # ...but nothing is claimed about why

    def test_a_clean_run_prints_nothing(self, capsys):
        render_failure_summary([RepoBranchReport(label='a', path='~/a', name='a')])
        assert capsys.readouterr().err == ''

    def test_it_goes_to_stderr_so_it_never_corrupts_piped_output(self, capsys):
        render_failure_summary([self._report('api', 'git@h.corp:p/api.git', 'Host key verification failed.')])
        captured = capsys.readouterr()
        assert 'Host key' in captured.err
        assert captured.out == ''


class TestReportOrdering:
    def test_reports_sorted_by_path_within_same_severity(self, tmp_path):
        # Submit out of alphabetical order; all synced (same severity) → path-sorted output.
        names = ['charlie', 'alpha', 'bravo']
        paths = [_make_cloned_repo(tmp_path, name) for name in names]
        config = _config_for(paths)
        reports = gather_reports(config, ToolConfig(default_policy='observe'), jitter=0.0)
        assert [report.name for report in reports] == ['alpha', 'bravo', 'charlie']

    def test_repos_needing_attention_sort_to_the_bottom(self, tmp_path):
        synced = _make_cloned_repo(tmp_path, 'aaa-synced')
        behind = _make_cloned_repo(tmp_path, 'zzz-behind')
        # Move zzz-behind behind its remote via a second clone push.
        second = tmp_path / 'second'
        subprocess.run(['git', 'clone', str(tmp_path / 'zzz-behind.git'), str(second)], capture_output=True)
        _git(second, 'config', 'user.email', 't@t.com')
        _git(second, 'config', 'user.name', 'T')
        (second / 'x.txt').write_text('x\n')
        _git(second, 'add', '.')
        _git(second, 'commit', '-m', 'ahead')
        _git(second, 'push')
        config = _config_for([synced, behind])
        reports = gather_reports(config, ToolConfig(default_policy='observe'), jitter=0.0)
        # Even though 'aaa' < 'zzz' by path, severity dominates: synced first, behind (warning) last.
        assert reports[0].name == 'aaa-synced'
        assert reports[-1].name == 'zzz-behind'

    def test_all_repos_processed_concurrently(self, tmp_path, capsys):
        paths = [_make_cloned_repo(tmp_path, f'repo{i}') for i in range(5)]
        config = _config_for(paths)
        report_branches(config, ToolConfig(default_policy='observe'), jobs=4, jitter=0.0)
        out = capsys.readouterr().out
        for i in range(5):
            assert f'repo{i}' in out


class TestProtectedBranchReporting:
    """Protection is static config, so a report-only run can say so. Rendering the decided
    `push` alone would promise a push that --apply is never going to make."""

    def _row(self, action, protected):
        state = BranchState(branch='develop', primary=PrimaryState.AHEAD, ahead=2, upstream='origin/develop')
        policy = Policy(name='p', protected=protected)
        return BranchRow(state=state, action=action, blocked=protection_refusal(action, state, policy))

    def test_report_line_marks_an_action_protection_would_refuse(self):
        line = _branch_line(self._row(Action.PUSH, ['develop']).state, Action.PUSH, "protected by 'develop'")
        assert 'would refuse' in line
        assert 'develop' in line

    def test_report_line_is_unchanged_when_nothing_is_protected(self):
        row = self._row(Action.PUSH, [])
        assert row.blocked is None
        assert 'would refuse' not in _branch_line(row.state, row.action, row.blocked)

    def test_a_protection_refusal_is_a_warning_not_an_error(self):
        """It is the guard working as configured. Counting it as an error would paint develop
        red at the bottom of every run forever, which trains you to ignore the bottom."""
        row = self._row(Action.PUSH, ['develop'])
        row.outcome = Outcome(branch='develop', action=Action.PUSH, status='refused', message=row.blocked or '')
        assert _row_severity(row) == Severity.WARNING

    def test_other_refusals_are_still_errors(self):
        row = self._row(Action.PUSH, [])
        row.outcome = Outcome(branch='develop', action=Action.PUSH, status='refused', message='working tree is dirty')
        assert _row_severity(row) == Severity.ERROR


class TestOriginMismatchReporting:
    """A wrong origin is silent drift — the clone is healthy, it just pulls from the wrong
    place. So it annotates the branch report rather than replacing it the way a lifecycle
    status would, and it lifts the repo to WARNING so it sorts near the prompt."""

    def _clone_with_origin(self, tmp_path, origin):
        path = tmp_path / 'homelab'
        subprocess.run(['git', 'init', '-b', 'main', str(path)], capture_output=True)
        subprocess.run(['git', 'config', 'user.email', 't@t'], cwd=path, capture_output=True)
        subprocess.run(['git', 'config', 'user.name', 'T'], cwd=path, capture_output=True)
        (path / 'README.md').write_text('# x\n')
        subprocess.run(['git', 'add', '.'], cwd=path, capture_output=True)
        subprocess.run(['git', 'commit', '-m', 'init'], cwd=path, capture_output=True)
        subprocess.run(['git', 'remote', 'add', 'origin', origin], cwd=path, capture_output=True)
        config = SyncerConfig(owner='khuedoan', host='https://github.com', search_paths=[], repos=[])
        return RepoConfig(name='homelab', path=str(path)), config

    def test_flagged_when_the_origin_is_someone_elses(self, tmp_path):
        repo_config, config = self._clone_with_origin(tmp_path, 'https://github.com/datapointchris/homelab')
        report = _build(repo_config, config)
        assert report.origin_mismatch == 'https://github.com/datapointchris/homelab'
        assert report.expected_url == 'https://github.com/khuedoan/homelab'

    def test_silent_when_the_origin_matches(self, tmp_path):
        repo_config, config = self._clone_with_origin(tmp_path, 'https://github.com/khuedoan/homelab')
        assert _build(repo_config, config).origin_mismatch is None

    def test_the_branch_report_is_annotated_not_replaced(self, tmp_path):
        """A lifecycle status would return instead of classifying — losing every branch row for
        a repo whose only problem is where it points."""
        repo_config, config = self._clone_with_origin(tmp_path, 'https://github.com/datapointchris/homelab')
        # observe has scope ALL, so main is classified despite having no upstream to track.
        report = _build(repo_config, config, cli_policy='observe')
        assert report.lifecycle is None
        assert [row.state.branch for row in report.rows] == ['main']

    def test_lifts_an_otherwise_clean_repo_to_warning(self, tmp_path):
        repo_config, config = self._clone_with_origin(tmp_path, 'https://github.com/datapointchris/homelab')
        assert report_severity(_build(repo_config, config)) == Severity.WARNING


class TestWatchedRemoteBranches:
    """A fetch already brings down every remote branch, but the pipeline only iterates local
    ones — so a long-lived branch deliberately never checked out is invisible."""

    def _repo_with_remote_branches(self, tmp_path, *branches):
        bare = tmp_path / 'remote.git'
        subprocess.run(['git', 'init', '--bare', '-b', 'main', str(bare)], capture_output=True)
        seed = tmp_path / 'seed'
        subprocess.run(['git', 'clone', str(bare), str(seed)], capture_output=True)
        subprocess.run(['git', 'config', 'user.email', 't@t'], cwd=seed, capture_output=True)
        subprocess.run(['git', 'config', 'user.name', 'T'], cwd=seed, capture_output=True)
        (seed / 'README.md').write_text('# x\n')
        subprocess.run(['git', 'add', '.'], cwd=seed, capture_output=True)
        subprocess.run(['git', 'commit', '-m', 'init'], cwd=seed, capture_output=True)
        subprocess.run(['git', 'push', '-u', 'origin', 'main'], cwd=seed, capture_output=True)
        for branch in branches:
            subprocess.run(['git', 'checkout', '-b', branch], cwd=seed, capture_output=True)
            subprocess.run(['git', 'push', '-u', 'origin', branch], cwd=seed, capture_output=True)

        clone = tmp_path / 'clone'
        subprocess.run(['git', 'clone', str(bare), str(clone)], capture_output=True)
        config = SyncerConfig(owner='me', host=str(tmp_path), search_paths=[], repos=[])
        # clone_url so the origin-mismatch check agrees; without it the expected URL is the
        # three-part default and every repo here would be flagged.
        return RepoConfig(name='remote.git', path=str(clone), clone_url=str(bare)), config

    def _watched(self, tmp_path, patterns, *branches):
        repo_config, config = self._repo_with_remote_branches(tmp_path, *branches)
        tool_config = ToolConfig(policies={'w': Policy(name='w', scope='all', watch_remote=patterns)})
        report = _build_repo_report(
            repo_config,
            config=config,
            tool_config=tool_config,
            policies=resolve_policies(tool_config),
            cli_policy='w',
            apply=False,
            jitter=0,
            include_lifecycle=True,
            search_paths=[],
            claimed_paths=set(),
        )
        return report

    def test_reports_a_branch_that_exists_only_on_the_remote(self, tmp_path):
        report = self._watched(tmp_path, ['develop', 'uat', 'prod'], 'develop', 'uat')
        assert {branch for branch, _ in report.remote_only} == {'develop', 'uat'}
        assert all(age for _, age in report.remote_only)

    def test_only_watched_patterns_are_reported(self, tmp_path):
        """Every repo has remote branches you will never care about; listing all of them is a
        check that gets ignored."""
        report = self._watched(tmp_path, ['develop'], 'develop', 'feature/noise', 'wip/other')
        assert [branch for branch, _ in report.remote_only] == ['develop']

    def test_a_branch_checked_out_locally_is_not_remote_only(self, tmp_path):
        """main is cloned locally, so it classifies normally and must not double-report here."""
        report = self._watched(tmp_path, ['main', 'develop'], 'develop')
        assert [branch for branch, _ in report.remote_only] == ['develop']

    def test_empty_watch_remote_reports_nothing(self, tmp_path):
        assert self._watched(tmp_path, [], 'develop', 'uat').remote_only == []

    def test_does_not_affect_severity(self, tmp_path):
        """There is no local branch to sync — a repo is not unhealthy for having branches you
        deliberately do not keep."""
        report = self._watched(tmp_path, ['develop'], 'develop')
        assert report_severity(report) == Severity.SYNCED
