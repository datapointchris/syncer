import enum
import json
import os
import pathlib
import subprocess
from contextlib import contextmanager
from typing import Annotated

import typer
from colorama import Fore, Style

from syncer.config import settings

app = typer.Typer()


class RepoType(enum.Enum):
    datapointchris = 'datapointchris'
    labs1904 = '1904labs'


class Repo:
    def __init__(self, code_root: pathlib.Path, group: str, host: str, owner: str, name: str):
        self.code_root = code_root
        self.group = group
        self.host = host
        self.owner = owner
        self.name = name
        self.full_path = self.code_root / self.group / self.name
        self.short_path = pathlib.Path(self.group) / self.name
        self.directory = self.code_root / self.group
        self.url = '/'.join([self.host, self.owner, self.name])

    @contextmanager
    def chdir(self):
        old_path = pathlib.Path.cwd()
        os.chdir(self.full_path)
        try:
            yield
        finally:
            os.chdir(old_path)

    def _print_message(self, msg: str, color: str):
        message = f'{color}{self.short_path} {"_" * (80 - len(str(self.short_path)) - len(msg))} {msg}{Style.RESET_ALL}'
        print()
        print(message)

    def okay(self, message: str):
        return self._print_message(message, color=Fore.GREEN)

    def info(self, message: str):
        return self._print_message(message, color=Fore.BLUE)

    def warning(self, message: str):
        return self._print_message(message, color=Fore.YELLOW)

    def error(self, message: str):
        return self._print_message(message, color=Fore.RED)

    @property
    def is_git_repo(self):
        with self.chdir():
            return False if subprocess.getoutput('git status --porcelain').startswith('fatal: not a git') else True

    @property
    def is_forked_repo(self):
        with self.chdir():
            return any('forked' in part for part in self.short_path.parts)

    @property
    def has_remote(self):
        with self.chdir():
            return subprocess.getoutput('git remote')

    @property
    def can_update(self):
        with self.chdir():
            return bool(subprocess.getoutput('git fetch'))

    @property
    def has_uncommitted_changes(self):
        with self.chdir():
            return subprocess.getoutput('git status --porcelain')

    @property
    def has_main_branch(self):
        with self.chdir():
            return False if subprocess.getoutput('git log origin/main..main').startswith('fatal') else True

    @property
    def has_unpushed_main_commits(self):
        with self.chdir():
            return subprocess.getoutput('git log origin/main..main')

    @property
    def has_master_branch(self):
        with self.chdir():
            return False if subprocess.getoutput('git log origin/master..master').startswith('fatal') else True

    @property
    def has_unpushed_master_commits(self):
        with self.chdir():
            return subprocess.getoutput('git log origin/master..master')


def load_repos(filename: pathlib.Path, code_root: pathlib.Path) -> list[Repo]:
    with filename.open() as file:
        return [Repo(**repo, code_root=code_root) for repo in json.load(file)]


def print_message(msg: str, repo: Repo, color: str):
    message = f'{color}{repo.short_path} {"_" * (80 - len(str(repo.short_path)) - len(msg))} {msg}{Style.RESET_ALL}'
    print()
    print(message)


def create_base_directories(repos: list[Repo], dry_run: bool = False):
    for repo in repos:
        if not repo.directory.exists():
            if not dry_run:
                repo.directory.mkdir(parents=True)
            print_message('created directory', repo, Fore.GREEN)


@app.callback(invoke_without_command=True)
@app.command()
def main(
    owner: Annotated[RepoType, typer.Argument()],
    dry_run: Annotated[bool, typer.Option()] = False,
    require_main_branch: Annotated[bool, typer.Option()] = False,
    code_root: Annotated[pathlib.Path, typer.Option()] = settings.data.CODE_ROOT,
):
    print(Fore.BLUE + 'Syncing Projects...' + Style.RESET_ALL)

    if dry_run:
        print(Fore.YELLOW + '=' * 35 + '|  DRY RUN  |' + '=' * 35 + Style.RESET_ALL)

    repos = load_repos(settings.data.REPOS_DIR / (owner.value + '.json'), code_root=code_root)

    create_base_directories(repos, dry_run=dry_run)

    for repo in repos:

        if not repo.full_path.exists():
            os.chdir(repo.directory)
            if not dry_run:
                subprocess.call(['git', 'clone', repo.url])
            repo.info('cloning')
            continue

        if not repo.is_git_repo:
            repo.error('not a git repository')
            continue

        if not repo.has_remote:
            repo.error('no remote')
            continue

        if not repo.has_main_branch and not repo.has_master_branch:
            repo.error('no main branch or master branch')
            continue

        if repo.has_main_branch and repo.has_master_branch:
            repo.error('both main and master branches exist')
            continue

        if repo.has_uncommitted_changes:
            repo.warning('uncommitted local changes')
            with repo.chdir():
                subprocess.call('git status --short'.split())

        if repo.has_main_branch:
            if repo.has_unpushed_main_commits:
                repo.warning('unpushed local changes on main branch')
                with repo.chdir():
                    subprocess.call('git log --oneline origin/main..main'.split())
                if not repo.can_update:
                    if not dry_run:
                        with repo.chdir():
                            subprocess.call('git push'.split())
                    repo.info('pushed changes')

        if repo.has_master_branch:
            if repo.has_unpushed_master_commits:
                repo.warning('unpushed local changes on master branch')
                with repo.chdir():
                    subprocess.call('git log --oneline origin/master..master'.split())
                if not repo.can_update:
                    if not dry_run:
                        with repo.chdir():
                            subprocess.call('git push'.split())
                    repo.info('pushed changes')

        if repo.can_update:
            if repo.has_uncommitted_changes:
                repo.error('both uncommitted local changes and remote changes')

            if repo.has_unpushed_main_commits:
                repo.error('both unpushed main branch commits and remote changes')

            if repo.has_unpushed_master_commits:
                repo.error('both unpushed master branch commits and remote changes')

            if (
                not repo.has_uncommitted_changes
                and not repo.has_unpushed_main_commits
                and not repo.has_unpushed_master_commits
            ):
                repo.info('pulling changes')
                if not dry_run:
                    with repo.chdir():
                        subprocess.call('git pull'.split())

        if main_is_required := (require_main_branch and not repo.has_main_branch and not repo.is_forked_repo):
            repo.warning("not using standard 'main' branch")
            print(f'{Fore.BLUE}Run the following commands to switch to main branch{Style.RESET_ALL}')
            print('git branch -m master main')
            print('git push -u origin main')
            print(f'gh repo edit {repo.owner}/{repo.name} --default-branch main')
            print('git push origin --delete master')

        if repo.has_main_branch and not any(
            [repo.has_unpushed_main_commits, repo.has_uncommitted_changes, repo.can_update]
        ):
            repo.okay('main branch up to date')

        if repo.has_master_branch and not any(
            [repo.has_unpushed_master_commits, repo.has_uncommitted_changes, repo.can_update, main_is_required]
        ):
            repo.okay('master branch up to date')

    if dry_run:
        print()
        print(Fore.YELLOW + '=' * 30 + '|  END DRY RUN  |' + '=' * 30 + Style.RESET_ALL)
