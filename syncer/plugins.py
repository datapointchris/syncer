import enum
import json
import os
import pathlib
import subprocess
from dataclasses import dataclass
from typing import Annotated

import typer
from colorama import Fore, Style

from syncer.config import settings

GITHUB = 'https://github.com'

app = typer.Typer()


class PluginType(enum.Enum):
    tmux = 'tmux'
    zsh = 'zsh'


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


def message(msg: str, directory: pathlib.Path, color: str):
    message = f'{directory} {color}{"." * (80 - len(str(directory)) - len(msg))} {msg}{Style.RESET_ALL}'
    print(message)


@app.callback(invoke_without_command=True)
@app.command()
def main(app: Annotated[PluginType, typer.Argument()]):
    """Sync various downloaded plugins"""
    with open(settings.data.PLUGINS_DIR / (app.value + '.json')) as f:
        plugins = [Plugin(**p) for p in json.load(f)]

    for plugin in plugins:
        if not plugin.directory.parent.exists():
            plugin.directory.parent.mkdir(parents=True, exist_ok=True)
            message('created', plugin.directory.parent, Fore.GREEN)

        if not plugin.directory.exists():
            message('cloning', plugin.directory, Fore.BLUE)
            os.chdir(plugin.directory.parent)
            subprocess.call(['git', 'clone', plugin.url])
            continue
        else:
            os.chdir(plugin.directory)

        if subprocess.getoutput('git fetch'):
            message('pulling changes', plugin.directory, Fore.BLUE)
            subprocess.call(['git', 'pull'])
        else:
            message('up to date', plugin.directory, Fore.GREEN)
