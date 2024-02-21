import pathlib

import typer
from colorama import Fore, Style

app = typer.Typer()

PATHS = {
    'Path()': [
        ('pathlib.Path()', pathlib.Path()),
        ('pathlib.Path.cwd()', pathlib.Path.cwd()),
        ('pathlib.Path().home()', pathlib.Path().home()),
        ('pathlib.Path().absolute()', pathlib.Path().absolute()),
        ('pathlib.Path().absolute().parent', pathlib.Path().absolute().parent),
        ('pathlib.Path().absolute().parent.parent', pathlib.Path().absolute().parent.parent),
        ('pathlib.Path().parent', pathlib.Path().parent),
        ('pathlib.Path().parent.absolute()', pathlib.Path().parent.absolute()),
        ('pathlib.Path().parent.parent', pathlib.Path().parent.parent),
        ('pathlib.Path().parent.parent.absolute()', pathlib.Path().parent.parent.absolute()),
        ('pathlib.Path().absolute().parent / "data"', pathlib.Path().absolute().parent / "data"),
    ],
    'Path(__file__)': [
        ('pathlib.Path(__file__)', pathlib.Path(__file__)),
        ('pathlib.Path.cwd()', pathlib.Path.cwd()),
        ('pathlib.Path(__file__).home()', pathlib.Path(__file__).home()),
        ('pathlib.Path(__file__).resolve()', pathlib.Path(__file__).resolve()),
        ('pathlib.Path(__file__).absolute()', pathlib.Path(__file__).absolute()),
        ('pathlib.Path(__file__).absolute().parent', pathlib.Path(__file__).absolute().parent),
        ('pathlib.Path(__file__).absolute().parent.parent', pathlib.Path(__file__).absolute().parent.parent),
        ('pathlib.Path(__file__).parent', pathlib.Path(__file__).parent),
        ('pathlib.Path(__file__).parent.absolute()', pathlib.Path(__file__).parent.absolute()),
        ('pathlib.Path(__file__).parent.parent', pathlib.Path(__file__).parent.parent),
        ('pathlib.Path(__file__).parent.parent.absolute()', pathlib.Path(__file__).parent.parent.absolute()),
        ('pathlib.Path(__file__).absolute().parent / "data"', pathlib.Path(__file__).absolute().parent / "data"),
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
