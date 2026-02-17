from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import patch

from rich.console import Console

from syncer.config import RepoConfig
from syncer.config import SyncerConfig
from syncer.stats import _format_duration
from syncer.stats import _show_commits_graph
from syncer.stats import _show_repo_age
from syncer.stats import _time_ago
from syncer.stats import show_stats
from syncer.tracking import RepoSnapshot
from syncer.tracking import RunSummary
from syncer.tracking import SyncRunEvent


def _make_snapshot(name='repo1', status='synced', uncommitted=0, **kwargs):
    return RepoSnapshot(name=name, path=f'~/code/{name}', status=status, uncommitted=uncommitted, **kwargs)


def _make_event(repos=None, timestamp=None, config_name='default', **summary_overrides):
    repos = repos or [_make_snapshot()]
    ts = timestamp or datetime.now(UTC)
    defaults = {'total': len(repos), 'synced': len(repos), 'pulled': 0, 'pushed': 0, 'issues': 0, 'duration_ms': 100}
    defaults.update(summary_overrides)
    return SyncRunEvent(timestamp=ts, config_name=config_name, repos=repos, summary=RunSummary(**defaults))


class TestTimeAgo:
    def test_just_now(self):
        assert _time_ago(datetime.now(UTC)) == 'just now'

    def test_minutes(self):
        assert _time_ago(datetime.now(UTC) - timedelta(minutes=30)) == '30m ago'

    def test_hours(self):
        assert _time_ago(datetime.now(UTC) - timedelta(hours=3)) == '3h ago'

    def test_days(self):
        assert _time_ago(datetime.now(UTC) - timedelta(days=5)) == '5d ago'

    def test_months(self):
        assert _time_ago(datetime.now(UTC) - timedelta(days=60)) == '2mo ago'

    def test_years(self):
        assert _time_ago(datetime.now(UTC) - timedelta(days=400)) == '1y ago'


class TestShowStats:
    def test_no_events(self, tmp_path):
        """Stats with no events should not crash."""
        config = SyncerConfig(owner='test', host='https://github.com', repos=[])
        console = Console(file=open(tmp_path / 'output.txt', 'w'))  # noqa: SIM115
        with (
            patch('syncer.stats.console', console),
            patch('syncer.stats.read_events', return_value=[]),
        ):
            show_stats(config)

    def test_with_events(self, tmp_path):
        """Stats with events should render all sections."""
        events = [
            _make_event(
                repos=[
                    _make_snapshot(),
                    _make_snapshot(name='dirty', status='issues', uncommitted=3),
                ],
                timestamp=datetime.now(UTC) - timedelta(days=2),
                issues=1,
            ),
            _make_event(
                repos=[
                    _make_snapshot(),
                    _make_snapshot(name='dirty', status='issues', uncommitted=2),
                ],
                timestamp=datetime.now(UTC) - timedelta(hours=1),
                issues=1,
            ),
        ]
        config = SyncerConfig(owner='test', host='https://github.com', repos=[])
        output_file = tmp_path / 'output.txt'
        console = Console(file=open(output_file, 'w'), width=120)  # noqa: SIM115
        with (
            patch('syncer.stats.console', console),
            patch('syncer.stats.read_events', return_value=events),
        ):
            show_stats(config)
        output = output_file.read_text()
        assert 'Summary' in output
        assert 'Total runs' in output
        assert 'Recent Runs' in output

    def test_summary_shows_correct_counts(self, tmp_path):
        """Verify summary section math."""
        events = [_make_event(timestamp=datetime.now(UTC) - timedelta(days=i), issues=i % 3) for i in range(5)]
        config = SyncerConfig(owner='test', host='https://github.com', repos=[])
        output_file = tmp_path / 'output.txt'
        console = Console(file=open(output_file, 'w'), width=120)  # noqa: SIM115
        with (
            patch('syncer.stats.console', console),
            patch('syncer.stats.read_events', return_value=events),
        ):
            show_stats(config)
        output = output_file.read_text()
        assert 'Total runs:     5' in output

    def test_recent_runs_shows_latest_first(self, tmp_path):
        """Recent runs should be reverse chronological."""
        events = [
            _make_event(timestamp=datetime(2025, 1, 10, tzinfo=UTC), issues=2),
            _make_event(timestamp=datetime(2025, 1, 12, tzinfo=UTC)),
        ]
        config = SyncerConfig(owner='test', host='https://github.com', repos=[])
        output_file = tmp_path / 'output.txt'
        console = Console(file=open(output_file, 'w'), width=120)  # noqa: SIM115
        with (
            patch('syncer.stats.console', console),
            patch('syncer.stats.read_events', return_value=events),
        ):
            show_stats(config)
        output = output_file.read_text()
        # Jan 12 should appear before Jan 10
        pos_12 = output.find('Jan 12')
        pos_10 = output.find('Jan 10')
        assert pos_12 < pos_10

    def test_all_repos_sorted_by_last_active(self, tmp_path):
        """All Repos table should sort most recently active first."""
        for name in ('old-repo', 'new-repo'):
            (tmp_path / name / '.git').mkdir(parents=True)

        config = SyncerConfig(
            owner='test',
            host='https://github.com',
            repos=[
                RepoConfig(name='old-repo', path=str(tmp_path / 'old-repo')),
                RepoConfig(name='new-repo', path=str(tmp_path / 'new-repo')),
            ],
        )
        old_date = (datetime.now(UTC) - timedelta(days=90)).isoformat()
        new_date = (datetime.now(UTC) - timedelta(days=1)).isoformat()

        output_file = tmp_path / 'output.txt'
        console = Console(file=open(output_file, 'w'), width=120)  # noqa: SIM115
        with (
            patch('syncer.stats.console', console),
            patch('syncer.stats.read_events', return_value=[]),
            patch('syncer.stats.Repo') as MockRepo,
        ):

            def make_repo(name, path, owner, host):
                from unittest.mock import MagicMock

                m = MagicMock()
                m.name = name
                m.is_fork = False
                m.total_commits = 50 if name == 'old-repo' else 200
                m.last_commit_date = old_date if name == 'old-repo' else new_date
                m.first_commit_date = old_date if name == 'old-repo' else new_date
                return m

            MockRepo.side_effect = make_repo
            show_stats(config)

        output = output_file.read_text()
        # Find positions within the "All Repos" section specifically
        all_repos_start = output.find('All Repos')
        pos_new = output.find('new-repo', all_repos_start)
        pos_old = output.find('old-repo', all_repos_start)
        assert pos_new < pos_old, 'new-repo should appear before old-repo in All Repos'


