"""Model access through LangChain: provider resolution, message construction, error mapping.

Only OpenAI is wired up. Adding a provider means: a resolve rule, a branch in
`ChatModels._create`, and its credentials in Settings. Error mapping needs no change:
it uses langchain-core's provider-neutral exceptions (ModelAuthenticationError, ...),
which every LangChain provider integration raises.
"""

import re
from typing import Any, Protocol

from langchain_core import exceptions as lc_errors
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import SecretStr

from app import lifecycle
from app.execution import ExecutionError
from app.schemas import AgentSpec, ScenarioSpec

OPENAI = "openai"
SUPPORTED_PROVIDERS = frozenset({OPENAI})
# Bare model names AgentLab users type for OpenAI models.
_OPENAI_PREFIXES = ("gpt-", "chatgpt-", "o1", "o3", "o4")


def resolve_model(model: str) -> tuple[str, str] | None:
    """Map an agent's model string to (provider, provider_model_name), or None if unsupported.

    Accepts "provider:model" (e.g. "openai:gpt-4o-mini") or a bare OpenAI model name.
    """
    model = model.strip()
    if ":" in model:
        provider, _, name = model.partition(":")
        provider, name = provider.strip().lower(), name.strip()
        return (provider, name) if provider in SUPPORTED_PROVIDERS and name else None
    if model.startswith(_OPENAI_PREFIXES):
        return (OPENAI, model)
    return None


def build_messages(agent: AgentSpec, scenario: ScenarioSpec) -> list[BaseMessage]:
    """The agent's system prompt (if any), then the scenario's messages in their exact order and roles."""
    messages: list[BaseMessage] = []
    if agent.system_prompt.strip():
        messages.append(SystemMessage(content=agent.system_prompt))
    role_to_message = {"system": SystemMessage, "user": HumanMessage, "assistant": AIMessage}
    messages.extend(role_to_message[m.role](content=m.content) for m in scenario.input_messages)
    return messages


class ModelSource(Protocol):
    """What the executor needs from a model registry (tests substitute fakes)."""

    def supports(self, model: str) -> bool: ...

    def get(self, model: str) -> BaseChatModel: ...


class ChatModels:
    """Creates and caches one LangChain chat model per model name.

    Owns the HTTP client its models use. Left to itself, langchain-openai shares one
    process-wide client per (base_url, timeout), and that client's keep-alive connections
    belong to whichever event loop opened them; a second app (or event loop) in the same
    process would then fail with "Event loop is closed". Owning the client also means
    connections are closed on shutdown instead of leaking.
    """

    def __init__(
        self,
        *,
        openai_api_key: SecretStr,
        timeout_seconds: float,
        max_retries: int,
        openai_base_url: str | None = None,
    ) -> None:
        self._openai_api_key = openai_api_key
        self._openai_base_url = openai_base_url
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._cache: dict[str, BaseChatModel] = {}
        self._openai_http: Any = None  # openai.DefaultAsyncHttpxClient, created on first use

    def supports(self, model: str) -> bool:
        return resolve_model(model) is not None

    def get(self, model: str) -> BaseChatModel:
        if model not in self._cache:
            self._cache[model] = self._create(model)
        return self._cache[model]

    def _create(self, model: str) -> BaseChatModel:
        resolved = resolve_model(model)
        if resolved is None:
            raise ExecutionError(lifecycle.UNSUPPORTED_MODEL, f"No provider integration for model {model!r}")
        provider, name = resolved
        if provider == OPENAI:
            import openai  # imported lazily: only needed when used
            from langchain_openai import ChatOpenAI

            if self._openai_http is None:
                # The SDK's own client class, so its default limits/redirect settings apply.
                self._openai_http = openai.DefaultAsyncHttpxClient()
            # Temperature is left at the provider default: some OpenAI models reject it.
            return ChatOpenAI(
                model=name,
                api_key=self._openai_api_key,
                timeout=self._timeout,
                max_retries=self._max_retries,
                http_async_client=self._openai_http,
                base_url=self._openai_base_url,  # None = the real OpenAI API
            )
        raise ExecutionError(lifecycle.UNSUPPORTED_MODEL, f"No provider integration for model {model!r}")

    async def aclose(self) -> None:
        """Close the HTTP client. Models are recreated (with a new client) if used again."""
        client, self._openai_http = self._openai_http, None
        self._cache.clear()
        if client is not None:
            await client.aclose()


def map_model_error(exc: BaseException, model: str) -> ExecutionError | None:
    """Translate a LangChain model exception into a structured run failure.

    Messages are written here rather than copied from the provider: provider messages can
    quote request content or (partially masked) API keys.
    """
    status = getattr(exc, "status_code", None)
    code = getattr(exc, "code", None)
    suffix = f" (HTTP {status})" if status else ""
    # The provider's machine-readable error code (e.g. "invalid_api_key",
    # "mismatched_organization") contains no secrets and says *why* a call was refused.
    reason = f": {code}" if isinstance(code, str) and code else ""
    # Order matters where classes could overlap: most specific first.
    if isinstance(exc, lc_errors.ModelAuthenticationError):
        return ExecutionError(lifecycle.MODEL_AUTH_FAILED, f"The model provider rejected the API key{suffix}{reason}")
    if isinstance(exc, lc_errors.ModelPermissionDeniedError):
        return ExecutionError(
            lifecycle.MODEL_AUTH_FAILED, f"The API key is not allowed to use model {model!r}{suffix}{reason}"
        )
    if isinstance(exc, lc_errors.ModelNotFoundError):
        return ExecutionError(
            lifecycle.MODEL_NOT_FOUND, f"Model {model!r} does not exist or this API key cannot access it{suffix}{reason}"
        )
    if isinstance(exc, lc_errors.ModelRateLimitError):
        if getattr(exc, "code", None) == "insufficient_quota":
            return ExecutionError(lifecycle.MODEL_RATE_LIMITED, f"The model provider quota is exhausted{suffix}")
        return ExecutionError(lifecycle.MODEL_RATE_LIMITED, f"Rate limited by the model provider after retries{suffix}")
    if isinstance(exc, lc_errors.ModelTimeoutError):
        return ExecutionError(lifecycle.MODEL_TIMEOUT, "The model provider did not respond in time")
    # langchain-openai only raises ContextOverflowError when certain phrases appear in the
    # provider's message; OpenAI's own error code is the more reliable signal.
    if isinstance(exc, lc_errors.ContextOverflowError) or code == "context_length_exceeded":
        return ExecutionError(lifecycle.MODEL_BAD_REQUEST, f"The conversation is too long for model {model!r}")
    if isinstance(exc, lc_errors.ModelInvalidRequestError):
        return ExecutionError(lifecycle.MODEL_BAD_REQUEST, f"The model provider rejected the request{suffix}{reason}")
    if isinstance(exc, lc_errors.ModelConnectionError):
        return ExecutionError(lifecycle.MODEL_PROVIDER_ERROR, "Could not reach the model provider")
    if isinstance(exc, lc_errors.ModelError):
        return ExecutionError(lifecycle.MODEL_PROVIDER_ERROR, f"The model provider returned an error{suffix}")
    return None


_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-*]{8,}"),  # OpenAI-style keys, including masked ones
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"),
]


def redact_secrets(text: str) -> str:
    """Mask anything that looks like an API key, for messages that leave the process."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "[REDACTED]", text)
    return text
