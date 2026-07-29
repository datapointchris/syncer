"""Output helpers for the command groups.

stdout is data, stderr is everything else: `--json`, and anything else a caller might pipe into
jq, goes to `console`; errors, hints, and confirmations go to `err_console`. A single warning
line on stdout turns a JSON parse into a failure that does not even name itself as a warning.
"""

from __future__ import annotations

import json
from typing import Any

from rich.console import Console

console = Console()
err_console = Console(stderr=True)


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
