"""Dashboard state: provider-backed inbox/calendar, plus the approve/reject endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient

from helpers import base_prefs, make_world
from meetingpilot.adapters.mock_calendar import MockCalendar
from meetingpilot.adapters.mock_mail import MockMail
from meetingpilot.cli import Services
from meetingpilot.server import create_app


class ProviderMail(MockMail):
    """Stands in for IMAP/Mailpit: list_new comes from the provider, not sandbox files."""

    async def list_new(self, mailbox: str = "inbox", limit: int = 10):
        if mailbox == "drafts":
            return [{"name": "draft-1.eml", "size_bytes": 10}]
        return [
            {
                "id": "42",
                "message_id": "<provider@mailpit>",
                "subject": "Provider message",
                "from_addr": "dana@company.com",
                "size_bytes": 123,
            }
        ]


class ProviderCalendar(MockCalendar):
    async def list_events(self):
        return [
            {
                "id": "evt-provider",
                "title": "Provider event",
                "start_utc": "2026-12-02T04:30:00+00:00",
                "end_utc": "2026-12-02T05:00:00+00:00",
                "status": "confirmed",
                "agent_tag": "",
            }
        ]


def _client(tmp_path) -> tuple[TestClient, Services]:
    world = make_world(tmp_path, prefs=base_prefs(min_notice_hours=0), busy=[])
    world.settings.inbox_dir.mkdir(parents=True, exist_ok=True)
    (world.settings.inbox_dir / "sandbox-only.eml").write_text("From: stale@local\n\nold", encoding="utf-8")
    services = Services(
        settings=world.settings,
        bus=world.bus,
        mail=ProviderMail(world.settings),
        calendar=ProviderCalendar(world.settings),
        kill=world.kill,
        orchestrator=world.orchestrator,
    )
    return TestClient(create_app(services)), services


def test_state_reads_the_providers_not_sandbox_files(tmp_path):
    client, services = _client(tmp_path)

    payload = client.get("/api/state").json()

    assert [item["name"] for item in payload["inbox"]] == ["Provider message"]
    assert payload["inbox"][0]["from_addr"] == "dana@company.com"
    assert payload["inbox"][0]["bytes"] == 123
    assert "sandbox-only.eml" not in [item["name"] for item in payload["inbox"]]
    assert [item["title"] for item in payload["calendar"]] == ["Provider event"]
    assert payload["mode"]["mail_provider"] == services.settings.mail_provider
    assert payload["mode"]["calendar_provider"] == services.settings.calendar_provider


def test_reject_endpoint_is_idempotent_for_the_queue(tmp_path):
    client, services = _client(tmp_path)
    services.bus.store.add_approval(
        "apr_test",
        "run_test",
        "escalation",
        {"to": "dana@company.com", "thread_id": "t", "subject": "s", "body": "b", "slots": []},
    )

    rejected = client.post("/api/reject/apr_test")
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert services.bus.store.list_approvals("pending") == []

    missing = client.post("/api/reject/apr_test")
    assert missing.status_code == 404


def _services_with(tmp_path, *, host: str = "127.0.0.1", token: str = "") -> Services:
    world = make_world(tmp_path, prefs=base_prefs(min_notice_hours=0), busy=[])
    world.settings.host = host
    world.settings.dashboard_token = token
    return Services(
        settings=world.settings,
        bus=world.bus,
        mail=ProviderMail(world.settings),
        calendar=ProviderCalendar(world.settings),
        kill=world.kill,
        orchestrator=world.orchestrator,
    )


def test_dashboard_token_guards_state_and_controls(tmp_path):
    services = _services_with(tmp_path, token="s3cret")
    client = TestClient(create_app(services))

    assert client.get("/api/state").status_code == 401
    assert client.post("/api/kill").status_code == 401
    assert services.kill.status().get("triggered") is False

    assert client.get("/api/state", headers={"X-Dashboard-Token": "s3cret"}).status_code == 200
    assert client.get("/api/state", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert client.get("/api/state", headers={"X-Dashboard-Token": "wrong"}).status_code == 401


def test_non_loopback_bind_without_token_is_locked_down(tmp_path):
    services = _services_with(tmp_path, host="0.0.0.0")
    client = TestClient(create_app(services))

    assert client.get("/api/state").status_code == 503
    assert client.post("/api/kill").status_code == 503
    assert services.kill.status().get("triggered") is False
    # The page itself is not token-gated (the test world has no static/ dir, hence 404).
    assert client.get("/").status_code != 503


def test_loopback_without_token_stays_open(tmp_path):
    client = TestClient(create_app(_services_with(tmp_path)))

    assert client.get("/api/state").status_code == 200
