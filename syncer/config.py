from pydantic import BaseSettings
import pathlib


class SyncerSettings(BaseSettings):
    BASE: pathlib.Path = pathlib.Path().home() / 'code' / 'syncer'
    DIST_DIR: pathlib.Path = BASE / 'dist'


class DotfilesSettings(BaseSettings):
    MAC_ONLY_DOTFILES: list[str] = ['.aws/config', '.aws/credentials', '.gitconfig']
    LINUX_ONLY_DOTFILES: list[str] = ['.gitconfig-linux']
    UNIVERSAL_DOTFILES: list[str] = [
        '.bash_profile',
        '.bash_prompt',
        '.zshrc',
        '.bashrc',
        '.inputrc',
        '.shellcheckrc',
        '.tmux.conf',
        '.config/neofetch',
        '.config/tmuxinator',
        '.iterm2_shell_integration.zsh',
        '.oh-my-zsh/custom/themes/datapointchris.zsh-theme',
    ]

    class DotfilesSourceSettings(BaseSettings):
        BASE: pathlib.Path = pathlib.Path().home() / 'code' / 'dotfiles'
        DOTFILES: pathlib.Path = BASE / 'dotfiles'
        BINS: pathlib.Path = BASE / 'bin'

    class DotfilesTargetSettings(BaseSettings):
        BASE: pathlib.Path = pathlib.Path().home()
        DOTFILES: pathlib.Path = BASE
        MAC_BINS: pathlib.Path = pathlib.Path('/usr/local/bin')

    source = DotfilesSourceSettings()
    target = DotfilesTargetSettings()


class DataSettings(BaseSettings):
    DATA_DIR: pathlib.Path = pathlib.Path(__file__).parent / 'data'
    PLUGINS: pathlib.Path = DATA_DIR / 'plugins.json'
    PROJECTS: pathlib.Path = DATA_DIR / 'projects.json'
    LABS: pathlib.Path = DATA_DIR / '1904labs.json'


class Settings(BaseSettings):
    syncer = SyncerSettings()
    dotfiles = DotfilesSettings()
    data = DataSettings()


settings = Settings()
