from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from syncer.tracking import BranchSnapshot
from syncer.tracking import RepoSnapshot
from syncer.tracking import RepoStatus
from syncer.tracking import RunSummary
from syncer.tracking import SyncRunEvent
from syncer.tracking import emit_event
from syncer.tracking import events_file_for
from syncer.tracking import find_stale_repos
from syncer.tracking import migrate_legacy_events
from syncer.tracking import read_events


def _make_snapshot(name: str = 'repo1', status: RepoStatus = 'synced', uncommitted: int = 0, **kwargs) -> RepoSnapshot:
    return RepoSnapshot(name=name, path=f'~/code/{name}', status=status, uncommitted=uncommitted, **kwargs)


def _make_event(
    repos: list[RepoSnapshot] | None = None,
    timestamp: datetime | None = None,
    config_name: str = 'default',
    **summary_overrides,
) -> SyncRunEvent:
    repos = repos or [_make_snapshot()]
    ts = timestamp or datetime.now(UTC)
    defaults = {'total': len(repos), 'synced': len(repos), 'pulled': 0, 'pushed': 0, 'issues': 0, 'duration_ms': 100}
    defaults.update(summary_overrides)
    return SyncRunEvent(timestamp=ts, config_name=config_name, repos=repos, summary=RunSummary(**defaults))


class TestRepoSnapshot:
    def test_defaults(self):
        snap = RepoSnapshot(name='r', path='/r', status='synced')
        assert snap.uncommitted == 0
        assert snap.unpushed == 0
        assert snap.behind == 0
        assert snap.stashes == 0
        assert snap.branch is None

    def test_all_fields(self):
        snap = RepoSnapshot(name='r', path='/r', status='issues', branch='main', uncommitted=3, unpushed=1, behind=2, stashes=1)
        assert snap.uncommitted == 3
        assert snap.branch == 'main'

    def test_branches_default_empty(self):
        snap = RepoSnapshot(name='r', path='/r', status='synced')
        assert snap.branches == []
        assert snap.policy is None

    def test_legacy_event_without_branches_still_parses(self):
        # Events written before the per-branch schema have no `branches`/`policy` keys.
        legacy = (
            '{"timestamp": "2026-01-01T00:00:00+00:00", "config_name": "me", '
            '"repos": [{"name": "r", "path": "~/r", "status": "synced", "uncommitted": 0}], '
            '"summary": {"total": 1, "synced": 1, "pulled": 0, "pushed": 0, "issues": 0, "duration_ms": 5}}'
        )
        event = SyncRunEvent.model_validate_json(legacy)
        assert event.repos[0].branches == []
        assert event.repos[0].policy is None

    def test_per_branch_snapshot_round_trip(self):
        snap = RepoSnapshot(
            name='r',
            path='~/r',
            status='synced',
            policy='standard',
            branches=[BranchSnapshot(branch='main', primary='synced', is_default=True, action='skip', outcome='skipped')],
        )
        restored = RepoSnapshot.model_validate_json(snap.model_dump_json())
        assert restored.policy == 'standard'
        assert restored.branches[0].branch == 'main'
        assert restored.branches[0].outcome == 'skipped'

    def test_all_statuses(self):
        for status in ('synced', 'issues', 'pulled', 'pushed', 'cloned', 'missing', 'not_git', 'no_remote', 'path_mismatch'):
            snap = RepoSnapshot(name='r', path='/r', status=status)
            assert snap.status == status

    def test_invalid_status_rejected(self):
        with pytest.raises(ValidationError):
            RepoSnapshot(name='r', path='/r', status='bogus')


class TestRunSummary:
    def test_fields(self):
        s = RunSummary(total=10, synced=8, pulled=1, pushed=0, issues=1, duration_ms=1500)
        assert s.total == 10
        assert s.duration_ms == 1500


class TestSyncRunEvent:
    def test_round_trip_json(self):
        event = _make_event()
        json_str = event.model_dump_json()
        restored = SyncRunEvent.model_validate_json(json_str)
        assert restored.config_name == event.config_name
        assert restored.timestamp == event.timestamp
        assert len(restored.repos) == len(event.repos)

    def test_dry_run_default_false(self):
        event = _make_event()
        assert event.dry_run is False


