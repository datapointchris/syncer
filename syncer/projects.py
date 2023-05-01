import os
from dataclasses import dataclass
import subprocess
import typer
import pathlib
from rich import print
import json

from .config import settings


@dataclass
class Project:
    base: str
    type: str | None
    language: str | None
    name: str
    owner: str
    host: str

    @property
    def directory(self):
        d = pathlib.Path().home() / self.base
        for part in (self.type, self.language, self.name):
            if part:
                d /= part
        return d

    @property
    def url(self):
        return '/'.join([self.host, self.owner, self.name])


app = typer.Typer()


def load_projects(filename: str | pathlib.Path) -> list[Project]:
    with open(filename, 'r') as f:
        return [Project(**project) for project in json.load(f)]


def create_base_directories(projects: list[Project]):
    for path in set(project.directory.parent for project in projects):
        if not path.exists():
            path.mkdir(parents=True)
            message(str(path), 'green', 'created')


def message(directory: str, color: str, msg: str):
    print(f'[default]{directory}[/default] [{color}]{msg}[/{color}]')


@app.callback(invoke_without_command=True)
@app.command()
def main():
    """
    Sync projects
    """
    print('[blue]Syncing Projects...[/blue]')

    projects = load_projects(settings.data.PROJECTS)

    create_base_directories(projects)

    for project in projects:
        if not project.directory.exists():
            message(str(project.directory), 'blue', 'cloning')
            os.chdir(project.directory.parent)
            subprocess.call(['git', 'clone', project.url])
            continue
        else:
            os.chdir(project.directory)

        if uncommitted := subprocess.getoutput('git status --porcelain'):
            message(str(project.directory), 'yellow', 'uncommitted changes')
            subprocess.call(['git', 'status', '--short'])

        if unpushed := subprocess.getoutput('git log origin/master..master'):
            if unpushed.startswith('fatal'):
                message(str(project.directory), 'red', 'no master branch')
            else:
                message(str(project.directory), 'yellow', 'unpushed local changes')
                subprocess.call(['git', 'log', 'origin/master..master'])

        if can_update := subprocess.getoutput('git fetch'):
            if uncommitted:
                message(str(project.directory), 'red', 'uncommitted local changes and remote changes')

            if unpushed := subprocess.getoutput('git log origin/master..master'):
                if unpushed.startswith('fatal'):
                    message(str(project.directory), 'red', 'no master branch and remote changes')
                else:
                    message(str(project.directory), 'red', 'unpushed local changes and remote changes')

            if not uncommitted and not unpushed:
                message(str(project.directory), 'blue', 'pulling changes')
                subprocess.call(['git', 'pull'])

        if not any([uncommitted, unpushed, can_update]):
            message(str(project.directory), 'green', 'up to date')
