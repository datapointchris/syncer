import io

from rich.console import Console

from syncer.progress import MAX_RUNNING_SHOWN
from syncer.progress import RunProgress
from syncer.progress import _clock


def _terminal(width: int = 120) -> Console:
    return Console(file=io.StringIO(), width=width, force_terminal=True, highlight=False)


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
        progress.start('dotfiles')
        output = _render(progress)
        assert 'dotfiles' in output
        assert 's' in output

    def test_a_finished_repo_leaves_the_line(self):
        progress = RunProgress(3, console=_terminal())
        token = progress.start('dotfiles')
        progress.finish(token)
        assert 'dotfiles' not in _render(progress)

    def test_the_count_advances(self):
        progress = RunProgress(2, console=_terminal())
        assert '0/2 repos' in _render(progress)
        progress.finish(progress.start('a'))
        assert '1/2 repos' in _render(progress)

    def test_only_the_longest_running_few_are_shown(self):
        progress = RunProgress(20, console=_terminal())
        for index in range(MAX_RUNNING_SHOWN + 3):
            progress.start(f'repo{index}')
        output = _render(progress, width=200)
        assert 'repo0' in output
        assert '+3 more' in output

    def test_the_oldest_is_listed_first(self):
        """The repo holding up the run is the one that stays on screen while quick ones churn."""
        progress = RunProgress(20, console=_terminal())
        progress.start('first')
        progress.start('second')
        output = _render(progress, width=200)
        assert output.index('first') < output.index('second')


class TestTheTallyMatchesTheSummaryLine:
    """A count that changes name between the run and the summary reads as a different
    measurement, so the words are the summary line's own."""

    def test_levels_are_counted_and_named(self):
        progress = RunProgress(3, console=_terminal())
        progress.finish(progress.start('a'), 'operation')
        progress.finish(progress.start('b'), 'warning')
        progress.finish(progress.start('c'), 'error')
        output = _render(progress)
        assert '1 to sync' in output
        assert '1 need you' in output
        assert '1 failed' in output

    def test_synced_repos_add_no_counter(self):
        progress = RunProgress(2, console=_terminal())
        progress.finish(progress.start('a'), 'synced')
        progress.finish(progress.start('b'), None)
        output = _render(progress)
        assert '2/2 repos' in output
        assert 'need you' not in output
        assert 'to sync' not in output


class TestClockFormat:
    def test_seconds(self):
        assert _clock(9) == '0:09'

    def test_minutes(self):
        assert _clock(125) == '2:05'
