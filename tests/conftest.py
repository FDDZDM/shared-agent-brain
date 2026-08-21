from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from shared_brain.api import create_app


TOKEN = "test-token-that-is-long-enough-123456"


@pytest.fixture()
def api(tmp_path):
    app = create_app(str(tmp_path / "brain.db"), TOKEN)
    with TestClient(app) as client:
        yield client


@pytest.fixture()
def auth_headers():
    return {"Authorization": f"Bearer {TOKEN}"}


def op(headers, key):
    return {**headers, "Idempotency-Key": key}