class TestFormatDuration:
    def test_days(self):
        assert _format_duration(15) == '15d'

    def test_zero_days(self):
        assert _format_duration(0) == '0d'

    def test_months(self):
        assert _format_duration(60) == '2mo'

    def test_exactly_30_days(self):
        assert _format_duration(30) == '1mo'

    def test_years_with_months(self):
        assert _format_duration(400) == '1y 1mo'

    def test_years_without_months(self):
        assert _format_duration(365) == '1y'

    def test_multiple_years(self):
        assert _format_duration(1000) == '2y 9mo'


class TestShowCommitsGraph:
    def test_renders_bars(self, tmp_path):
        for name in ('big', 'small'):
            (tmp_path / name / '.git').mkdir(parents=True)

        config = SyncerConfig(
            owner='test',
            host='https://github.com',
            repos=[
                RepoConfig(name='big', path=str(tmp_path / 'big')),
                RepoConfig(name='small', path=str(tmp_path / 'small')),
            ],
        )
        output_file = tmp_path / 'output.txt'
        console = Console(file=open(output_file, 'w'), width=120)  # noqa: SIM115
        with (
            patch('syncer.stats.console', console),
            patch('syncer.stats.Repo') as MockRepo,
        ):

            def make_repo(name, path, owner, host):
                from unittest.mock import MagicMock

                m = MagicMock()
                m.name = name
                m.is_fork = False
                m.total_commits = 300 if name == 'big' else 100
                return m

            MockRepo.side_effect = make_repo
            _show_commits_graph(config)

        output = output_file.read_text()
        assert 'Commits by Repo' in output
        assert '300' in output
        assert '100' in output

    def test_skips_forks(self, tmp_path):
        for name in ('mine', 'forked'):
            (tmp_path / name / '.git').mkdir(parents=True)

        config = SyncerConfig(
            owner='test',
            host='https://github.com',
            repos=[
                RepoConfig(name='mine', path=str(tmp_path / 'mine')),
                RepoConfig(name='forked', path=str(tmp_path / 'forked')),
            ],
        )
        output_file = tmp_path / 'output.txt'
        console = Console(file=open(output_file, 'w'), width=120)  # noqa: SIM115
        with (
            patch('syncer.stats.console', console),
            patch('syncer.stats.Repo') as MockRepo,
        ):

            def make_repo(name, path, owner, host):
                from unittest.mock import MagicMock

                m = MagicMock()
                m.name = name
                m.is_fork = name == 'forked'
                m.total_commits = 200
                return m

            MockRepo.side_effect = make_repo
            _show_commits_graph(config)

        output = output_file.read_text()
        assert 'mine' in output
        assert 'forked' not in output


class TestShowRepoAge:
    def test_renders_age_bars(self, tmp_path):
        for name in ('old', 'new'):
            (tmp_path / name / '.git').mkdir(parents=True)

        config = SyncerConfig(
            owner='test',
            host='https://github.com',
            repos=[
                RepoConfig(name='old', path=str(tmp_path / 'old')),
                RepoConfig(name='new', path=str(tmp_path / 'new')),
            ],
        )
        output_file = tmp_path / 'output.txt'
        console = Console(file=open(output_file, 'w'), width=120)  # noqa: SIM115
        with (
            patch('syncer.stats.console', console),
            patch('syncer.stats.Repo') as MockRepo,
        ):

            def make_repo(name, path, owner, host):
                from unittest.mock import MagicMock

                m = MagicMock()
                m.name = name
                m.is_fork = False
                if name == 'old':
                    m.first_commit_date = (datetime.now(UTC) - timedelta(days=500)).isoformat()
                else:
                    m.first_commit_date = (datetime.now(UTC) - timedelta(days=30)).isoformat()
                return m

            MockRepo.side_effect = make_repo
            _show_repo_age(config)

        output = output_file.read_text()
        assert 'Repo Age' in output
        assert '1y' in output
        assert '1mo' in output
