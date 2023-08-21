import os
import subprocess

import typer
from rich import print

from .config import settings

app = typer.Typer()


@app.callback(invoke_without_command=True)
@app.command()
def main():
    print('[blue]Updating syncer...[/blue]')
    print(settings.syncer.BASE)
    os.chdir(settings.syncer.BASE)

    print('[green]Building new wheel...[/green]')
    subprocess.call('poetry build', shell=True)

    print('[green]Installing new wheel...[/green]')
    wheel_path = next(settings.syncer.DIST_DIR.glob('*.whl'))
    subprocess.call(f'pip install --quiet --user {wheel_path} --force-reinstall', shell=True)
    print('[green]Done![/green]')
