import pathlib
import typer
from rich import print

app = typer.Typer()


@app.callback(invoke_without_command=True)
@app.command()
def main():
    print('---------- Path()---------- ')
    print()
    print('[blue]pathlib.Path()[/]')
    print(pathlib.Path())

    print('[blue]pathlib.Path.cwd()[/]')
    print(pathlib.Path.cwd())

    print('[blue]pathlib.Path().home()[/]')
    print(pathlib.Path().home())

    print('[blue]pathlib.Path().resolve()[/]')
    print(pathlib.Path().resolve())

    print('[blue]pathlib.Path().absolute()[/]')
    print(pathlib.Path().absolute())

    print('[blue]pathlib.Path().absolute().parent[/]')
    print(pathlib.Path().absolute().parent)

    print('[blue]pathlib.Path().absolute().parent.parent[/]')
    print(pathlib.Path().absolute().parent.parent)

    print('[blue]pathlib.Path().parent[/]')
    print(pathlib.Path().parent)

    print('[blue]pathlib.Path().parent.absolute()[/]')
    print(pathlib.Path().parent.absolute())

    print('[blue]pathlib.Path().parent.parent[/]')
    print(pathlib.Path().parent.parent)

    print('[blue]pathlib.Path().parent.parent.absolute()[/]')
    print(pathlib.Path().parent.parent.absolute())

    print('[blue]pathlib.Path().absolute().parent / data[/]')
    print(pathlib.Path().absolute().parent / 'data')

    print()
    print('---------- Path(__file__) ----------')
    print()

    print('[blue]pathlib.Path(__file__)[/]')
    print(pathlib.Path(__file__))

    print('[blue]pathlib.Path.cwd()[/]')
    print(pathlib.Path.cwd())

    print('[blue]pathlib.Path(__file__).home()[/]')
    print(pathlib.Path(__file__).home())

    print('[blue]pathlib.Path(__file__).resolve()[/]')
    print(pathlib.Path(__file__).resolve())

    print('[blue]pathlib.Path(__file__).absolute()[/]')
    print(pathlib.Path(__file__).absolute())

    print('[blue]pathlib.Path(__file__).absolute().parent[/]')
    print(pathlib.Path(__file__).absolute().parent)

    print('[blue]pathlib.Path(__file__).absolute().parent.parent[/]')
    print(pathlib.Path(__file__).absolute().parent.parent)

    print('[blue]pathlib.Path(__file__).parent[/]')
    print(pathlib.Path(__file__).parent)

    print('[blue]pathlib.Path(__file__).parent.absolute()[/]')
    print(pathlib.Path(__file__).parent.absolute())

    print('[blue]pathlib.Path(__file__).parent.parent[/]')
    print(pathlib.Path(__file__).parent.parent)

    print('[blue]pathlib.Path(__file__).parent.parent.absolute()[/]')
    print(pathlib.Path(__file__).parent.parent.absolute())

    print('[blue]pathlib.Path(__file__).absolute().parent / data[/]')
    print(pathlib.Path(__file__).absolute().parent / 'data')
