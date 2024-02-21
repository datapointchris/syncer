import logging

import typer

from syncer import dotfiles, plugins, repos, testpaths, update, create_release

app = typer.Typer()

logger = logging.getLogger("__name__")
handler = logging.FileHandler("/usr/local/var/log/syncer.log")
formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)

app.add_typer(dotfiles.app, name='dotfiles', short_help='Sync dotfiles for MacOS or Linux based on OS detection')
app.add_typer(repos.app, name='repos', short_help='Sync datapointchris or other repositories based on config files in data directory')
app.add_typer(plugins.app, name='plugins', short_help='Sync tmux or zsh plugins')
app.add_typer(testpaths.app, name='testpaths', short_help='Show different outputs for pathlib.Path methods')
app.add_typer(update.app, name='update', short_help='Update syncer to latest release version on Github')
app.add_typer(create_release.app, name='create-release', short_help='Create a new release for syncer on Github')

if __name__ == "__main__":
    app()
