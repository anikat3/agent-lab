"""Model layer: provider resolution, message construction, error mapping, secrets.

Error mapping is exercised through the real stack (ChatOpenAI -> OpenAI SDK -> httpx)
with a mocked HTTP transport, so no request leaves the machine and nothing is billed.
"""

import asyncio
import json

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from app import lifecycle as lc
from app.execution import ExecutionError
from app.llm import ChatModels, build_messages, map_model_error, redact_secrets, resolve_model
from app.schemas import AgentSpec, Message, ScenarioSpec

FAKE_KEY = "sk-test-0123456789abcdefSECRET"


def agent(system_prompt="You are Acme's support agent.", model="gpt-4o-mini") -> AgentSpec:  # type: ignore[no-untyped-def]
    return AgentSpec(id="a1", name="Support bot", system_prompt=system_prompt, model=model)


def scenario(*messages: tuple[str, str]) -> ScenarioSpec:
    return ScenarioSpec(
        id="s1",
        name="Refunds",
        input_messages=[Message(role=r, content=c) for r, c in messages],  # type: ignore[arg-type]
        expected_response=None,
        eval_type="keyword",
        eval_config={"required": ["30 days"]},
    )


# --- provider resolution -----------------------------------------------------------------


@pytest.mark.parametrize(
    "model,expected",
    [
        ("gpt-4o-mini", ("openai", "gpt-4o-mini")),
        ("gpt-4.1", ("openai", "gpt-4.1")),
        ("o3-mini", ("openai", "o3-mini")),
        ("chatgpt-4o-latest", ("openai", "chatgpt-4o-latest")),
        ("openai:gpt-4o-mini", ("openai", "gpt-4o-mini")),
        ("  OpenAI : gpt-4o  ", ("openai", "gpt-4o")),
        ("claude-sonnet-4", None),
        ("anthropic:claude-sonnet-4", None),
        ("llama3", None),
        ("openai:", None),
    ],
)
def test_resolve_model(model, expected):
    assert resolve_model(model) == expected


# --- message construction ----------------------------------------------------------------


def test_system_prompt_first_then_scenario_messages_in_order():
    msgs = build_messages(
        agent(),
        scenario(("user", "Hi"), ("assistant", "Hello! How can I help?"), ("system", "Be brief."), ("user", "Refunds?")),
    )
    assert [(type(m), m.content) for m in msgs] == [
        (SystemMessage, "You are Acme's support agent."),
        (HumanMessage, "Hi"),
        (AIMessage, "Hello! How can I help?"),
        (SystemMessage, "Be brief."),
        (HumanMessage, "Refunds?"),
    ]


def test_empty_system_prompt_is_omitted():
    msgs = build_messages(agent(system_prompt="   "), scenario(("user", "Hi")))
    assert [type(m) for m in msgs] == [HumanMessage]


# --- ChatModels registry -----------------------------------------------------------------


def test_chat_models_builds_configured_chat_openai_and_caches_it():
    models = ChatModels(openai_api_key=SecretStr(FAKE_KEY), timeout_seconds=12, max_retries=1)
    m = models.get("openai:gpt-4o-mini")
    assert isinstance(m, ChatOpenAI)
    assert (m.model_name, m.request_timeout, m.max_retries) == ("gpt-4o-mini", 12, 1)
    assert m.openai_api_key.get_secret_value() == FAKE_KEY
    assert FAKE_KEY not in repr(m)  # the key is a SecretStr, never printed
    assert models.get("openai:gpt-4o-mini") is m
    assert models.supports("gpt-4o-mini") and not models.supports("claude-sonnet-4")


def test_chat_models_rejects_unsupported_model():
    models = ChatModels(openai_api_key=SecretStr(FAKE_KEY), timeout_seconds=12, max_retries=0)
    with pytest.raises(ExecutionError) as e:
        models.get("claude-sonnet-4")
    assert e.value.code == lc.UNSUPPORTED_MODEL


# --- real ChatOpenAI over a mocked transport ---------------------------------------------


