from __future__ import annotations

import importlib.metadata
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Annotated

import typer
from pyselfupdate import Config
from pyselfupdate import notify
from pyselfupdate.typercmd import run_update
from rich.console import Console

from syncer.config import TOOL_CONFIG_PATH
from syncer.config import RepoConfig
from syncer.config import SyncerConfig
from syncer.config import load_tool_config
from syncer.config import resolve_config
from syncer.report import DEFAULT_JOBS
from syncer.report import report_branches
from syncer.repos import ICON_MOVE
from syncer.repos import ICON_WARN
from syncer.repos import Repo
from syncer.repos import _status_line
from syncer.repos import find_repo_in_search_paths
from syncer.stats import show_stats
from syncer.sync import run_sync

app = typer.Typer(invoke_without_command=True, rich_markup_mode='rich')
console = Console()

# Shared by the `update` command and the daily check in the callback below, so
# the notice cannot name a release the update command would not install.
UPDATE_CONFIG = Config(tool='syncer', owner='datapointchris')

_EPILOG = (
    '[bold]Examples[/bold]\n\n'
    '[cyan]syncer[/cyan] — report every repo across all branches (read-only)\n\n'
    "[cyan]syncer --apply[/cyan] — pull, push, fast-forward, and clone what's safe\n\n"
    '[cyan]syncer -p mirror --apply[/cyan] — run the aggressive mirror policy this once\n\n'
    '[cyan]syncer branches[/cyan] — quick per-branch view, no lifecycle or history\n\n'
    '[cyan]syncer issues[/cyan] — find moved, missing, or untracked repos'
)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f'syncer {importlib.metadata.version("syncer")}')
        raise typer.Exit()


@app.callback(epilog=_EPILOG)
def main(
    ctx: typer.Context,
    apply: Annotated[
        bool, typer.Option('--apply', help='Execute the decided actions (pull/push/ff/clone); default is report-only')
    ] = False,
    dry_run: Annotated[bool, typer.Option('--dry-run', '-n', help='Force report-only, even with --apply')] = False,
    policy: Annotated[str | None, typer.Option('--policy', '-p', help='Override the resolved policy for every repo')] = None,
    jobs: Annotated[int, typer.Option('--jobs', '-j', help='Max repos to process concurrently')] = DEFAULT_JOBS,
    repos_file: Annotated[
        Path | None,
        typer.Option('--repos-file', '-c', help='Use a different repo registry; replaces the default set entirely'),
    ] = None,
    version: Annotated[
        bool, typer.Option('--version', '-V', callback=_version_callback, is_eager=True, help='Show the installed version and exit')
    ] = False,
) -> None:
    """Check whether local git repos are synced, across every branch.

    Running [bold]syncer[/bold] with no command reports the state of every repo (read-only).
    Add [bold]--apply[/bold] to execute each policy's safe actions. Repos are checked concurrently
    and shown least-to-most urgent, so anything needing attention sits nearest the prompt.
    """
    ctx.ensure_object(dict)
    ctx.obj['dry_run'] = dry_run

    # Never raises and never prints an error; the notice is deferred to exit so
    # it lands after the command's own output. `syncer update` is the only place
    # an update failure is reported. Skipped for `update` itself, which is about
    # to do the thing the notice would suggest.
    if ctx.invoked_subcommand != 'update':
        notify(UPDATE_CONFIG)

    if ctx.invoked_subcommand is None:
        syncer_config = resolve_config(repos_file)
        tool_config = load_tool_config()
        run_sync(syncer_config, tool_config, cli_policy=policy, apply=apply and not dry_run, jobs=jobs)


@app.command(rich_help_panel='Sync')
def branches(
    policy: Annotated[str | None, typer.Option('--policy', '-p', help='Override the resolved policy for every repo')] = None,
    apply: Annotated[bool, typer.Option('--apply', help='Execute the decided action per branch (safe actions only)')] = False,
    jobs: Annotated[int, typer.Option('--jobs', '-j', help='Max repos to process concurrently')] = DEFAULT_JOBS,
    repos_file: Annotated[
        Path | None,
        typer.Option('--repos-file', '-c', help='Use a different repo registry; replaces the default set entirely'),
    ] = None,
) -> None:
    """Per-branch report only — like the default run without lifecycle, cloning, or history.

    Classifies every local branch (ahead/behind/gone/no-upstream/…) and shows the action the
    resolved policy would take. Read-only by default; --apply executes it, enforcing the hard
    safety invariants (never force, never touch a dirty tree, refuse rather than force).
    """
    syncer_config = resolve_config(repos_file)
    tool_config = load_tool_config()
    report_branches(syncer_config, tool_config, cli_policy=policy, apply=apply, jobs=jobs)


