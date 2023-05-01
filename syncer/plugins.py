import os
from dataclasses import dataclass
import subprocess
import typer
import pathlib
from rich import print
import json

from .config import settings

GITHUB = 'https://github.com'


@dataclass
class Plugin:
    base: str
    name: str
    owner: str

    @property
    def directory(self):
        return pathlib.Path.home() / self.base / self.name

    @property
    def url(self):
        return '/'.join([GITHUB, self.owner, self.name])


app = typer.Typer()


def message(directory: str, color: str, msg: str):
    print(f'[default]{directory}[/default] [{color}]{msg}[/{color}]')


@app.callback(invoke_without_command=True)
@app.command()
def main():
    """Sync various downloaded plugins"""
    with open(settings.data.PLUGINS, 'r') as f:
        plugins = [Plugin(**p) for p in json.load(f)]

    for plugin in plugins:
        if not plugin.directory.parent.exists():
            plugin.directory.parent.mkdir(parents=True, exist_ok=True)
            message(str(plugin.directory.parent), 'green', 'created')

        if not plugin.directory.exists():
            message(str(plugin.directory), 'blue', 'cloning')
            os.chdir(plugin.directory.parent)
            subprocess.call(['git', 'clone', plugin.url])
            continue
        else:
            os.chdir(plugin.directory)

        if subprocess.getoutput('git fetch'):
            message(str(plugin.directory), 'blue', 'pulling changes')
            subprocess.call(['git', 'pull'])
        else:
            message(str(plugin.directory), 'green', 'up to date')
