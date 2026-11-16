import pytest

from app import __version__

from .conftest import AUTH, assert_error


def test_health_ok(client):
    resp = client.get("/health", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "version": __version__,
        "execution": {"available": True, "executor": "not_executed", "reason": None},
    }


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-key"},
        {"Authorization": "Basic test-key"},
        {"Authorization": "Bearer"},
        {"Authorization": "test-key"},
    ],
    ids=["missing", "wrong-key", "wrong-scheme", "empty-token", "no-scheme"],
)
@pytest.mark.parametrize("method,path", [("GET", "/health"), ("POST", "/runs"), ("GET", "/runs/run_x")])
def test_auth_required_everywhere(client, headers, method, path):
    resp = client.request(method, path, headers=headers, json={})
    assert_error(resp, 401, "UNAUTHORIZED")
    assert resp.headers["www-authenticate"] == "Bearer"


def test_bearer_scheme_is_case_insensitive(client):
    assert client.get("/health", headers={"Authorization": "bearer test-key"}).status_code == 200


def test_unknown_route_uses_error_envelope(client):
    assert_error(client.get("/nope", headers=AUTH), 404, "NOT_FOUND")


def test_wrong_method_uses_error_envelope(client):
    assert_error(client.delete("/health", headers=AUTH), 405, "METHOD_NOT_ALLOWED")


def test_settings_require_api_key(monkeypatch, tmp_path):
    from pydantic import ValidationError

    from app.config import Settings

    monkeypatch.delenv("EVAL_ENGINE_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)  # make sure no local .env supplies one
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]