def find_untracked_repos(search_path: Path, known_paths: set[Path], excluded: set[Path] | None = None, max_depth: int = 3) -> list[Path]:
    """Find git repos under search_path that no registry entry claims.

    Recurses, because repos are routinely nested more than one level down
    (~/code/python-projects/, ~/code/sql/, ~/code/refs/). Scanning only direct
    children silently passed every one of those — which is exactly how twenty
    reference clones dropped out of tracking without a word.

    Matches on resolved path, not name: two repos can share a basename.
    Descent stops at a repo, so nested worktrees and vendored checkouts are not
    reported separately.
    """
    excluded = excluded or set()
    if max_depth <= 0 or not search_path.is_dir() or search_path.resolve() in excluded:
        return []

    found: list[Path] = []
    try:
        entries = sorted(search_path.iterdir())
    except PermissionError:
        return []

    for item in entries:
        if not item.is_dir() or item.is_symlink() or item.name.startswith('.'):
            continue
        if (item / '.git').exists():
            if item.resolve() not in known_paths:
                found.append(item)
            continue
        found.extend(find_untracked_repos(item, known_paths, excluded, max_depth - 1))
    return found


@app.command(rich_help_panel='Inspect')
def issues(
    repos_file: Annotated[
        Path | None,
        typer.Option('--repos-file', '-c', help='Use a different repo registry; replaces the default set entirely'),
    ] = None,
) -> None:
    """Flag repos that moved, went missing, aren't tracked, or still default to master.

    Compares repos.json against the filesystem and search_paths. Read-only — never writes
    repos.json; fix the reported paths yourself.
    """
    syncer_config = resolve_config(repos_file)
    tool_config = load_tool_config()
    search_paths = [Path(p).expanduser() for p in syncer_config.search_paths]
    claimed_paths = {Path(rc.path).expanduser() for rc in syncer_config.repos}

    issues_found = 0

    console.print()

    for repo_config in syncer_config.repos:
        path = Path(repo_config.path).expanduser()
        label = repo_config.path if repo_config.path.startswith('~') else repo_config.name

        if not path.exists():
            found = find_repo_in_search_paths(repo_config.name, search_paths, claimed_paths)
            if found:
                console.print(_status_line(ICON_MOVE, label, 'path mismatch', 'yellow'))
                console.print(f'    found at {found} (update repos.json manually)')
            else:
                console.print(_status_line(ICON_WARN, label, 'missing', 'yellow'))
            issues_found += 1
            console.print()
            continue

        if not (path / '.git').is_dir():
            continue

        owner = repo_config.owner or syncer_config.owner
        repo = Repo(name=repo_config.name, path=path, owner=owner, host=syncer_config.host, timeout=tool_config.git_timeout)

        # Only nag about master on repos we control. A third-party clone's default
        # branch is upstream's decision — every exemplar clone would report it
        # forever and there is nothing to do about any of them.
        is_ours = bool(syncer_config.owner) and owner == syncer_config.owner
        if is_ours and repo.default_branch == 'master':
            if repo.is_fork:
                console.print(_status_line(ICON_WARN, label, 'using master (fork)', 'yellow', branch='master'))
            else:
                console.print(_status_line(ICON_WARN, label, 'using master', 'yellow', branch='master'))
            issues_found += 1
            console.print()

    # Check for untracked repos in search paths
    known_paths = {Path(r.path).expanduser().resolve() for r in syncer_config.repos}
    excluded = {Path(p).expanduser().resolve() for p in syncer_config.exclude_paths}
    for search_path in search_paths:
        for found in find_untracked_repos(search_path, known_paths, excluded):
            item_path = str(found).replace(str(Path.home()), '~')
            console.print(_status_line(ICON_WARN, item_path, 'untracked (not in the registry)', 'yellow'))
            console.print()
            issues_found += 1

    console.print()
    if issues_found == 0:
        console.print('[blue] All repos healthy.[/blue]')
    else:
        console.print(f'[yellow] {issues_found} issue(s) found.[/yellow]')


@app.command(rich_help_panel='Inspect')
def stats(ctx: typer.Context) -> None:
    """Show run history and repo insights: sync summary, commits, age, dirty and stale repos."""
    syncer_config = resolve_config()
    show_stats(syncer_config)


@app.command(rich_help_panel='Manage')
def init() -> None:
    """Create the syncer tool config (~/.config/syncer/config.toml) if it doesn't exist."""
    if TOOL_CONFIG_PATH.exists():
        console.print(f'[red]Config already exists: {TOOL_CONFIG_PATH}[/red]')
        sys.exit(1)
    TOOL_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOOL_CONFIG_PATH.write_text('repos_file = "~/dev/repos.json"\n')
    console.print(f'[green]Created config: {TOOL_CONFIG_PATH}[/green]')
    console.print('[yellow]Edit repos_file to point to your repo registry.[/yellow]')


