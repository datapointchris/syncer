"""Console pair and shared presentation for every command.

stdout is data, stderr is everything else: the report itself, and anything else a caller might
pipe into jq, goes to `console`; errors, hints, diagnostics and confirmations go to `err_console`.
A single warning line on stdout turns a JSON parse into a failure that does not even name itself
as a warning.

Every module renders through this pair. There used to be three separate Console objects — one in
repos.py that repos.py never used, one in main.py and one in stats.py — so the entire sync
surface bypassed the helpers below and wrote its errors to stdout, which is the opposite of the
contract stated in this docstring. The icons and line helpers live here rather than beside the
git layer for the same reason: they are presentation, and repos.py has no business importing
rich to run a subprocess.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from rich.console import Console

# highlight=False globally: Rich's automatic highlighting colors anything that looks like a
# number or a path, which turns a report of branch names and commit counts into confetti.
console = Console(highlight=False)
err_console = Console(stderr=True, highlight=False)


# Nerd font icons
ICON_OK = '\uf00c'
ICON_WARN = '\uf071'
ICON_ERR = '\uf00d'
ICON_DOWNLOAD = '\uf0ed'
ICON_PULL = '\uf019'
ICON_PUSH = '\uf093'
ICON_MOVE = '\uf0ec'
ICON_DOT = '\uf444'

LINE_WIDTH = 80


class Tally(StrEnum):
    """The attention counters, keyed rather than spelled at each site.

    The live display and the summary line count the same run seconds apart, so a counter that
    changes its name between them reads as a different measurement. Keying them is the same move
    `Refusal`/`REFUSAL_TEXT` makes for refusals, and for the same reason: a test joining two
    surfaces on an English sentence fails whenever someone rewrites the sentence, which teaches
    you to loosen the assertion instead of trusting it.
    """

    TO_SYNC = 'to_sync'
    NEEDS_YOU = 'needs_you'
    UNVERIFIED = 'unverified'


TALLY_TEXT = {
    Tally.TO_SYNC: 'to sync',
    Tally.NEEDS_YOU: 'need you',
    Tally.UNVERIFIED: 'unverified',
}

# Cyan is syncer's own work, yellow is yours, red is what could not be found out at all.
TALLY_COLOR = {
    Tally.TO_SYNC: 'cyan',
    Tally.NEEDS_YOU: 'yellow',
    Tally.UNVERIFIED: 'red',
}

ALL_ICONS = {ICON_OK, ICON_WARN, ICON_ERR, ICON_DOWNLOAD, ICON_PULL, ICON_PUSH, ICON_MOVE}


def _display_width(text: str) -> int:
    """Calculate display width accounting for double-width nerd font icons."""
    return sum(2 if ch in ALL_ICONS else 1 for ch in text)


def _status_line(icon: str, name: str, msg: str, color: str, branch: str | None = None) -> str:
    prefix = f'{icon}  {name} '
    prefix_w = _display_width(prefix)
    if branch:
        # Displayed: {prefix}{padding} ({branch}) {msg}
        branch_w = len(f' ({branch}) ')
        msg_w = len(msg)
        padding = '_' * max(1, LINE_WIDTH - prefix_w - branch_w - msg_w)
        return f'[{color}]{prefix}{padding}[/{color}] [blue]({branch})[/blue] [{color}]{msg}[/{color}]'
    # Displayed: {prefix}{padding} {msg}
    suffix_w = len(f' {msg}')
    padding = '_' * max(1, LINE_WIDTH - prefix_w - suffix_w)
    return f'[{color}]{prefix}{padding} {msg}[/{color}]'


def emit_json(data: Any) -> None:
    """Print JSON to stdout with no markup or ANSI escapes, so it survives a pipe into jq."""
    print(json.dumps(data, indent=2, default=str))


# soft_wrap leaves wrapping to the terminal. Rich's own wrapping breaks mid-path, and these
# messages exist to hand the user a path to copy.


def error(message: str) -> None:
    err_console.print(f'[red]{message}[/red]', soft_wrap=True)


def success(message: str) -> None:
    err_console.print(f'[green]{message}[/green]', soft_wrap=True)


def hint(message: str) -> None:
    err_console.print(message, soft_wrap=True)
