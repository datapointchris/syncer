from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import patch

from rich.console import Console

from syncer.config import SyncerConfig
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
