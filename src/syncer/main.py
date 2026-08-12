from __future__ import annotations

import importlib.metadata
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Annotated

import typer
from pyselfupdate import Config
from pyselfupdate import notify
from pyselfupdate.typercmd import run_update

from syncer.commands.config_cmd import config_app
from syncer.commands.policy_cmd import policy_app
from syncer.config import RepoConfig
from syncer.config import SyncerConfig
from syncer.config import load_tool_config
from syncer.config import resolve_clone_url
from syncer.config import resolve_config
from syncer.config import resolve_registry
from syncer.doctor import doctor_exit_code
from syncer.doctor import render_doctor
from syncer.doctor import run_doctor
from syncer.output import ICON_MOVE
from syncer.output import ICON_WARN
from syncer.output import _status_line
from syncer.output import console
from syncer.output import error
from syncer.report import DEFAULT_JOBS
from syncer.report import exit_code_for
from syncer.report import report_branches
from syncer.repos import Repo
from syncer.repos import find_repo_in_search_paths
from syncer.repos import find_untracked_repos
from syncer.repos import origin_mismatch
from syncer.stats import show_stats
from syncer.sync import run_sync
from syncer.tracking import events_file_for
from syncer.tracking import migrate_legacy_events

app = typer.Typer(no_args_is_help=True, rich_markup_mode='rich')
app.add_typer(config_app, name='config', rich_help_panel='Manage')
app.add_typer(policy_app, name='policy', rich_help_panel='Inspect')

# Shared by the `update` command and the daily check in the callback below, so
# the notice cannot name a release the update command would not install.
UPDATE_CONFIG = Config(tool='syncer', owner='datapointchris')

_EPILOG = (
    '[bold]Examples[/bold]\n\n'
    '[cyan]syncer check[/cyan] — report the repos that need something; never writes\n\n'
    '[cyan]syncer check -v[/cyan] — the same run, listing every repo including the synced ones\n\n'
    "[cyan]syncer apply[/cyan] — pull, push, fast-forward, and clone what's safe\n\n"
    '[cyan]syncer apply -p mirror[/cyan] — run the aggressive mirror policy this once\n\n'
    '[cyan]syncer check --per-branch[/cyan] — quick per-branch view, no lifecycle or history\n\n'
    '[cyan]syncer issues[/cyan] — find moved, missing, or untracked repos\n\n'
    '[cyan]syncer config init[/cyan] — start from scratch on a new machine'
)


def _events_file(repos_path: Path, override: Path | None) -> Path:
    """Event stream for the registry in play. The pre-split global stream is adopted only when
    no --repos-file was given, since it is the default registry's history and nobody else's."""
    events_file = events_file_for(repos_path)
    migrate_legacy_events(events_file, adopt_global=override is None)
    return events_file


def _version_callback(value: bool) -> None:
    if value:
        console.print(f'syncer {importlib.metadata.version("syncer")}')
        raise typer.Exit()


@app.callback(epilog=_EPILOG)
def main(
    ctx: typer.Context,
    version: Annotated[
        bool, typer.Option('--version', '-V', callback=_version_callback, is_eager=True, help='Show the installed version and exit')
    ] = False,
) -> None:
    """Check whether local git repos are synced, across every branch.

    Two verbs over the same run: [bold]check[/bold] reports what each policy would do and never
    writes, [bold]apply[/bold] executes it. Both take [bold]--per-branch[/bold] to swap the
    repo-level view for a per-branch one.
    """
    # Never raises and never prints an error; the notice is deferred to exit so
    # it lands after the command's own output. `syncer update` is the only place
    # an update failure is reported. Skipped for `update` itself, which is about
    # to do the thing the notice would suggest.
    if ctx.invoked_subcommand != 'update':
        notify(UPDATE_CONFIG)


PerBranch = Annotated[bool, typer.Option('--per-branch', help='Per-branch view: no lifecycle, cloning, or run history')]
Policy = Annotated[str | None, typer.Option('--policy', '-p', help='Override the resolved policy for every repo')]
Jobs = Annotated[int, typer.Option('--jobs', '-j', help='Max repos to process concurrently')]
ReposFile = Annotated[
    Path | None, typer.Option('--repos-file', '-c', help='Use a different repo registry; replaces the default set entirely')
]
JsonOutput = Annotated[bool, typer.Option('--json', help='Emit the run as JSON on stdout instead of a report')]
# -v, not --all. The flag changes what is *printed*, never what is done: every repo is fetched,
# classified and decided either way, and the set the run operates on is identical with and without
# it. `-a` widens that set (`ls -a`, `git branch -a`, `docker ps -a`), so spelling this one `--all`
# would imply the default checked a subset of the registry — a false claim about the single thing
# syncer exists to be trusted about.
Verbose = Annotated[bool, typer.Option('--verbose', '-v', help='Show every repo, including the ones with nothing to report')]

# Exit code for a run the user interrupted, matching the shell's convention for SIGINT.
INTERRUPTED_EXIT_CODE = 130


def _run(*, apply: bool, per_branch: bool, policy: str | None, jobs: int, repos_file: Path | None, as_json: bool, verbose: bool) -> None:
    """Both verbs are the same run; only whether it writes and how it is grouped differ."""
    try:
        if per_branch:
            reports = report_branches(
                resolve_config(repos_file),
                load_tool_config(),
                cli_policy=policy,
                apply=apply,
                jobs=jobs,
                as_json=as_json,
                verbose=verbose,
            )
        else:
            syncer_config, repos_path = resolve_registry(repos_file)
            reports = run_sync(
                syncer_config,
                load_tool_config(),
                _events_file(repos_path, repos_file),
                cli_policy=policy,
                apply=apply,
                jobs=jobs,
                as_json=as_json,
                verbose=verbose,
            )
    except KeyboardInterrupt:
        # Nothing is rendered and no event is written. A run that covered some unknown fraction of
        # the registry is not a measurement, and `stats` would read one back as if it were.
        error('interrupted — nothing was reported and no run was recorded')
        raise typer.Exit(INTERRUPTED_EXIT_CODE) from None
    raise typer.Exit(exit_code_for(reports))


