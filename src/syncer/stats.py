from __future__ import annotations

from datetime import UTC
from datetime import datetime
from operator import itemgetter
from pathlib import Path

from rich.console import Console
from rich.table import Table

from syncer.config import SyncerConfig
from syncer.repos import Repo
from syncer.tracking import SyncRunEvent
from syncer.tracking import find_stale_repos
from syncer.tracking import read_events

console = Console(highlight=False)

BLOCK_FULL = '\u2588'


def _time_ago(dt: datetime) -> str:
    now = datetime.now(UTC)
    delta = now - dt
    minutes = int(delta.total_seconds() / 60)
    if minutes < 1:
        return 'just now'
    if minutes < 60:
        return f'{minutes}m ago'
    hours = minutes // 60
    if hours < 24:
        return f'{hours}h ago'
    days = hours // 24
    if days < 30:
        return f'{days}d ago'
    months = days // 30
    if months < 12:
        return f'{months}mo ago'
    years = days // 365
    return f'{years}y ago'


def _format_duration(days: int) -> str:
    if days >= 365:
        years = days // 365
        months = (days % 365) // 30
        return f'{years}y {months}mo' if months else f'{years}y'
    if days >= 30:
        months = days // 30
        return f'{months}mo'
    return f'{days}d'


def _show_summary(events: list[SyncRunEvent]) -> None:
    console.print('  [bold]Summary (last 30 days)[/bold]')
    console.print('  ' + '\u2500' * 42)

    now = datetime.now(UTC)
    recent = [e for e in events if (now - e.timestamp).days <= 30]

    if not recent:
        console.print('  No runs in the last 30 days.')
        return

    total_runs = len(recent)
    last_run = max(recent, key=lambda e: e.timestamp)
    avg_issues = sum(e.summary.issues for e in recent) / total_runs

    console.print(f'  Total runs:     {total_runs}')
    console.print(f'  Last run:       {_time_ago(last_run.timestamp)}')
    console.print(f'  Avg issues:     {avg_issues:.1f} per run')


def _show_commits_graph(config: SyncerConfig) -> None:
    rows: list[tuple[str, int]] = []
    for repo_config in config.repos:
        path = Path(repo_config.path).expanduser()
        if not path.exists() or not (path / '.git').is_dir():
            continue
        repo = Repo(name=repo_config.name, path=path, owner=config.owner, host=config.host)
        try:
            is_fork = repo.is_fork
        except Exception:
            is_fork = False
        if is_fork:
            continue
        commits = repo.total_commits
        if commits > 0:
            label = repo_config.path if repo_config.path.startswith('~') else repo_config.name
            rows.append((label, commits))

    if not rows:
        return

    console.print()
    console.print('  [bold]Commits by Repo[/bold]')
    console.print('  ' + '\u2500' * 42)

    max_commits = max(c for _, c in rows)
    label_width = max(len(label) for label, _ in rows)
    max_bar = 30
    for label, commits in sorted(rows, key=itemgetter(1), reverse=True):
        bar_len = max(1, round(commits / max_commits * max_bar))
        bar = BLOCK_FULL * bar_len
        console.print(f'  {label:<{label_width}s} {bar}  {commits}')


def _show_repo_age(config: SyncerConfig) -> None:
    rows: list[tuple[str, int]] = []
    for repo_config in config.repos:
        path = Path(repo_config.path).expanduser()
        if not path.exists() or not (path / '.git').is_dir():
            continue
        repo = Repo(name=repo_config.name, path=path, owner=config.owner, host=config.host)
        try:
            is_fork = repo.is_fork
        except Exception:
            is_fork = False
        if is_fork:
            continue
        first = repo.first_commit_date
        if not first:
            continue
        try:
            dt = datetime.fromisoformat(first)
        except ValueError:
            continue
        age_days = (datetime.now(UTC) - dt).days
        if age_days >= 0:
            label = repo_config.path if repo_config.path.startswith('~') else repo_config.name
            rows.append((label, age_days))

    if not rows:
        return

    console.print()
    console.print('  [bold]Repo Age[/bold]')
    console.print('  ' + '\u2500' * 42)

    max_age = max(d for _, d in rows)
    label_width = max(len(label) for label, _ in rows)
    max_bar = 30
    for label, age_days in sorted(rows, key=itemgetter(1), reverse=True):
        bar_len = max(1, round(age_days / max_age * max_bar)) if max_age > 0 else 1
        bar = BLOCK_FULL * bar_len
        console.print(f'  {label:<{label_width}s} {bar}  {_format_duration(age_days)}')


