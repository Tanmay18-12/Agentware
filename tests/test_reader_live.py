"""LLM_MODE=live: schema parsing, one repair retry and fail-closed, against a loopback stub.

The stub is a real HTTP server on 127.0.0.1, so these tests exercise the actual ``httpx`` call,
headers and JSON payload without touching the network or needing a key.
"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import pytest

from helpers import base_prefs, make_world, next_business_day
from meetingpilot.config import Settings
from meetingpilot.pipeline.reader_llm import (
    DEFAULT_PRICING,
    ReaderLLM,
    ReaderUnavailable,
    estimate_cost,
    model_pricing,
)
from meetingpilot.schemas import EmailEnvelope


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        self.server.stub.requests.append(  # type: ignore[attr-defined]
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "payload": json.loads(raw),
            }
        )
        reply = self.server.stub.responses.pop(0)  # type: ignore[attr-defined]
        body = json.dumps(
            {
                "choices": [{"message": {"content": reply["content"]}}],
                "usage": {
                    "total_tokens": reply.get("tokens", 40),
                    "prompt_tokens": reply.get("prompt_tokens", 30),
                    "completion_tokens": reply.get("completion_tokens", 10),
                },
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


class StubModel:
    """A queue of model replies served over HTTP, plus the requests it received."""

    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.responses: list[dict[str, object]] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.stub = self  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def queue(
        self,
        intent: dict[str, object] | str,
        tokens: int = 40,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> None:
        """Queue one reply. ``tokens`` is the provider total; the split defaults to 10 out."""
        content = intent if isinstance(intent, str) else json.dumps(intent)
        completion = completion_tokens if completion_tokens is not None else min(10, tokens)
        prompt = prompt_tokens if prompt_tokens is not None else tokens - completion
        self.responses.append(
            {
                "content": content,
                "tokens": tokens,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
            }
        )

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def model(monkeypatch):
    stub = StubModel()
    stub.start()
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-a-secret")
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{stub.port}/v1")
    monkeypatch.setenv("LLM_MODEL", "stub-model")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    try:
        yield stub
    finally:
        stub.stop()


def _intent(windows: list[dict[str, object]] | None = None, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": "meeting_request",
        "confidence": 0.95,
        "title": "Roadmap sync",
        "duration_min": 30,
        "windows": windows or [],
        "attendees": [],
        "ambiguity": [],
        "injection_suspected": False,
        "rationale": "stub model",
        "selected_slot": None,
    }
    payload.update(overrides)
    return payload


def _envelope(body: str = "Can we meet tomorrow at 10:00 IST for 30 minutes?") -> EmailEnvelope:
    return EmailEnvelope(
        message_id="<live-1@mail.example.com>",
        thread_id="thread-live",
        from_addr="dana@company.com",
        subject="Roadmap sync",
        body_text=body,
        received_at=datetime.now(timezone.utc),
    )


def _reader(tmp_path) -> ReaderLLM:
    prefs = base_prefs()
    return ReaderLLM(Settings(root=tmp_path, llm_mode="live", prefs=prefs), prefs)


def test_live_mode_parses_the_model_json(tmp_path, model):
    model.queue(_intent(), tokens=123)
    reader = _reader(tmp_path)

    output = asyncio.run(reader.read(_envelope()))

    assert output.mode == "live"
    assert output.model == "stub-model"
    assert output.intent.title == "Roadmap sync"
    assert output.tokens == 123
    assert output.cost_usd > 0
    assert output.cassette_hit is False
    assert output.repaired is False

    request = model.requests[0]
    assert request["path"] == "/v1/chat/completions"
    assert request["authorization"] == "Bearer test-key-not-a-secret"
    payload = request["payload"]
    assert payload["model"] == "stub-model"
    assert payload["response_format"] == {"type": "json_object"}
    assert "Roadmap sync" in payload["messages"][1]["content"]
    assert payload["messages"][0]["role"] == "system"


def test_live_mode_repairs_one_invalid_reply(tmp_path, model):
    model.queue("not json at all", tokens=10)
    model.queue(_intent(), tokens=25)
    reader = _reader(tmp_path)

    output = asyncio.run(reader.read(_envelope()))

    assert output.repaired is True
    assert output.tokens == 35
    assert output.intent.kind == "meeting_request"
    assert len(model.requests) == 2
    retry_messages = model.requests[1]["payload"]["messages"]
    assert retry_messages[-2] == {"role": "assistant", "content": "not json at all"}
    assert "failed validation" in retry_messages[-1]["content"]


def test_live_mode_gives_up_after_two_invalid_replies(tmp_path, model):
    model.queue("{}")
    model.queue('{"kind": "meeting_request"}')
    reader = _reader(tmp_path)

    with pytest.raises(ReaderUnavailable, match="invalid JSON twice"):
        asyncio.run(reader.read(_envelope()))
    assert len(model.requests) == 2


def test_live_mode_without_a_key_fails_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    reader = _reader(tmp_path)

    with pytest.raises(ReaderUnavailable, match="OPENAI_API_KEY"):
        asyncio.run(reader.read(_envelope()))


def test_live_mode_books_end_to_end(tmp_path, model):
    world = make_world(tmp_path / "live", llm_mode="live")
    tz = ZoneInfo(world.settings.prefs.owner_tz)
    day = next_business_day(tz=tz)
    start = datetime.combine(day.date(), time(10, 0), tzinfo=tz)
    end = start + timedelta(minutes=30)
    model.queue(
        _intent(
            windows=[
                {
                    "start_iso": start.isoformat(),
                    "end_iso": end.isoformat(),
                    "raw_phrase": "tomorrow at 10:00 IST",
                    "tz": world.settings.prefs.owner_tz,
                    "flexibility": "exact",
                }
            ]
        )
    )

    envelope = world.ingest(
        {
            "from": "dana@company.com",
            "subject": "Roadmap sync",
            "body": "Can we meet {{D1}} at 10:00 IST for 30 minutes?",
            "message_id": "<live-e2e@mail.example.com>",
            "thread_id": "thread-live-e2e",
        }
    )
    summary = asyncio.run(world.orchestrator.handle(envelope))

    assert summary.decision == "BOOK", summary.reasons
    assert summary.outcome == "BOOKED"
    assert summary.cost_usd > 0
    llm_calls = [
        event for event in world.bus.store.events(summary.run_id) if event["type"] == "llm_call"
    ]
    assert llm_calls and llm_calls[0]["payload"]["mode"] == "live"
    assert llm_calls[0]["payload"]["cassette_hit"] is False


def test_live_mode_prices_prompt_and_completion_tokens_separately(tmp_path, model):
    model.queue(_intent(), tokens=1_000_000, prompt_tokens=900_000, completion_tokens=100_000)
    reader = _reader(tmp_path)

    output = asyncio.run(reader.read(_envelope()))

    # "stub-model" is unknown, so it prices at the default gpt-4o-mini rate: $0.15 in, $0.60 out.
    assert output.tokens == 1_000_000
    assert output.cost_usd == pytest.approx(0.195)


def test_model_pricing_prefers_the_longest_known_prefix():
    assert model_pricing("gpt-4o-mini") == (0.15, 0.60)
    assert model_pricing("openai/gpt-4o-mini-2024-07-18") == (0.15, 0.60)
    assert model_pricing("gpt-4o") == (2.50, 10.00)
    assert model_pricing("some-local-model") == DEFAULT_PRICING


def test_estimate_cost_falls_back_to_the_input_rate_for_total_only_usage():
    assert estimate_cost("gpt-4o-mini", 0, 0, 1_000_000) == 0.15