@app.command(rich_help_panel='Sync')
def check(
    per_branch: PerBranch = False,
    policy: Policy = None,
    jobs: Jobs = DEFAULT_JOBS,
    repos_file: ReposFile = None,
    json_output: JsonOutput = False,
    verbose: Verbose = False,
) -> None:
    """Report what each policy would do to every repo. Never writes.

    Only the repos with something to report are shown; the summary line counts the rest, and
    [bold]-v[/bold] lists them. Repos are checked concurrently and shown least-to-most urgent, so
    anything needing attention sits nearest the prompt. --per-branch swaps the repo-level view
    (which also clones missing repos under `apply`, and records run history) for a per-branch one.

    Exits 1 if any repo reached an error state, so it can gate a script.
    """
    _run(apply=False, per_branch=per_branch, policy=policy, jobs=jobs, repos_file=repos_file, as_json=json_output, verbose=verbose)


@app.command(rich_help_panel='Sync')
def apply(
    per_branch: PerBranch = False,
    policy: Policy = None,
    jobs: Jobs = DEFAULT_JOBS,
    repos_file: ReposFile = None,
    json_output: JsonOutput = False,
    verbose: Verbose = False,
) -> None:
    """Execute each policy's safe actions: pull, push, fast-forward, clone, prune.

    Enforces the hard safety invariants — never force, never touch a dirty tree or one whose
    cleanliness cannot be verified, refuse rather than force. `syncer check` is this command's
    dry run: same classification and the same decided actions, without the writes.

    Only the repos with something to report are shown; [bold]-v[/bold] lists every one.

    Exits 1 if any repo reached an error state, so it can gate a script.
    """
    _run(apply=True, per_branch=per_branch, policy=policy, jobs=jobs, repos_file=repos_file, as_json=json_output, verbose=verbose)


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
        repo = Repo(
            name=repo_config.name,
            path=path,
            owner=owner,
            host=syncer_config.host,
            timeout=tool_config.git_timeout,
            url=resolve_clone_url(repo_config, syncer_config),
        )

        # A clone pointing somewhere the registry never named is silent drift: `gh repo clone
        # <bare-name>` resolves to the authenticated user, so a reference repo that also exists
        # under your own account gets your fork as origin and pulls from it forever. Report
        # only — a deliberate fork is indistinguishable from a mistake without asking.
        actual_origin = origin_mismatch(repo)
        if actual_origin:
            console.print(_status_line(ICON_WARN, label, 'origin mismatch', 'yellow'))
            console.print(f'    origin is {actual_origin}')
            console.print(f'    registry expects {repo.url} (fix the remote or repos.json manually)')
            issues_found += 1
            console.print()

        # Only nag about master where the naming is ours to change. A third-party clone's
        # default branch is upstream's decision and a work repo's is the org's — either way
        # the check would report forever with nothing to do about any of them.
        is_ours = bool(syncer_config.owner) and owner == syncer_config.owner
        if syncer_config.owns_branch_naming and is_ours and repo.default_branch == 'master':
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
        # Names what was actually checked. 'All repos healthy.' claimed a verdict this command
        # never measures — it reads the registry against the filesystem and never looks at sync
        # state, so it printed a clean bill of health for repos `check` was calling untidy.
        console.print('[blue] Registry matches the filesystem. Run [cyan]syncer check[/cyan] for sync state.[/blue]')
        return
    console.print(f'[yellow] {issues_found} issue(s) found.[/yellow]')
    # Non-zero, because printing a count and exiting 0 is the exact shape of a check nothing can
    # be scripted against — the caller has to scrape the text to learn what the exit code
    # already should have said.
    raise typer.Exit(1)


@app.command(rich_help_panel='Inspect')
def stats(
    repos_file: Annotated[
        Path | None,
        typer.Option('--repos-file', '-c', help='Use a different repo registry; replaces the default set entirely'),
    ] = None,
) -> None:
    """Show run history and repo insights: sync summary, commits, age, dirty and stale repos.

    History is per registry, so this reports on the same working set the sync run used.
    """
    syncer_config, repos_path = resolve_registry(repos_file)
    show_stats(syncer_config, _events_file(repos_path, repos_file))


@app.command(rich_help_panel='Inspect')
def doctor(
    repos_file: Annotated[
        Path | None,
        typer.Option('--repos-file', '-c', help='Check a different repo registry'),
    ] = None,
) -> None:
    """Check whether this machine can run syncer at all, and say which part is wrong.

    Completes the trio: [bold]config validate[/bold] checks structure, [bold]issues[/bold] checks
    reality (do the registry's paths exist), and this checks the machine — git, the resolved
    config and registry paths with the reason each was chosen, whether the remotes can actually
    be reached, and how many repos are cloned. Read-only, and never writes a config.

    Exits 1 if any check fails, so [bold]syncer doctor && syncer --apply[/bold] stops on a box
    that was never going to work.
    """
    checks = run_doctor(repos_file)
    render_doctor(checks)
    raise typer.Exit(doctor_exit_code(checks))


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
        # Demo history is throwaway; it must never land in a real registry's event stream.
        run_sync(config, load_tool_config(), tmp / 'events.jsonl', apply=True, jitter=0.0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    app()