def _show_frequently_dirty(events: list[SyncRunEvent]) -> None:
    now = datetime.now(UTC)
    recent = [e for e in events if (now - e.timestamp).days <= 30]
    if not recent:
        return

    total_runs = len(recent)
    dirty_counts: dict[str, int] = {}
    for event in recent:
        for snap in event.repos:
            if snap.uncommitted > 0 or snap.unpushed > 0:
                dirty_counts[snap.path] = dirty_counts.get(snap.path, 0) + 1

    # Only show repos dirty in >25% of runs
    threshold = max(2, total_runs // 4)
    frequent = [(path, count) for path, count in dirty_counts.items() if count >= threshold]
    if not frequent:
        return

    frequent.sort(key=itemgetter(1), reverse=True)

    console.print()
    console.print('  [bold]Frequently Dirty Repos[/bold]')
    console.print('  ' + '\u2500' * 42)

    label_width = max(len(path) for path, _ in frequent)
    max_bar = 13
    for path, count in frequent:
        pct = count / total_runs
        bar_len = max(1, round(pct * max_bar))
        bar = BLOCK_FULL * bar_len
        console.print(f'  {path:<{label_width}s} {bar}  {count} of {total_runs} runs ({pct:.0%})')


def _show_stale(events: list[SyncRunEvent]) -> None:
    stale = find_stale_repos(events)
    if not stale:
        return

    console.print()
    console.print('  [bold]Stale Repos (uncommitted > 3 days)[/bold]')
    console.print('  ' + '\u2500' * 42)
    for path, days in stale:
        console.print(f'  [yellow]\u26a0 {path:<30s} {days} days[/yellow]')


def _show_all_repos(config: SyncerConfig, events: list[SyncRunEvent]) -> None:
    # Build latest status from most recent event
    latest_status: dict[str, str] = {}
    if events:
        last_event = max(events, key=lambda e: e.timestamp)
        for snap in last_event.repos:
            if snap.status == 'synced':
                latest_status[snap.name] = 'synced'
            elif snap.uncommitted > 0:
                latest_status[snap.name] = f'{snap.uncommitted} uncommitted'
            elif snap.unpushed > 0:
                latest_status[snap.name] = f'{snap.unpushed} unpushed'
            elif snap.behind > 0:
                latest_status[snap.name] = f'{snap.behind} behind'
            else:
                latest_status[snap.name] = snap.status

    console.print()
    console.print('  [bold]All Repos[/bold]')
    console.print('  ' + '\u2500' * 62)

    table = Table(show_header=True, show_edge=False, pad_edge=False, padding=(0, 2))
    table.add_column('Path', style='white', min_width=30)
    table.add_column('Commits', justify='right')
    table.add_column('Last Active', justify='right')
    table.add_column('Status')

    rows: list[tuple[str, str, str, str, datetime | None]] = []
    for repo_config in config.repos:
        path = Path(repo_config.path).expanduser()
        label = repo_config.path if repo_config.path.startswith('~') else repo_config.name
        last_dt: datetime | None = None

        if path.exists() and (path / '.git').is_dir():
            repo = Repo(name=repo_config.name, path=path, owner=config.owner, host=config.host)
            commits = str(repo.total_commits)
            last_date = repo.last_commit_date
            if last_date:
                try:
                    last_dt = datetime.fromisoformat(last_date)
                    last_active = _time_ago(last_dt)
                except ValueError:
                    last_active = last_date
            else:
                last_active = '-'
        else:
            commits = '-'
            last_active = '-'

        status = latest_status.get(repo_config.name, '-')
        status_style = 'green' if status == 'synced' else 'yellow' if status != '-' else 'dim'
        rows.append((label, commits, last_active, f'[{status_style}]{status}[/{status_style}]', last_dt))

    rows.sort(key=lambda r: r[4] or datetime.min.replace(tzinfo=UTC), reverse=True)
    for label, commits, last_active, status_text, _ in rows:
        table.add_row(label, commits, last_active, status_text)

    console.print(table)


def _show_recent_runs(events: list[SyncRunEvent]) -> None:
    if not events:
        return

    console.print()
    console.print('  [bold]Recent Runs[/bold]')
    console.print('  ' + '\u2500' * 62)

    sorted_events = sorted(events, key=lambda e: e.timestamp, reverse=True)

    for event in sorted_events[:10]:
        date_str = event.timestamp.strftime('%b %d  %H:%M')
        parts = []
        if event.summary.synced:
            parts.append(f'{event.summary.synced} synced')
        if event.summary.pulled:
            parts.append(f'{event.summary.pulled} pulled')
        if event.summary.pushed:
            parts.append(f'{event.summary.pushed} pushed')
        if event.summary.issues:
            parts.append(f'{event.summary.issues} issues')
        summary = ', '.join(parts)
        console.print(f'  {date_str}   {summary}')


def show_stats(config: SyncerConfig) -> None:
    events = read_events()

    console.print()
    console.print('[bold]Syncer Stats[/bold]')
    console.print('\u2550' * 64)
    console.print()

    _show_summary(events)
    _show_commits_graph(config)
    _show_repo_age(config)
    _show_frequently_dirty(events)
    _show_stale(events)
    _show_all_repos(config, events)
    _show_recent_runs(events)
    console.print()
