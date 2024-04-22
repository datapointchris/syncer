import logging

import typer

from syncer import create_release, dotfiles, plugins, repos, testpaths, update, utilities
from syncer.config import settings

app = typer.Typer(no_args_is_help=True)

logger = logging.getLogger()
handler = logging.FileHandler('/usr/local/var/log/syncer.log')
formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s:%(funcName)s:%(lineno)d | %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.info('syncer started')


@app.command()
def version():
    """Show the current installed version of syncer."""
    print(utilities.get_installed_version())


@app.command()
def readme():
    """Show the README.md file as help text."""
    print(utilities.convert_readme_to_help_text(settings.syncer.ROOT / 'README.md'))


app.add_typer(dotfiles.app, name='dotfiles', short_help='Sync dotfiles for MacOS or Linux based on OS detection')
app.add_typer(repos.app, name='repos', short_help='Sync repositories based on config files in data directory')
app.add_typer(plugins.app, name='plugins', short_help='Sync tmux or zsh plugins')
app.add_typer(testpaths.app, name='testpaths', short_help='Show different outputs for Path methods')
app.add_typer(update.app, name='update', short_help='Update syncer to latest release version on Github')
app.add_typer(create_release.app, name='create-release', short_help='Create a new release for syncer on Github')

if __name__ == "__main__":
    app()
