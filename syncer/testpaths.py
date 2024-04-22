from pathlib import Path

import typer
from colorama import Fore, Style

app = typer.Typer()

PATHS = {
    'Path()': [
        ('Path()', Path()),
        ('Path.cwd()', Path.cwd()),
        ('Path.home()', Path.home()),
        ('Path().absolute()', Path().absolute()),
        ('Path().absolute().parent', Path().absolute().parent),
        ('Path().absolute().parent.parent', Path().absolute().parent.parent),
        ('Path().parent', Path().parent),
        ('Path().parent.absolute()', Path().parent.absolute()),
        ('Path().parent.parent', Path().parent.parent),
        ('Path().parent.parent.absolute()', Path().parent.parent.absolute()),
        ('Path().absolute().parent / "data"', Path().absolute().parent / "data"),
    ],
    'Path(__file__)': [
        ('Path(__file__)', Path(__file__)),
        ('Path.cwd()', Path.cwd()),
        ('Path(__file__).home()', Path(__file__).home()),
        ('Path(__file__).resolve()', Path(__file__).resolve()),
        ('Path(__file__).absolute()', Path(__file__).absolute()),
        ('Path(__file__).absolute().parent', Path(__file__).absolute().parent),
        ('Path(__file__).absolute().parent.parent', Path(__file__).absolute().parent.parent),
        ('Path(__file__).parent', Path(__file__).parent),
        ('Path(__file__).parent.absolute()', Path(__file__).parent.absolute()),
        ('Path(__file__).parent.parent', Path(__file__).parent.parent),
        ('Path(__file__).parent.parent.absolute()', Path(__file__).parent.parent.absolute()),
        ('Path(__file__).absolute().parent / "data"', Path(__file__).absolute().parent / "data"),
    ],
}


@app.callback(invoke_without_command=True)
@app.command()
def main():
    for path, commands in PATHS.items():
        print()
        print(Fore.GREEN + '-' * 20 + path + '-' * 20 + Style.RESET_ALL)
        print()
        for name, command in commands:
            print(Fore.BLUE + name + Style.RESET_ALL)
            print(command)
            print()
