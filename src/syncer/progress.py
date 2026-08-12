"""Live progress for a concurrent run: how far in, what is being fetched right now, and how long
each of those has been going.

The gap this closes is the whole reason the concurrency was unpleasant to use. Every repo's git
work happens in a worker thread and nothing is rendered until all of them are collected, so a run
against a slow host showed an empty terminal for minutes and then everything at once. Nothing
distinguished working from wedged, which is the state a progress display exists to name.

Three rules it follows:

- **The in-flight repos are named, with each one's elapsed seconds.** A bare bar answers "is it
  moving" and nothing else. The actual question on a slow machine is *which* repo is slow, and
  that is one line: the longest-running few, oldest first.
- **Stderr, never stdout.** This is not the result, and `--json` on stdout has to survive a pipe.
- **Off unless the terminal can redraw.** Rich repaints in place; into a pipe or a CI log that is
  a repeated line per refresh, so a non-tty gets no display at all rather than a worse one.

Deliberately not a `rich.progress.Progress`: its columns are per-task, and the second line here is
about the run rather than about any one repo. A renderable that rebuilds itself on each refresh is
both smaller and the only way the elapsed times advance while nothing is completing.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from types import TracebackType

from rich.console import Console
from rich.console import Group
from rich.console import RenderableType
from rich.live import Live
from rich.progress_bar import ProgressBar
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from syncer.output import err_console

# Enough to see what is holding the run up without the line wrapping on a narrow terminal.
MAX_RUNNING_SHOWN = 4
REFRESH_PER_SECOND = 8
BAR_WIDTH = 24

# Worded exactly as the summary line's counters, so the number that was climbing during the run is
# the number sitting in the summary afterwards. A count that changes name at the end reads as a
# different measurement.
_LEVEL_TALLY: dict[str, tuple[str, str]] = {
    'operation': ('to sync', 'cyan'),
    'warning': ('need you', 'yellow'),
    'error': ('failed', 'red'),
}


@dataclass(frozen=True)
class _Running:
    label: str
    started: float


def _clock(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f'{minutes}:{secs:02d}'


class RunProgress:
    """A live two-line display over a concurrent run, driven by the workers themselves.

    Every method is safe to call from a worker thread and every one is a no-op when disabled, so
    the caller has no `if progress` branches and the git path has no idea whether anything is
    being drawn.
    """

    def __init__(self, total: int, *, enabled: bool = True, console: Console | None = None) -> None:
        self.total = total
        self.console = console or err_console
        self.enabled = enabled and total > 0 and self.console.is_terminal
        self._lock = threading.Lock()
        self._running: dict[int, _Running] = {}
        self._tally: dict[str, int] = {}
        self._done = 0
        self._next_token = 0
        self._started = time.monotonic()
        self._spinner = Spinner('dots')
        self._live: Live | None = None

    def __enter__(self) -> RunProgress:
        self._started = time.monotonic()
        if self.enabled:
            # transient: the display is scaffolding for the wait, and leaving a finished bar above
            # the report is one more line between the reader and the thing they asked for.
            self._live = Live(self, console=self.console, transient=True, refresh_per_second=REFRESH_PER_SECOND)
            self._live.start()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None

    def start(self, label: str) -> int:
        """Mark a repo as in flight and return the token that finishes it."""
        with self._lock:
            token = self._next_token
            self._next_token += 1
            if self.enabled:
                self._running[token] = _Running(label=label, started=time.monotonic())
        return token

    def finish(self, token: int, level: str | None = None) -> None:
        """Retire a repo from the display, counting it under `level` when it produced a report."""
        with self._lock:
            self._running.pop(token, None)
            self._done += 1
            if level in _LEVEL_TALLY:
                self._tally[level] = self._tally.get(level, 0) + 1

    def _tally_text(self, tally: dict[str, int]) -> Text:
        text = Text()
        for level, (label, style) in _LEVEL_TALLY.items():
            count = tally.get(level, 0)
            if count:
                text.append(f'{count} {label}  ', style=style)
        return text

    def _running_text(self, running: list[_Running], now: float) -> Text:
        if not running:
            return Text('')
        shown = running[:MAX_RUNNING_SHOWN]
        parts = [f'{item.label} {int(now - item.started)}s' for item in shown]
        if len(running) > len(shown):
            parts.append(f'+{len(running) - len(shown)} more')
        return Text(f'   {" · ".join(parts)}', style='blue', overflow='ellipsis', no_wrap=True)

    def __rich__(self) -> RenderableType:
        now = time.monotonic()
        with self._lock:
            done = self._done
            tally = dict(self._tally)
            # Oldest first: the repo that has been running longest is the one holding up the run,
            # and it is the one that stays on screen while the quick ones churn past it.
            running = sorted(self._running.values(), key=lambda item: item.started)
        header = Table.grid(padding=(0, 1))
        header.add_row(
            self._spinner,
            Text(f'{done}/{self.total} repos', style='bold'),
            ProgressBar(total=max(self.total, 1), completed=done, width=BAR_WIDTH),
            self._tally_text(tally),
            Text(_clock(now - self._started), style='blue'),
        )
        return Group(header, self._running_text(running, now))
