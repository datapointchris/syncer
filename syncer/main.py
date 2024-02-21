import logging

import typer

from syncer import dotfiles, plugins, repos, testpaths, update

app = typer.Typer()

logger = logging.getLogger("__name__")
handler = logging.FileHandler("/usr/local/var/log/syncer.log")
formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)

app.add_typer(dotfiles.app, name='dotfiles')
app.add_typer(repos.app, name='repos')
app.add_typer(plugins.app, name='plugins')
app.add_typer(testpaths.app, name='testpaths')
app.add_typer(update.app, name='update')

if __name__ == "__main__":
    app()
