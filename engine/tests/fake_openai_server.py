"""A local, OpenAI-compatible chat-completions server for offline tests.

Speaks just enough of POST /v1/chat/completions for ChatOpenAI and the OpenAI SDK: real
HTTP on 127.0.0.1, keep-alive connections (like the real API), bearer-token checks, and
OpenAI-shaped error bodies. Nothing leaves the machine and nothing is billed.

    server = FakeOpenAIServer(respond=lambda req: "Refunds within 30 days.")
    server.start(); ...; server.stop()

`respond(request)` gets a FakeRequest and returns the reply text, or a Reply(status, error_code)
to simulate a provider error.
"""

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

TEST_KEY = "sk-local-test-key-not-a-real-key"


@dataclass(frozen=True)
class FakeRequest:
    model: str
    messages: list[dict[str, Any]]
    authorization: str

    @property
    def last_user_message(self) -> str:
        return next(m["content"] for m in reversed(self.messages) if m["role"] == "user")


@dataclass(frozen=True)
class Reply:
    """Make the server answer with an OpenAI-style error instead of a completion."""

    status: int
    error_code: str | None = None
    message: str = "simulated provider error"


@dataclass
class FakeOpenAIServer:
    respond: Callable[[FakeRequest], "str | Reply"] = lambda req: "OK"
    api_key: str = TEST_KEY
    requests: list[FakeRequest] = field(default_factory=list)

    def start(self, port: int = 0) -> "FakeOpenAIServer":
        """Serve on 127.0.0.1:`port` (0 = any free port) in a background thread."""
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                req = FakeRequest(body.get("model", ""), body.get("messages", []), self.headers.get("authorization", ""))
                owner.requests.append(req)
                if not self.path.endswith("/chat/completions"):
                    return self._send(404, _error("Unknown endpoint", "unknown_url"))
                if req.authorization != f"Bearer {owner.api_key}":
                    return self._send(401, _error("Incorrect API key provided", "invalid_api_key"))
                result = owner.respond(req)
                if isinstance(result, Reply):
                    return self._send(result.status, _error(result.message, result.error_code))
                self._send(200, _completion(req.model, result))

            def _send(self, status: int, payload: dict[str, Any]) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _completion(model: str, text: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-local",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _error(message: str, code: str | None) -> dict[str, Any]:
    return {"error": {"message": message, "type": "invalid_request_error", "code": code}}



def _manual_reply(req: FakeRequest) -> str:
    """Canned, clearly labelled reply for manual runs."""
    return f"[fake-openai] You asked: {req.last_user_message!r}. Refunds are accepted within 30 days."


if __name__ == "__main__":
    # Manual development without API charges (from engine/):
    #   python -m tests.fake_openai_server 8799
    import sys

    server = FakeOpenAIServer(respond=_manual_reply).start(port=int(sys.argv[1]) if len(sys.argv) > 1 else 8799)
    print(f"Fake OpenAI server (NOT a real model) at {server.base_url}  key: {TEST_KEY}  (Ctrl+C to stop)", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.stop()
