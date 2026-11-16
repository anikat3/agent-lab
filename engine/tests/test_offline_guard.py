"""The safeguards that keep the test suite from ever using a paid API."""

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

from .conftest import NetworkAccessBlocked, make_settings

ENGINE_DIR = Path(__file__).resolve().parents[1]


def test_outbound_connections_are_blocked():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        with pytest.raises(NetworkAccessBlocked, match="Tests must stay offline"):
            s.connect(("203.0.113.10", 443))  # TEST-NET-3: never routable anyway


def test_http_clients_cannot_reach_openai():
    import httpx

    with pytest.raises(NetworkAccessBlocked):
        httpx.get("https://162.159.140.245/v1/models", timeout=2)  # an api.openai.com address


def test_loopback_is_allowed(fake_openai):
    import httpx

    resp = httpx.post(f"{fake_openai.base_url}/chat/completions", json={"model": "m", "messages": []})
    assert resp.status_code == 401  # reached the local server (no key sent)


def test_shell_credentials_are_scrubbed():
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID", "EVAL_EXECUTOR"):
        assert name not in os.environ


def test_dotenv_file_is_ignored_by_test_settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("EVAL_EXECUTOR=langgraph\nOPENAI_API_KEY=sk-real-key-from-dotenv\n")
    settings = make_settings(tmp_path)
    assert settings.eval_executor == "none"
    assert settings.openai_api_key is None
    app = create_app(settings)
    with TestClient(app):
        assert app.state.executor is None  # nothing that could call a model


def test_live_tests_are_deselected_by_default():
    """A plain `pytest` never selects live tests, even with RUN_LIVE_TESTS=1 set."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPENAI_")}
    env["RUN_LIVE_TESTS"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "tests/test_live_openai.py"],
        cwd=ENGINE_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert "no tests collected (2 deselected)" in proc.stdout, proc.stdout
    assert proc.returncode == 5  # pytest's "no tests collected"
