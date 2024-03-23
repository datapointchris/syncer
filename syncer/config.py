import pathlib

from pydantic_settings import BaseSettings


class SyncerSettings(BaseSettings):
    ROOT: pathlib.Path = pathlib.Path.home() / 'code' / 'syncer'


class DotfilesSettings(BaseSettings):
    MAC_ONLY_DOTFILES: list[str] = [".aws/config", ".aws/credentials", ".gitconfig"]
    LINUX_ONLY_DOTFILES: list[str] = [".gitconfig-linux"]
    UNIVERSAL_DOTFILES: list[str] = [
        ".bash_profile",
        ".bash_prompt",
        ".zshrc",
        ".bashrc",
        ".inputrc",
        ".shellcheckrc",
        ".profile",
        ".tmux.conf",
        ".config/neofetch",
        ".config/tmuxinator",
        ".iterm2_shell_integration.zsh",
        ".oh-my-zsh/custom/themes/datapointchris.zsh-theme",
    ]

    SOURCE_BASE: pathlib.Path = pathlib.Path().home() / "code" / "dotfiles"
    SOURCE_DOTFILES: pathlib.Path = SOURCE_BASE / "dotfiles"
    SOURCE_BINS: pathlib.Path = SOURCE_BASE / "bin"

    TARGET_BASE: pathlib.Path = pathlib.Path().home()
    TARGET_DOTFILES: pathlib.Path = TARGET_BASE
    TARGET_MAC_BINS: pathlib.Path = pathlib.Path("/usr/local/bin")


class DataSettings(BaseSettings):
    SYNCER_DATA_DIR: pathlib.Path = pathlib.Path(__file__).parent / "data"
    PLUGINS_DIR: pathlib.Path = SYNCER_DATA_DIR / 'plugins'
    REPOS_DIR: pathlib.Path = SYNCER_DATA_DIR / 'repos'
    CODE_ROOT: pathlib.Path = pathlib.Path.home() / 'code'


class Settings(BaseSettings):
    syncer: SyncerSettings = SyncerSettings()
    dotfiles: DotfilesSettings = DotfilesSettings()
    data: DataSettings = DataSettings()


settings = Settings()
