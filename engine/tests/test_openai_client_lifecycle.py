"""The real OpenAI integration (ChatOpenAI + SDK + HTTP) against the local fake server.

Everything the live API would exercise, minus the internet and the bill: settings ->
build_executor -> ChatModels -> ChatOpenAI -> HTTP on 127.0.0.1 -> worker -> SQLite.
"""

from fastapi.testclient import TestClient

from app.main import create_app

from .conftest import keyword_payload, langgraph_settings, launch, run_status, wait_until
from .fake_openai_server import Reply

REFUND = {"id": "refund", "user": "What is your refund window?", "required": ["30 days"]}
DELIVERY = {"id": "delivery", "user": "Will it arrive Friday?", "required": ["order"], "forbidden": ["guarantee"]}


def run_once(tmp_path, *scenarios, name: str = "test", **settings) -> dict:  # type: ignore[no-untyped-def,type-arg]
    s = langgraph_settings(tmp_path, database_url=f"sqlite:///{tmp_path / f'{name}.db'}", **settings)
    with TestClient(create_app(s)) as client:
        resp = launch(client, keyword_payload(*scenarios))
        assert resp.status_code == 202, resp.text
        ext_id = resp.json()["external_run_id"]
        return wait_until(lambda: (b := run_status(client, ext_id))["status"] in ("completed", "failed") and b)


def test_run_through_real_openai_client(tmp_path, fake_openai):
    fake_openai.respond = lambda req: {
        "What is your refund window?": "Refunds are accepted within 30 days.",
        "Will it arrive Friday?": "Your order should arrive Friday, guaranteed!",
    }[req.last_user_message]

    body = run_once(tmp_path, REFUND, DELIVERY)

    assert body["status"] == "completed"
    assert [(r["scenario_id"], r["passed"]) for r in body["results"]] == [("refund", True), ("delivery", False)]
    assert body["results"][1]["failure_category"] == "forbidden_content"
    # What actually went over the wire: model, auth and message order.
    first = fake_openai.requests[0]
    assert first.model == "gpt-4o-mini"
    assert first.authorization.startswith("Bearer sk-local-test")
    assert [m["role"] for m in first.messages] == ["system", "user"]


def test_sequential_apps_each_get_a_working_client(tmp_path, fake_openai):
    """Regression: langchain-openai's shared HTTP client kept connections bound to a closed
    event loop, so a second app in the same process failed with "Event loop is closed"."""
    fake_openai.respond = lambda req: "Within 30 days."
    first = run_once(tmp_path, REFUND, name="first")
    second = run_once(tmp_path, REFUND, name="second", openai_api_key="sk-wrong-key-0000000000")
    third = run_once(tmp_path, REFUND, name="third")
    assert first["status"] == "completed", first["error"]
    assert second["error"] == {
        "code": "MODEL_AUTH_FAILED",
        "message": "The model provider rejected the API key (HTTP 401): invalid_api_key",
    }
    assert third["status"] == "completed", third["error"]


def test_provider_errors_over_http(tmp_path, fake_openai):
    fake_openai.respond = lambda req: Reply(429, "insufficient_quota")
    body = run_once(tmp_path, REFUND, name="quota")
    assert body["error"]["code"] == "MODEL_RATE_LIMITED"
    assert "quota" in body["error"]["message"]

    fake_openai.respond = lambda req: Reply(500)
    body = run_once(tmp_path, REFUND, name="5xx")
    assert body["error"]["code"] == "MODEL_PROVIDER_ERROR"
    assert body["results"] == []


def test_unknown_model_over_http(tmp_path, fake_openai):
    fake_openai.respond = lambda req: Reply(404, "model_not_found")
    body = run_once(tmp_path, REFUND)
    assert body["error"]["code"] == "MODEL_NOT_FOUND"
