import logging
import subprocess
import sys
from dataclasses import dataclass
from typing import Annotated

import typer

from syncer.repos import Repo

logger = logging.getLogger(__name__)

app = typer.Typer()


@dataclass
class Command:
    message: str
    command: str


@app.callback(invoke_without_command=True)
@app.command()
def main(
    version: Annotated[str, typer.Argument()],
    title: Annotated[str, typer.Argument()],
    dry_run: Annotated[bool, typer.Option()] = False,
):
    syncer = Repo(group='', name='syncer', logger=logger)

    if dry_run:
        syncer.warning('=' * 35 + '|  DRY RUN  |' + '=' * 35)

    syncer.okay(f"creating release: '{version} - {title}'")
    syncer.info('running checks...')
    errors = False

    if syncer.has_uncommitted_changes:
        syncer.error('abort: uncommitted local changes')
        errors = True
        with syncer.chdir():
            subprocess.call('git status --short'.split())
        if not dry_run:
            sys.exit(1)

    if syncer.has_main_branch:
        if syncer.has_unpushed_main_commits:
            errors = True
            syncer.error('abort: unpushed local changes on main branch')
            with syncer.chdir():
                subprocess.call('git log --oneline origin/main..main'.split())
            if not dry_run:
                sys.exit(1)

    if syncer.has_master_branch:
        if syncer.has_unpushed_master_commits:
            errors = True
            syncer.error('abort: unpushed local changes on master branch')
            with syncer.chdir():
                subprocess.call('git log --oneline origin/master..master'.split())
            if not dry_run:
                sys.exit(1)

    if syncer.can_update:
        syncer.error('abort: repo has upstream changes')
        errors = True
        if not dry_run:
            sys.exit(1)

    if errors:
        syncer.error('checks failed for creating release')
    else:
        syncer.okay('checks passed for creating release')

    COMMANDS = [
        Command(f'updating project version to {version}', f'poetry version {version}'),
        Command('locking dependencies', 'poetry lock'),
        Command('creating commit', f"git commit -am 'build: create release {version} - {title}'"),
        Command('pushing to git', 'git push'),
        Command(f'creating git tag for version {version}', f"git tag {version} -m '{title}'"),
        Command('pushing tag to origin', f'git push --tags origin refs/tags/{version}'),
        Command('creating release on github', f"gh release create {version} -t '{title}' --notes-from-tag"),
    ]

    with syncer.chdir():
        for cmd in COMMANDS:
            syncer.info(cmd.message)
            if not dry_run:
                subprocess.call(cmd.command, shell=True)

    syncer.okay(f"release created successfully: '{version} - {title}'")

    if dry_run:
        syncer.warning('=' * 35 + '|  END DRY RUN  |' + '=' * 35)
