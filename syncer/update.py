import logging
import subprocess
import sys
from typing import Annotated

import typer
from colorama import Fore, Style

from syncer import utilities

logger = logging.getLogger(__name__)

app = typer.Typer()


@app.callback(invoke_without_command=True)
@app.command()
def main(
    dry_run: Annotated[bool, typer.Option()] = False,
    force: Annotated[bool, typer.Option()] = False,
):

    def print_and_log(message: str, color: str):
        log_dry_run = 'DRY RUN: ' if dry_run else ''
        print(color + message + Style.RESET_ALL)
        logger.info(log_dry_run + message.lower())

    if dry_run:
        print(Fore.YELLOW + '=' * 35 + '|  DRY RUN  |' + '=' * 35 + Style.RESET_ALL)

    current_version = utilities.get_installed_version()
    print_and_log(f"Current version: {current_version}", Fore.RESET)
    if not current_version:
        print_and_log('ABORT: could not retrieve syncer current version', Fore.RED)
        if not dry_run:
            sys.exit(1)

    github_version = utilities.get_latest_github_version()
    print_and_log(f"Latest version: {github_version}", Fore.RESET)

    if github_version > current_version:
        print_and_log(f'A new release of syncer is available: {current_version} → {github_version}', Fore.CYAN)
    else:
        print_and_log('No new release of syncer is available', Fore.YELLOW)
        if force:
            print_and_log('Forcing update...', Fore.RED)
        elif not dry_run:
            sys.exit(0)

    print_and_log("Updating...", Fore.BLUE)

    latest_release_url = f'git+https://www.github.com/datapointchris/syncer.git@{github_version}'

    # pipx upgrade does not seem to work, instead force install
    install_command = f"pipx install --force {latest_release_url}"
    logger.info(f"Install command: {install_command}")
    if not dry_run:
        subprocess.call(install_command, shell=True)
    print_and_log(f'Successfully updated to version {github_version}', Fore.GREEN)

    if dry_run:
        print()
        print(Fore.YELLOW + '=' * 30 + '|  END DRY RUN  |' + '=' * 30 + Style.RESET_ALL)
