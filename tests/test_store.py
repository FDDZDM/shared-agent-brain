import stat

import pytest

from shared_brain.db import BrainStore
from shared_brain.queue import OfflineQueue


def test_server_fails_closed_when_restart_token_does_not_match(tmp_path):
    store = BrainStore(str(tmp_path / "brain.db"))
    store.initialize("first-token-that-is-long-enough-123")
    with pytest.raises(RuntimeError, match="does not match"):
        store.initialize("different-token-that-is-long-enough")


def test_local_databases_are_owner_only(tmp_path):
    store_path = tmp_path / "brain.db"
    queue_path = tmp_path / "queue.db"
    BrainStore(str(store_path)).initialize("test-token-that-is-long-enough")
    OfflineQueue(str(queue_path))

    assert stat.S_IMODE(store_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(queue_path.stat().st_mode) == 0o600
