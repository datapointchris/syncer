from __future__ import annotations

import pytest

# Every variable syncer consults that names a file outside its own directories. A machine
# that runs syncer exports these, so a test relying on the built-in default silently reads
# the developer's real registry instead of the temp one it built.
#
# Measured when $REPOS_JSON was added to registry_location: 31 tests failed at once, and the
# reports were plausible rather than obviously wrong — `doctor` passed against 80 real repos
# where the test expected a missing registry. The suite had no environment isolation at all
# until then, which held only because syncer had no environment layer to leak.
SHARED_PATH_VARS = ('REPOS_JSON',)


@pytest.fixture(autouse=True)
def isolate_shared_environment(monkeypatch):
    """Unset the machine-wide path variables for every test.

    Autouse and unconditional: a test that wants one sets it explicitly, and opting in is the
    safe direction. Opting out is not — a test that forgets reads the real machine and passes
    for the wrong reason, which is the failure this exists to stop.
    """
    for name in SHARED_PATH_VARS:
        monkeypatch.delenv(name, raising=False)