class TestEmitAndRead:
    def test_emit_creates_file(self, tmp_path):
        events_file = tmp_path / 'events.jsonl'
        event = _make_event()
        emit_event(event, events_file=events_file)
        assert events_file.exists()

    def test_round_trip(self, tmp_path):
        events_file = tmp_path / 'events.jsonl'
        event = _make_event(config_name='test')
        emit_event(event, events_file=events_file)
        events = read_events(events_file=events_file)
        assert len(events) == 1
        assert events[0].config_name == 'test'

    def test_multiple_events_append(self, tmp_path):
        events_file = tmp_path / 'events.jsonl'
        for i in range(3):
            emit_event(_make_event(config_name=f'run{i}'), events_file=events_file)
        events = read_events(events_file=events_file)
        assert len(events) == 3
        assert events[2].config_name == 'run2'

    def test_read_missing_file(self, tmp_path):
        events = read_events(events_file=tmp_path / 'nonexistent.jsonl')
        assert events == []

    def test_emit_creates_parent_dirs(self, tmp_path):
        events_file = tmp_path / 'deep' / 'nested' / 'events.jsonl'
        emit_event(_make_event(), events_file=events_file)
        assert events_file.exists()

    def test_preserves_repo_data(self, tmp_path):
        events_file = tmp_path / 'events.jsonl'
        snap = _make_snapshot(name='myrepo', status='issues', uncommitted=5, branch='main', stashes=2)
        emit_event(_make_event(repos=[snap], issues=1, synced=0), events_file=events_file)
        events = read_events(events_file=events_file)
        restored_snap = events[0].repos[0]
        assert restored_snap.name == 'myrepo'
        assert restored_snap.uncommitted == 5
        assert restored_snap.stashes == 2
        assert restored_snap.branch == 'main'


class TestPerRegistryEventStreams:
    """Two registries are two working sets. Sharing one stream makes `stats` a blend of both,
    and find_stale_repos() scopes to the paths in the most recent run — so alternating a
    personal and a work run would make each set's dirty-repo warnings vanish on the other's."""

    def test_stream_is_keyed_on_the_registry_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr('syncer.tracking.STATE_DIR', tmp_path)
        personal = events_file_for(Path('~/dev/repos.json'))
        work = events_file_for(Path('~/dev/work-repos.json'))
        assert personal != work
        assert personal.name == 'repos-events.jsonl'
        assert work.name == 'work-repos-events.jsonl'

    def test_separate_registries_do_not_share_history(self, tmp_path):
        personal = tmp_path / 'repos-events.jsonl'
        work = tmp_path / 'work-repos-events.jsonl'
        emit_event(_make_event(config_name='personal'), personal)
        emit_event(_make_event(config_name='work'), work)
        assert [event.config_name for event in read_events(personal)] == ['personal']
        assert [event.config_name for event in read_events(work)] == ['work']


class TestLegacyStateMigration:
    """Run history is machine-local, so it exists separately on every box that ever ran syncer.
    Both shapes have to land: a machine that skipped the per-registry split still has the single
    global stream sitting in the old data dir."""

    @pytest.fixture
    def dirs(self, tmp_path, monkeypatch):
        data_dir = tmp_path / 'share' / 'syncer'
        state_dir = tmp_path / 'state' / 'syncer'
        monkeypatch.setattr('syncer.tracking.LEGACY_DATA_DIR', data_dir)
        monkeypatch.setattr('syncer.tracking.LEGACY_EVENTS_FILE', data_dir / 'events.jsonl')
        monkeypatch.setattr('syncer.tracking.STATE_DIR', state_dir)
        return data_dir, state_dir

    def test_global_stream_is_adopted_by_the_default_registry(self, dirs):
        data_dir, state_dir = dirs
        emit_event(_make_event(config_name='history'), data_dir / 'events.jsonl')

        migrate_legacy_events(state_dir / 'repos-events.jsonl', adopt_global=True)
        assert not (data_dir / 'events.jsonl').exists()  # renamed, so no second registry claims it
        assert [event.config_name for event in read_events(state_dir / 'repos-events.jsonl')] == ['history']

    def test_global_stream_is_not_adopted_by_a_named_registry(self, dirs):
        data_dir, state_dir = dirs
        emit_event(_make_event(config_name='history'), data_dir / 'events.jsonl')

        migrate_legacy_events(state_dir / 'work-repos-events.jsonl', adopt_global=False)
        assert read_events(state_dir / 'work-repos-events.jsonl') == []
        assert (data_dir / 'events.jsonl').exists()

    def test_split_streams_move_under_their_own_names(self, dirs):
        data_dir, state_dir = dirs
        emit_event(_make_event(config_name='personal'), data_dir / 'repos-events.jsonl')
        emit_event(_make_event(config_name='work'), data_dir / 'work-repos-events.jsonl')

        migrate_legacy_events(state_dir / 'repos-events.jsonl', adopt_global=True)
        assert [event.config_name for event in read_events(state_dir / 'repos-events.jsonl')] == ['personal']
        assert [event.config_name for event in read_events(state_dir / 'work-repos-events.jsonl')] == ['work']

    def test_never_overwrites_an_existing_stream(self, dirs):
        data_dir, state_dir = dirs
        emit_event(_make_event(config_name='legacy'), data_dir / 'repos-events.jsonl')
        emit_event(_make_event(config_name='legacy-global'), data_dir / 'events.jsonl')
        emit_event(_make_event(config_name='current'), state_dir / 'repos-events.jsonl')

        migrate_legacy_events(state_dir / 'repos-events.jsonl', adopt_global=True)
        assert [event.config_name for event in read_events(state_dir / 'repos-events.jsonl')] == ['current']
        assert (data_dir / 'repos-events.jsonl').exists()
        assert (data_dir / 'events.jsonl').exists()

    def test_runs_exactly_once(self, dirs):
        data_dir, state_dir = dirs
        emit_event(_make_event(config_name='history'), data_dir / 'events.jsonl')
        target = state_dir / 'repos-events.jsonl'

        migrate_legacy_events(target, adopt_global=True)
        emit_event(_make_event(config_name='after-migration'), target)
        migrate_legacy_events(target, adopt_global=True)

        assert [event.config_name for event in read_events(target)] == ['history', 'after-migration']

    def test_empty_data_dir_is_removed_so_the_migration_is_observably_done(self, dirs):
        data_dir, state_dir = dirs
        emit_event(_make_event(), data_dir / 'events.jsonl')
        migrate_legacy_events(state_dir / 'repos-events.jsonl', adopt_global=True)
        assert not data_dir.exists()

    def test_no_op_on_a_fresh_install(self, dirs):
        _, state_dir = dirs
        migrate_legacy_events(state_dir / 'repos-events.jsonl', adopt_global=True)
        assert not (state_dir / 'repos-events.jsonl').exists()


