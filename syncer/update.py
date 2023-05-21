import pathlib
import typer
import os
import subprocess
from rich import print

app = typer.Typer()

HARDCODED_BASE_DIR = pathlib.Path().home() / 'code' / 'syncer'


@app.callback(invoke_without_command=True)
@app.command()
def main():
    print('[blue]Updating syncer...[/blue]')
    print(HARDCODED_BASE_DIR)
    os.chdir(HARDCODED_BASE_DIR)
    print('[green]Building new wheel...[/green]')
    subprocess.call('deactivate', shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.call('poetry build', shell=True)
    print('[green]Installing new wheel...[/green]')
    subprocess.call('pip install -q --user ~/code/syncer/dist/syncer-0.45.0-py3-none-any.whl --force-reinstall', shell=True)
    print('[green]Done![/green]')
