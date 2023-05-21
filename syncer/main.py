import typer
from syncer import dotfiles, projects, plugins, testpaths, update

app = typer.Typer()


@app.command()
def main():
    """
    Sync dotfiles, projects, or plugins
    """
    pass


app.add_typer(dotfiles.app, name='dotfiles')
app.add_typer(projects.app, name='projects')
app.add_typer(plugins.app, name='plugins')
app.add_typer(testpaths.app, name='testpaths')
app.add_typer(update.app, name='update')

if __name__ == "__main__":
    app()
