from syncer.main import find_untracked_repos


class TestFindUntrackedRepos:
    """The untracked scan is the safety net that catches repos falling out of a
    registry. Scanning only direct children silently missed twenty of them."""

    def _repo(self, path):
        path.mkdir(parents=True, exist_ok=True)
        (path / '.git').mkdir(exist_ok=True)
        return path

    def test_finds_repos_nested_below_the_search_path(self, tmp_path):
        # ~/code/refs/<name> and ~/code/python-projects/<name> are the real shapes.
        nested = self._repo(tmp_path / 'refs' / 'bubbletea')
        found = find_untracked_repos(tmp_path, known_paths=set())
        assert nested in found

    def test_registered_repos_are_not_reported(self, tmp_path):
        known = self._repo(tmp_path / 'tools' / 'forge')
        found = find_untracked_repos(tmp_path, known_paths={known.resolve()})
        assert found == []

    def test_excluded_subtree_is_skipped_entirely(self, tmp_path):
        self._repo(tmp_path / 'refs' / 'fastapi')
        kept = self._repo(tmp_path / 'tools' / 'syncer')
        found = find_untracked_repos(tmp_path, known_paths=set(), excluded={(tmp_path / 'refs').resolve()})
        assert found == [kept]

    def test_does_not_descend_into_a_repo(self, tmp_path):
        outer = self._repo(tmp_path / 'outer')
        self._repo(outer / 'vendored')
        found = find_untracked_repos(tmp_path, known_paths=set())
        assert found == [outer]

    def test_hidden_directories_are_skipped(self, tmp_path):
        self._repo(tmp_path / '.cache' / 'thing')
        assert find_untracked_repos(tmp_path, known_paths=set()) == []
