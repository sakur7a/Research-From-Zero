import httpx
import pytest
from fastapi.testclient import TestClient

from re0.main import create_app


@pytest.fixture
def client(tmp_path):
    def transport(request):
        raise AssertionError(f"Unexpected network request: {request.url}")
    app = create_app(str(tmp_path / "test.sqlite3"), httpx.MockTransport(transport))
    with TestClient(app, headers={"X-Re0-Client": "web", "Content-Type": "application/json"}) as client:
        yield client
