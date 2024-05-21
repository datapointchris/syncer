from pathlib import Path

from pydantic_settings import BaseSettings


class SyncerSettings(BaseSettings):
    ROOT: Path = Path.home() / 'code' / 'syncer'


class DotfilesSettings(BaseSettings):
    MAC_ONLY_DOTFILES: list[str] = ['.aws/config', '.aws/credentials', '.gitconfig']
    LINUX_ONLY_DOTFILES: list[str] = ['.gitconfig-linux']
    UNIVERSAL_DOTFILES: list[str] = [
        '.bash_profile',
        '.bash_prompt',
        '.zshrc',
        '.bashrc',
        '.inputrc',
        '.profile',
        '.shellcheckrc',
        '.tmux.conf',
        '.config/neofetch',
        '.config/tmuxinator',
        '.iterm2_shell_integration.zsh',
        '.oh-my-zsh/custom/themes/datapointchris.zsh-theme',
    ]

    SOURCE_BASE: Path = Path.home() / 'code' / 'dotfiles'
    SOURCE_DOTFILES: Path = SOURCE_BASE / 'dotfiles'
    SOURCE_BINS: Path = SOURCE_BASE / 'bin'

    TARGET_BASE: Path = Path.home()
    TARGET_DOTFILES: Path = TARGET_BASE
    TARGET_MAC_BINS: Path = Path('/usr/local/bin')


class DataSettings(BaseSettings):
    SYNCER_DATA_DIR: Path = Path(__file__).parent / 'data'
    PLUGINS_DIR: Path = SYNCER_DATA_DIR / 'plugins'
    REPOS_DIR: Path = SYNCER_DATA_DIR / 'repos'
    CODE_ROOT: Path = Path.home() / 'code'


class Settings(BaseSettings):
    syncer: SyncerSettings = SyncerSettings()
    dotfiles: DotfilesSettings = DotfilesSettings()
    data: DataSettings = DataSettings()


settings = Settings()
