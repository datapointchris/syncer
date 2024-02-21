import pathlib
import platform

import typer
from rich import print

from syncer.config import settings

SPACES = ' ' * 10

app = typer.Typer()


def remove_and_symlink(
    file: str,
    source_base: pathlib.Path = settings.dotfiles.SOURCE_DOTFILES,
    target_base: pathlib.Path = settings.dotfiles.TARGET_DOTFILES,
):
    source = source_base / file
    target = target_base / file
    try:
        target.unlink()
    except FileNotFoundError:
        print(f'[yellow]Cannot remove {target}, still proceeding[/]')
    target.symlink_to(source, target_is_directory=source.is_dir())
    print(f'[magenta]{target}[/] --> \n\t[default]{source}[/]')


def symlink_universal():
    print(f'[yellow underline]{SPACES}Dotfiles{SPACES}[/]')
    for dotfile in settings.dotfiles.UNIVERSAL_DOTFILES:
        remove_and_symlink(dotfile)
    print()


def symlink_mac():
    print(f'[yellow underline]{SPACES}MacOS Specific{SPACES}[/]')
    for dotfile in settings.dotfiles.MAC_ONLY_DOTFILES:
        remove_and_symlink(dotfile)

    remove_and_symlink(
        'aws-profiles', source_base=settings.dotfiles.SOURCE_BINS, target_base=settings.dotfiles.TARGET_MAC_BINS
    )
    remove_and_symlink(
        'ichrisbirch', source_base=settings.dotfiles.SOURCE_BINS, target_base=settings.dotfiles.TARGET_MAC_BINS
    )


def symlink_linux():
    print(f'[yellow underline]{SPACES}Linux Specific{SPACES}[/]')
    for dotfile in settings.dotfiles.LINUX_ONLY_DOTFILES:
        remove_and_symlink(dotfile)


@app.callback(invoke_without_command=True)
@app.command()
def main():
    print('[blue]Symlinking Dotfiles...')
    symlink_universal()
    if platform.system() == 'Darwin':
        symlink_mac()
    else:
        symlink_linux()
