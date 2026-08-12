import io
import re

from rich.console import Console

from syncer.output import TALLY_TEXT
from syncer.output import Tally
from syncer.progress import MAX_RUNNING_SHOWN
from syncer.progress import RunProgress
from syncer.progress import _clock


def _terminal(width: int = 120) -> Console:
    return Console(file=io.StringIO(), width=width, force_terminal=True, highlight=False)


def _running_summary(progress: RunProgress) -> tuple[list[str], int]:
    """The in-flight names the display would draw, and how many it folds into '+N more'.

    Read from the state rather than from a render widened until the line fits — pinning a width
    only picks which terminal the suite pretends to be, and the line truncates by design.
    """
    running = sorted(progress._running.values(), key=lambda item: item.started)
    return [item.label for item in running[:MAX_RUNNING_SHOWN]], max(0, len(running) - MAX_RUNNING_SHOWN)


def _render(progress: RunProgress, width: int = 120) -> str:
    buffer = io.StringIO()
    Console(file=buffer, width=width, force_terminal=False, highlight=False).print(progress)
    return buffer.getvalue()


class TestTheDisplayIsOffWhenItCannotRedraw:
    """Rich repaints in place. Into a pipe or a CI log that is one repeated line per refresh, so a
    non-tty gets no display rather than a worse one."""

    def test_a_non_terminal_disables_it(self):
        console = Console(file=io.StringIO(), force_terminal=False)
        assert RunProgress(10, console=console).enabled is False

    def test_a_terminal_enables_it(self):
        assert RunProgress(10, console=_terminal()).enabled is True

    def test_the_caller_can_turn_it_off_for_json(self):
        assert RunProgress(10, enabled=False, console=_terminal()).enabled is False

    def test_an_empty_run_has_nothing_to_show(self):
        assert RunProgress(0, console=_terminal()).enabled is False

    def test_every_method_works_while_disabled(self):
        """The workers call these unconditionally — a display that needed guarding at each call
        site would put terminal branches in the git path."""
        progress = RunProgress(3, enabled=False, console=_terminal())
        with progress:
            token = progress.start('repo')
            progress.finish(token, 'error')
        assert progress._done == 1


class TestTheDisplayNamesWhatIsRunning:
    """A bare bar answers 'is it moving' and nothing else. On a slow machine the question is which
    repo is slow, and that is the line that answers it."""

    def test_in_flight_repos_are_named_with_their_elapsed_seconds(self):
        progress = RunProgress(3, console=_terminal())
        progress.start('alpha')
        assert 'alpha' in _render(progress)

    def test_an_elapsed_time_is_shown_beside_each_name(self):
        progress = RunProgress(3, console=_terminal())
        progress.start('alpha')
        assert re.search(r'alpha \d+s', _render(progress))

    def test_a_finished_repo_leaves_the_line(self):
        progress = RunProgress(3, console=_terminal())
        token = progress.start('alpha')
        progress.finish(token)
        assert 'alpha' not in _render(progress)

    def test_the_count_advances(self):
        progress = RunProgress(2, console=_terminal())
        assert '0/2 repos' in _render(progress)
        progress.finish(progress.start('a'))
        assert '1/2 repos' in _render(progress)

    def test_only_the_longest_running_few_are_named(self):
        progress = RunProgress(20, console=_terminal())
        for index in range(MAX_RUNNING_SHOWN + 3):
            progress.start(f'repo{index}')
        names, overflow = _running_summary(progress)
        assert len(names) == MAX_RUNNING_SHOWN
        assert overflow == 3

    def test_the_oldest_is_named_first(self):
        """The repo holding up the run is the one that stays on screen while quick ones churn."""
        progress = RunProgress(20, console=_terminal())
        progress.start('first')
        progress.start('second')
        names, _ = _running_summary(progress)
        assert names[:2] == ['first', 'second']


class TestTheTallyIsSpelledFromTheSharedTable:
    """Asserting the display's own copy of the words is how a divergence ships green: the summary
    line printed `unverified` while this printed `failed`, and the test that named the invariant
    never read the summary line. tests/test_sync.py joins the other half to the same table."""

    def test_every_counter_is_rendered_with_its_shared_text(self):
        progress = RunProgress(len(Tally), console=_terminal())
        for tally in Tally:
            progress.finish(progress.start(tally.value), tally)
        output = _render(progress)
        for tally in Tally:
            assert f'1 {TALLY_TEXT[tally]}' in output

    def test_a_repo_with_no_counter_is_still_counted_done(self):
        progress = RunProgress(2, console=_terminal())
        progress.finish(progress.start('a'), None)
        progress.finish(progress.start('b'), None)
        output = _render(progress)
        assert '2/2 repos' in output
        for text in TALLY_TEXT.values():
            assert text not in output


class TestClockFormat:
    def test_seconds(self):
        assert _clock(9) == '0:09'

    def test_minutes(self):
        assert _clock(125) == '2:05'
