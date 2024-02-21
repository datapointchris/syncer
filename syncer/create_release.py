import logging
import subprocess
import sys
from typing import Annotated

import typer
from colorama import Fore, Style

from syncer.config import settings
from syncer.repos import Repo

app = typer.Typer()

logger = logging.getLogger(__name__)


@app.callback(invoke_without_command=True)
@app.command()
def main(
    version: Annotated[str, typer.Argument()],
    title: Annotated[str, typer.Argument()],
    dry_run: Annotated[bool, typer.Option()] = False,
):
    print(Fore.BLUE + 'Creating Release...' + Style.RESET_ALL)

    log_dry_run = 'DRY RUN: ' if dry_run else ''
    logger.info(f'{log_dry_run}creating release {version} - {title}')

    if dry_run:
        print(Fore.YELLOW + '=' * 35 + '|  DRY RUN  |' + '=' * 35 + Style.RESET_ALL)

    syncer = Repo(settings.data.CODE_ROOT, '', 'https://www.github.com', 'datapointchris', 'syncer')

    error_msg = 'ABORT:'
    if syncer.has_uncommitted_changes:
        syncer.error(f'{error_msg} uncommitted local changes')
        logger.error(f'{log_dry_run}{error_msg} uncommitted local changes')
        with syncer.chdir():
            subprocess.call('git status --short'.split())
        if not dry_run:
            sys.exit(1)

    if syncer.has_main_branch:
        if syncer.has_unpushed_main_commits:
            syncer.error(f'{error_msg} unpushed local changes on main branch')
            logger.error(f'{log_dry_run}{error_msg} unpushed local changes on main branch')
            with syncer.chdir():
                subprocess.call('git log --oneline origin/main..main'.split())
            if not dry_run:
                sys.exit(1)

    if syncer.has_master_branch:
        if syncer.has_unpushed_master_commits:
            syncer.error(f'{error_msg} unpushed local changes on master branch')
            logger.error(f'{log_dry_run}{error_msg} unpushed local changes on master branch')
            with syncer.chdir():
                subprocess.call('git log --oneline origin/master..master'.split())
            if not dry_run:
                sys.exit(1)

    if syncer.can_update:
        syncer.error(f'{error_msg} repo has upstream changes')
        logger.error(f'{log_dry_run}{error_msg} repo has upstream changes')
        if not dry_run:
            sys.exit(1)

    syncer.okay('checkes passed for creating release')

    update_project_version = f'poetry version {version}'
    commit_version_bump = f"git commit -am 'build: create release {version} - {title}'"
    create_git_tag = f"git tag {version} -m '{title}'"
    push_tag_to_github = f'git push --tags origin refs/tags/{version}'
    create_release_on_github = f"gh release create {version} -t '{title}' --notes-from-tag"

    if not dry_run:
        with syncer.chdir():
            syncer.info(f'Updating project version to {version}')
            subprocess.call(update_project_version, shell=True)

            syncer.info('Pushing version bump to git')
            subprocess.call(commit_version_bump, shell=True)

            syncer.info(f'Creating git tag for version {version}')
            subprocess.call(create_git_tag, shell=True)

            syncer.info('Pushing tag to origin')
            subprocess.call(push_tag_to_github, shell=True)

            subprocess.call(create_release_on_github, shell=True)
            syncer.info(f'Created release on github for version {version} - {title}')
    else:
        syncer.info(f'Update project version: {update_project_version}')
        logger.info(f'{log_dry_run}update project version: {update_project_version}')

        syncer.info('Push version bump to git')
        logger.info(f'{log_dry_run}push version bump to git')

        syncer.info(f'Create git tag: {create_git_tag}')
        logger.info(f'{log_dry_run}create git tag: {create_git_tag}')

        syncer.info(f'Push tag: {push_tag_to_github}')
        logger.info(f'{log_dry_run}push tag: {push_tag_to_github}')

        syncer.info(f'Create Github release: {create_release_on_github}')
        logger.info(f'{log_dry_run}create github release: {create_release_on_github}')

    if dry_run:
        print()
        print(Fore.YELLOW + '=' * 30 + '|  END DRY RUN  |' + '=' * 30 + Style.RESET_ALL)