class TestFindStaleRepos:
    def test_no_events(self):
        assert find_stale_repos([]) == []

    def test_no_stale_when_clean(self):
        events = [_make_event(repos=[_make_snapshot()])]
        assert find_stale_repos(events) == []

    def test_dirty_but_not_stale(self):
        """Dirty for less than threshold days is not stale."""
        event = _make_event(
            repos=[_make_snapshot(uncommitted=3, status='issues')],
            timestamp=datetime.now(UTC) - timedelta(days=1),
        )
        assert find_stale_repos([event], threshold_days=3) == []

    def test_stale_detected(self):
        """Dirty across runs for > threshold days is stale."""
        old = datetime.now(UTC) - timedelta(days=5)
        recent = datetime.now(UTC) - timedelta(hours=1)
        snap = _make_snapshot(name='dirty', uncommitted=3, status='issues')
        events = [
            _make_event(repos=[snap], timestamp=old),
            _make_event(repos=[snap], timestamp=recent),
        ]
        stale = find_stale_repos(events, threshold_days=3)
        assert len(stale) == 1
        assert stale[0][0] == '~/code/dirty'
        assert stale[0][1] >= 5

    def test_clean_run_resets_staleness(self):
        """If a repo becomes clean in a later run, it's no longer stale."""
        day1 = datetime.now(UTC) - timedelta(days=10)
        day5 = datetime.now(UTC) - timedelta(days=5)
        day6 = datetime.now(UTC) - timedelta(days=4)
        dirty = _make_snapshot(name='repo', uncommitted=2, status='issues')
        clean = _make_snapshot(name='repo')
        events = [
            _make_event(repos=[dirty], timestamp=day1),
            _make_event(repos=[clean], timestamp=day5),
            _make_event(repos=[dirty], timestamp=day6),
        ]
        # Dirty since day6 (4 days ago), threshold is 5
        stale = find_stale_repos(events, threshold_days=5)
        assert len(stale) == 0

    def test_multiple_stale_sorted_by_days(self):
        old = datetime.now(UTC) - timedelta(days=10)
        newer = datetime.now(UTC) - timedelta(days=4)
        snap1 = _make_snapshot(name='older', uncommitted=1, status='issues')
        snap2 = _make_snapshot(name='newer', uncommitted=2, status='issues')
        events = [
            _make_event(repos=[snap1], timestamp=old),
            _make_event(repos=[snap1, snap2], timestamp=newer),
        ]
        stale = find_stale_repos(events, threshold_days=3)
        assert len(stale) == 2
        # older should be first (more days stale)
        assert stale[0][0] == '~/code/older'
        assert stale[1][0] == '~/code/newer'

    def test_renamed_repo_path_not_stale(self):
        """A repo path that was dirty but no longer appears in the latest event is ignored."""
        old = datetime.now(UTC) - timedelta(days=10)
        recent = datetime.now(UTC) - timedelta(hours=1)
        old_snap = RepoSnapshot(name='myrepo', path='~/code/old-dir/myrepo', status='issues', uncommitted=1)
        new_snap = RepoSnapshot(name='myrepo', path='~/code/new-dir/myrepo', status='synced', uncommitted=0)
        events = [
            _make_event(repos=[old_snap], timestamp=old),
            _make_event(repos=[new_snap], timestamp=recent),
        ]
        stale = find_stale_repos(events, threshold_days=3)
        assert stale == []