def chat_completion(content: str) -> dict:  # type: ignore[type-arg]
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-4o-mini",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def mocked_openai(handler) -> ChatOpenAI:  # type: ignore[no-untyped-def]
    return ChatOpenAI(
        model="gpt-4o-mini",
        api_key=FAKE_KEY,
        max_retries=0,
        timeout=5,
        http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def test_messages_reach_openai_with_roles_and_order_preserved():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json=chat_completion("Refunds within 30 days."))

    msgs = build_messages(agent(), scenario(("user", "Hi"), ("assistant", "Hello!"), ("user", "Refunds?")))
    reply = asyncio.run(mocked_openai(handler).ainvoke(msgs))
    assert reply.text == "Refunds within 30 days."
    assert [(m["role"], m["content"]) for m in seen["body"]["messages"]] == [
        ("system", "You are Acme's support agent."),
        ("user", "Hi"),
        ("assistant", "Hello!"),
        ("user", "Refunds?"),
    ]
    assert seen["body"]["model"] == "gpt-4o-mini"
    assert seen["auth"] == f"Bearer {FAKE_KEY}"


def openai_error(status: int, code: str | None = None, message: str = "error"):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": message, "type": "x", "code": code}})

    return handler


def raise_from(exc: Exception):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


def provider_error(handler) -> BaseException:  # type: ignore[no-untyped-def]
    with pytest.raises(Exception) as caught:
        asyncio.run(mocked_openai(handler).ainvoke("hi"))
    return caught.value


@pytest.mark.parametrize(
    "handler,code,text",
    [
        (openai_error(401, "invalid_api_key"), lc.MODEL_AUTH_FAILED, "rejected the API key (HTTP 401): invalid_api_key"),
        (openai_error(401, "mismatched_organization"), lc.MODEL_AUTH_FAILED, "(HTTP 401): mismatched_organization"),
        (openai_error(403), lc.MODEL_AUTH_FAILED, "not allowed to use model 'gpt-4o-mini' (HTTP 403)"),
        (openai_error(404, "model_not_found"), lc.MODEL_NOT_FOUND, "access it (HTTP 404): model_not_found"),
        (openai_error(429, "rate_limit_exceeded"), lc.MODEL_RATE_LIMITED, "Rate limited"),
        (openai_error(429, "insufficient_quota"), lc.MODEL_RATE_LIMITED, "quota is exhausted"),
        (openai_error(400, "context_length_exceeded"), lc.MODEL_BAD_REQUEST, "too long"),
        (openai_error(400, "invalid_value"), lc.MODEL_BAD_REQUEST, "rejected the request (HTTP 400)"),
        (openai_error(500), lc.MODEL_PROVIDER_ERROR, "returned an error (HTTP 500)"),
        (openai_error(503), lc.MODEL_PROVIDER_ERROR, "returned an error (HTTP 503)"),
        (raise_from(httpx.ReadTimeout("slow")), lc.MODEL_TIMEOUT, "did not respond in time"),
        (raise_from(httpx.ConnectError("refused")), lc.MODEL_PROVIDER_ERROR, "Could not reach"),
    ],
    ids=["401", "401-org", "403", "404", "429", "429-quota", "400-context", "400", "500", "503", "timeout", "connect"],
)
def test_provider_errors_map_to_structured_execution_errors(handler, code, text):
    mapped = map_model_error(provider_error(handler), "gpt-4o-mini")
    assert mapped is not None
    assert mapped.code == code
    assert text in mapped.message


def test_mapped_messages_never_echo_provider_text():
    leaky = openai_error(401, "invalid_api_key", message=f"Incorrect API key provided: {FAKE_KEY}. Prompt was: SECRET")
    mapped = map_model_error(provider_error(leaky), "gpt-4o-mini")
    assert FAKE_KEY not in mapped.message and "SECRET" not in mapped.message


def test_non_model_errors_are_not_mapped():
    assert map_model_error(ValueError("bug"), "gpt-4o-mini") is None


# --- redaction ---------------------------------------------------------------------------


def test_redact_secrets():
    assert redact_secrets(f"key {FAKE_KEY} rejected") == "key [REDACTED] rejected"
    assert redact_secrets("Incorrect API key provided: sk-proj-****abcd1234") == "Incorrect API key provided: [REDACTED]"
    assert redact_secrets("Authorization: Bearer abc.def-ghi_jkl") == "Authorization: Bearer [REDACTED]"
    assert redact_secrets("nothing secret here") == "nothing secret here"