@app.command(rich_help_panel='Manage')
def version() -> None:
    """Print the installed version of syncer."""
    v = importlib.metadata.version('syncer')
    console.print(f'syncer {v}')


@app.command(rich_help_panel='Manage')
def update(
    check_only: Annotated[bool, typer.Option('--check', help='Report whether an update is available without installing it')] = False,
) -> None:
    """Update syncer to the latest GitHub release."""
    run_update(UPDATE_CONFIG, check_only=check_only)


def _git(path: Path, *args: str) -> None:
    subprocess.run(['git', *args], cwd=path, capture_output=True, text=True)  # nosec B607


def _setup_demo_repos(base: Path) -> SyncerConfig:
    """Create real git repos in various states for demo."""

    def make_repo(name: str) -> Path:
        bare = base / 'remotes' / f'{name}.git'
        bare.mkdir(parents=True)
        subprocess.run(['git', 'init', '--bare', str(bare)], capture_output=True)  # nosec B607
        repo = base / 'repos' / name
        subprocess.run(['git', 'clone', str(bare), str(repo)], capture_output=True)  # nosec B607
        _git(repo, 'config', 'user.email', 'demo@syncer')
        _git(repo, 'config', 'user.name', 'Demo')
        (repo / 'README.md').write_text(f'# {name}\n')
        _git(repo, 'add', '.')
        _git(repo, 'commit', '-m', 'init')
        _git(repo, 'push')
        return repo

    # 1. Synced — clean, up to date
    make_repo('synced-repo')

    # 2. Uncommitted changes
    repo = make_repo('uncommitted-repo')
    (repo / 'src').mkdir()
    (repo / 'src' / 'main.py').write_text('print("hello")\n')
    (repo / 'new_file.txt').write_text('untracked\n')
    _git(repo, 'add', 'src/main.py')

    # 3. Unpushed commits
    repo = make_repo('unpushed-repo')
    (repo / 'feature.py').write_text('def feature(): pass\n')
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-m', 'feat: add new feature')
    (repo / 'fix.py').write_text('def fix(): pass\n')
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-m', 'fix: resolve bug')

    # 4. Behind remote (push from a second clone)
    make_repo('behind-repo')
    second = base / 'second-clone'
    bare = base / 'remotes' / 'behind-repo.git'
    subprocess.run(['git', 'clone', str(bare), str(second)], capture_output=True)  # nosec B607
    _git(second, 'config', 'user.email', 'demo@syncer')
    _git(second, 'config', 'user.name', 'Demo')
    (second / 'update.txt').write_text('remote change\n')
    _git(second, 'add', '.')
    _git(second, 'commit', '-m', 'chore: update deps')
    _git(second, 'push')

    # 5. Stashes
    repo = make_repo('stashed-repo')
    (repo / 'wip.txt').write_text('work in progress\n')
    _git(repo, 'add', '.')
    _git(repo, 'stash', 'push', '-m', 'saving work')
    (repo / 'wip2.txt').write_text('more wip\n')
    _git(repo, 'add', '.')
    _git(repo, 'stash', 'push', '-m', 'saving more work')

    # 6. Not a git repo
    not_git = base / 'repos' / 'not-a-repo'
    not_git.mkdir(parents=True)
    (not_git / 'file.txt').write_text('just a directory\n')

    # 7. No remote
    no_remote = base / 'repos' / 'no-remote-repo'
    no_remote.mkdir(parents=True)
    subprocess.run(['git', 'init', str(no_remote)], capture_output=True)  # nosec B607
    _git(no_remote, 'config', 'user.email', 'demo@syncer')
    _git(no_remote, 'config', 'user.name', 'Demo')
    (no_remote / 'README.md').write_text('# no remote\n')
    _git(no_remote, 'add', '.')
    _git(no_remote, 'commit', '-m', 'init')

    # 8. Missing repo (not yet cloned)
    missing_path = base / 'repos' / 'missing-repo'
    # Don't create the directory — it simulates a repo that needs cloning

    repos_dir = base / 'repos'
    existing = [RepoConfig(name=d.name, path=str(d)) for d in sorted(repos_dir.iterdir()) if d.is_dir()]
    existing.append(RepoConfig(name='missing-repo', path=str(missing_path)))
    return SyncerConfig(
        owner='demo',
        host='https://github.com',
        search_paths=[],
        repos=existing,
    )


@app.command(rich_help_panel='Examples')
def demo() -> None:
    """Run a full sync against throwaway temp repos to show every status state."""
    tmp = Path(tempfile.mkdtemp(prefix='syncer-demo-'))
    try:
        config = _setup_demo_repos(tmp)
        run_sync(config, load_tool_config(), apply=True, jitter=0.0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    app()
