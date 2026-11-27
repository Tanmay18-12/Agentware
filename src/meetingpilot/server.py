"""FastAPI dashboard: state snapshot, SSE stream, kill/resume, approval queue, inject."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .cli import Services, build_services
from .config import Settings


def _state(services: Services) -> dict[str, Any]:
    settings = services.settings
    store = services.bus.store
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    runs = store.list_runs(20)
    return {
        "kill_switch": services.kill.status(),
        "runs": runs,
        "totals": {
            "runs": len(runs),
            "minutes_saved": round(store.total("minutes_saved"), 1),
            "cost_today_usd": round(store.total(f"cost:{today}"), 4),
            "cost_per_run_cap_usd": settings.prefs.cost_per_run_usd,
            "sends": int(store.total("sends")),
            "bookings": int(store.total("bookings")),
            "violations": int(store.total("violations")),
            "human_interventions": sum(int(run.get("human_interventions") or 0) for run in runs),
        },
        "approvals": store.list_approvals(),
        "inbox": _listing(settings.inbox_dir),
        "samples": _listing(settings.sandbox_dir / "inbox_samples"),
        "outbox": _outbox(settings.outbox_dir),
        "drafts": _listing(settings.drafts_dir),
        "calendar": _calendar(services),
        "mode": {"mock_mode": settings.mock_mode, "llm_mode": settings.llm_mode},
    }


def _listing(folder: Path) -> list[dict[str, Any]]:
    if not folder.is_dir():
        return []
    return [
        {"name": path.name, "bytes": path.stat().st_size}
        for path in sorted(folder.glob("*"))
        if path.is_file()
    ]


def _outbox(folder: Path) -> list[dict[str, Any]]:
    messages = []
    if folder.is_dir():
        for path in sorted(folder.glob("*.eml")):
            text = path.read_text(encoding="utf-8", errors="ignore")
            body = text.split("\n\n", 1)[-1][:400]
            messages.append({"name": path.name, "preview": body, "bytes": len(text)})
    return messages


def _calendar(services: Services) -> list[dict[str, Any]]:
    try:
        rows = services.calendar._rows()
    except Exception:  # pragma: no cover - defensive for missing db
        return []
    return [
        {
            "id": row["id"],
            "title": row["title"],
            "start_utc": row["start_utc"],
            "end_utc": row["end_utc"],
            "status": row["status"],
            "agent_tag": row["agent_tag"],
        }
        for row in rows
    ]


def create_app(services: Services | None = None, settings: Settings | None = None) -> FastAPI:
    services = services or build_services(settings)
    app = FastAPI(title="MeetingPilot", version="0.1.0")
    static_dir = services.settings.root / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        page = static_dir / "index.html"
        if not page.is_file():
            raise HTTPException(status_code=404, detail="dashboard not found")
        return FileResponse(page)

    @app.get("/api/state")
    async def state() -> JSONResponse:
        return JSONResponse(_state(services))

    @app.get("/events")
    async def events(request: Request) -> StreamingResponse:
        queue = services.bus.subscribe()

        async def stream():
            try:
                yield _sse({"type": "state", "payload": _state(services)})
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=10.0)
                    except (asyncio.TimeoutError, TimeoutError):
                        yield ": ping\n\n"
                        continue
                    yield _sse(item)
            finally:
                services.bus.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/kill")
    async def kill() -> JSONResponse:
        return JSONResponse(services.kill.trigger(by="dashboard", reason="KILL button"))

    @app.post("/api/resume")
    async def resume() -> JSONResponse:
        try:
            services.kill.reset(ack=True)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return JSONResponse(services.kill.status())

    @app.post("/api/approve/{approval_id}")
    async def approve(approval_id: str) -> JSONResponse:
        result = await services.orchestrator.approve(approval_id)
        return JSONResponse(result, status_code=200 if result.get("ok") else 404)

    @app.post("/api/reject/{approval_id}")
    async def reject(approval_id: str) -> JSONResponse:
        decided = services.bus.store.decide_approval(approval_id, "rejected")
        if decided is None:
            raise HTTPException(status_code=404, detail="approval not found")
        return JSONResponse({"ok": True, "approval_id": approval_id, "status": "rejected"})

    @app.post("/api/inject")
    async def inject(request: Request) -> JSONResponse:
        payload = await request.json()
        from .pipeline.ingest import Ingest

        try:
            if "name" in payload and "body" not in payload:
                path = services.settings.sandbox_dir / "inbox_samples" / str(payload["name"])
                summary = await services.orchestrator.handle_path(path)
            else:
                envelope = Ingest(services.settings, services.bus.store).from_dict(payload)
                summary = await services.orchestrator.handle(envelope)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return JSONResponse(summary.as_dict())

    return app


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"


# Convenience for ``uvicorn meetingpilot.server:app``
app = create_app()
