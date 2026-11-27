# MeetingPilot — Master Build Prompt (paste into coding agent)

## ROLE & OBJECTIVE

You are a principal AI systems and security engineer building **MeetingPilot**, a submission for **Hackathon Raptors: Agentware 2026 (Track T1: Inbox & Calendar)**.
Build a production-grade, local-first, autonomous meeting-scheduling agent that ingests incoming emails, checks calendar availability, books slots, sends threaded replies, and files follow-ups—while enforcing hard, code-level security guardrails, a sub-second kill switch, live SSE monitoring, and a tamper-evident hash-chained audit trail.

## HARD ARCHITECTURAL INVARIANTS (NEVER VIOLATE)

1. **LLM Proposes, Code Disposes:** No LLM ever has direct access to email, calendar, filesystem, or shell tools. The Reader LLM receives sanitized text, has **zero tools**, and outputs strictly validated Pydantic JSON (`extra='forbid'`).
2. **Single Choke Point (Policy Gateway):** The orchestrator never calls provider adapters directly. Every side effect is wrapped in an `ActionRequest` and evaluated by `PolicyGateway.execute(action_request)` against rules `P1–P10`.
3. **Fail-Closed Default:** Any exception, schema validation failure, low confidence (`< 0.75`), ambiguous time, or policy block defaults to `ESCALATE` (save draft + queue for human approval) or `QUARANTINE` (no actions), never "proceed anyway".
4. **Offline-First & Zero-Secret Quickstart:** `MOCK_MODE=1` and `LLM_MODE=cassette` must be the defaults in `.env.example` so any judge can run the full pipeline and dashboard in under 2 minutes with zero API keys.
5. **Kill Switch Everywhere:** Check `KillSwitch` (in-process `asyncio.Event` + `.kill` file flag) before every LLM call, before every Policy Gateway tool execution, and between retries. All run tasks execute inside an `asyncio.TaskGroup` so triggering kill cancels in-flight tasks in < 1 second.

## DIRECTORY STRUCTURE TO GENERATE

Create this exact repository layout:

```text
meetingpilot/
├── .agentware.toml
├── .env.example
├── .gitignore
├── LICENSE                      # MIT License
├── Makefile                     # targets: setup, seed, demo, test, lint, clean
├── pyproject.toml               # Python 3.12+, uv-compatible, pydantic>=2.7, fastapi, uvicorn, aiosqlite, httpx, pyyaml, pytest, hypothesis
├── README.md
├── AGENT.md
├── SAFETY.md
├── config/
│   └── prefs.yaml               # Owner tz, working hours, 10m buffer, 4h min notice, max 4 meetings/day, protected focus blocks, allowed/blocked domains
├── scripts/
│   └── seed_demo.py             # Seeds sandbox/calendar.db with realistic events & creates sample emails in sandbox/inbox_samples/
├── static/
│   └── index.html               # Single-file HTML/JS/CSS live SSE dashboard (no npm/node build step)
├── src/
│   └── meetingpilot/
│       ├── __init__.py
│       ├── cli.py               # Typer/argparse CLI: watch, inject, demo, kill, resume, explain, replay, verify-chain
│       ├── config.py            # Loads .env + config/prefs.yaml
│       ├── schemas.py           # Strict Pydantic v2 models (extra='forbid')
│       ├── event_bus.py         # Pub/sub bus -> hash-chained JSONL writer + SQLite state + SSE broadcaster
│       ├── kill_switch.py       # Cross-process KillSwitch (asyncio.Event + .kill file + TaskGroup cancellation)
│       ├── policy_gateway.py    # Single choke point enforcing rules P1-P10 + idempotency + budgets
│       ├── orchestrator.py      # 10-stage deterministic state machine
│       ├── pipeline/
│       │   ├── ingest.py        # Stage 1: HTML->text, hidden-text detector, Message-ID dedup
│       │   ├── prefilter.py     # Stage 2: Loop/bulk/no-reply/rate-limit filter
│       │   ├── screen.py        # Stage 3: Spoof detector (From/Reply-To/Return-Path/SPF/DKIM) + injection heuristics
│       │   ├── reader_llm.py    # Stage 4: Zero-tool quarantined LLM client (live, cassette, stub modes) + 1 JSON repair retry
│       │   ├── validator.py     # Stage 5: Date/duration/attendee/tz validation
│       │   ├── scheduler.py     # Stage 6: Pure Python zoneinfo 15-min grid slot finder & scorer
│       │   └── composer.py      # Stage 8: Template renderer + deterministic Output Guard + anti-loop headers
│       ├── adapters/
│       │   ├── base.py          # MailProvider & CalendarProvider protocols
│       │   ├── mock_mail.py     # Folder-backed inbox/outbox/drafts/labels/followups
│       │   ├── mock_calendar.py # SQLite free/busy + two-phase tentative hold -> re-check -> confirm
│       │   └── fault_injector.py# Wraps providers to inject 429, 500, timeout, malformed JSON
│       └── server.py            # FastAPI app: SSE /events, /api/state, /api/kill, /api/resume, /api/approve/{id}, /api/reject/{id}, /api/inject
├── traces/                      # Sample committed run JSONL traces + global index
└── tests/
    ├── test_policy_gateway.py   # Hypothesis property-based tests for P1-P10
    ├── test_scheduler_dst.py    # DST spring-forward/fall-back, cross-tz, buffers, two-phase hold race tests
    ├── test_kill_switch.py      # Mid-run kill latency (<1s) & cancellation proof
    ├── test_replay_and_audit.py # Deterministic replay & hash-chain tamper detection
    └── trap_pack/
        ├── cases.json           # 32 hostile prompt-injection, spoofing, exfiltration, and edge-case inputs
        └── test_trap_pack.py    # Executes all trap cases and outputs trap_pack_report.json
```

## DETAILED COMPONENT SPECIFICATIONS

### 1. Strict Schemas (`src/meetingpilot/schemas.py`)

Use Pydantic v2 `BaseModel` with `model_config = ConfigDict(extra="forbid")`:

- `EmailEnvelope`: message_id, thread_id, from_addr, reply_to, return_path, auth_results, subject, body_text (max 8000 chars), hidden_text_flag: bool, header_flags: list[str], received_at: datetime.
- `TimeWindow`: start_iso: str | None, end_iso: str | None, raw_phrase: str, tz: str, flexibility: Literal["exact", "window", "flexible"].
- `MeetingIntent`: kind: Literal["meeting_request", "reschedule", "cancel", "slot_selection", "not_meeting", "other"], confidence: float, title: str, duration_min: int, windows: list[TimeWindow], attendees: list[str], location_pref: str | None, ambiguity: list[str], injection_suspected: bool, rationale: str.
- `SlotCandidate`: start_utc: datetime, end_utc: datetime, score: float, reasons: list[str].
- `Decision`: kind: Literal["BOOK", "PROPOSE", "CLARIFY", "ESCALATE", "FILE", "QUARANTINE"], reasons: list[str], slots: list[SlotCandidate].
- `ActionRequest`: id: str, run_id: str, stage: str, tool: str, args: dict[str, Any], idempotency_key: str, risk_tier: Literal["READ", "REVERSIBLE", "IRREVERSIBLE"].
- `PolicyDecision`: action_id: str, verdict: Literal["ALLOW", "GATE", "BLOCK", "LIMIT"], rule_ids: list[str], explanation: str.
- `RunEvent`: seq: int, ts: str, run_id: str, step_id: str, parent_id: str | None, type: str, payload: dict[str, Any], cost_usd: float, prev_hash: str, hash: str.

### 2. Policy Gateway Rules (`src/meetingpilot/policy_gateway.py`)

Implement every rule explicitly with clear `rule_ids`:

- **P1 (Kill Switch):** If `kill_switch.is_triggered()`, immediately return BLOCK (P1) and cancel active tasks.
- **P2 (Registry & Stage Scope):** Registered tools ONLY: `mail.list_new`, `mail.read`, `calendar.freebusy`, `calendar.get_own_event` (READ); `calendar.create_hold`, `calendar.cancel_own_hold`, `mail.draft_reply`, `mail.label`, `followup.file` (REVERSIBLE); `calendar.confirm_event`, `mail.send_reply` (IRREVERSIBLE). Any unregistered tool (e.g., `mail.delete`, `mail.forward`, `calendar.delete_event`) returns BLOCK (P2) and emits a violation event. Enforce stage allowlists (`reader_llm` stage has `[]`).
- **P3 (Arg Validation):** Validate args against per-tool Pydantic schemas; enforce max string lengths.
- **P4 (Auto-Send Guard):** `mail.send_reply` gets ALLOW (P4) ONLY if: `to == envelope.from_addr`, `cc` and `bcc` are empty, `thread_id == envelope.thread_id`, sender domain is in `allowed_domains` (or `MOCK_MODE=1` and not in `blocked_domains`), `output_guard.check(body)` passes, and `sends in current run == 0`. Otherwise downgrade to GATE (P4) (creates draft + approval queue item).
- **P5 (Auto-Book Guard):** `calendar.confirm_event` gets ALLOW (P5) ONLY if: event carries `agent_tag="meetingpilot"`, `len(attendees) <= max_attendees`, `start_utc` is in the future within 30 days and within owner's working hours, and a live `calendar.freebusy` re-check shows zero overlap. Otherwise GATE (P5) or BLOCK (P5).
- **P6 (Budgets & Rate Limits):** Max 25 tool calls/run, $0.25/run, $5.00/day, 120s wall-clock/run, 10 sends/hour, 10 bookings/day. Exceeding returns LIMIT (P6) and forces ESCALATE.
- **P7 (Idempotency):** Compute `sha256(message_id + tool + canonical_json(args))`. If present in SQLite idempotency table, return cached result without re-executing.
- **P8 (Dry-Run):** If `DRY_RUN=1`, log `PolicyDecision` and return synthetic success without mutating adapters.
- **P9 (Audit Emission):** Every call emits a `PolicyDecision` event to the Event Bus before and after execution.

### 3. Deterministic Scheduler & Two-Phase Booking (`src/meetingpilot/pipeline/scheduler.py`)

- Convert all calendar events, working hours, and protected focus blocks into `zoneinfo.ZoneInfo(owner_tz)` before normalizing to UTC.
- Pad busy intervals by `buffer_min` (default 10 min) on both sides.
- Slice available intervals on a 15-minute grid (:00, :15, :30, :45).
- Score slots: +3.0 if inside user's requested window, +1.5 if within preferred hours (10:00–16:00), -1.0 per existing meeting that day, -2.0 if back-to-back with another meeting.
- **Commit Protocol:** Call `calendar.create_hold` → call `calendar.freebusy` to verify no concurrent booking race → if conflict, call `calendar.cancel_own_hold` and retry next candidate (up to 2 retries) → else call `calendar.confirm_event`.

### 4. Output Guard (`src/meetingpilot/pipeline/composer.py`)

Before any `mail.draft_reply` or `mail.send_reply`:

- Verify no email address appears in the body other than the original sender and owner.
- Verify no internal calendar event titles or third-party attendee names appear in the text.
- Verify any URL matches `^https://meet\.example\.com/` (or configured meeting URL prefix).
- Scrub/block API key patterns (`sk-`, `Bearer `, `ghp_`, `AKIA`).
- Reject if text echoes common injection markers (`IGNORE PREVIOUS`, `SYSTEM PROMPT`).
- Append disclosure footer: `\n\n---\nSent automatically by MeetingPilot on behalf of the calendar owner.`
- Set headers: `In-Reply-To`, `References`, and `Auto-Submitted: auto-replied`.

### 5. Live Dashboard (`static/index.html`)

Build a clean, dark-mode, zero-dependency HTML/CSS/JS dashboard connected to `/events` via `EventSource`:

- **Top Bar:** System status badge (RUNNING / HALTED), live cost meter ($0.000 / $0.250), runs processed, estimated minutes saved, and a prominent red KILL SWITCH button (`POST /api/kill`) + Resume button (`POST /api/resume`).
- **4 Main Panels:**
  1. **Active Run & Pipeline Stepper:** Real-time highlight of stages 1–10 with `human_interventions = 0` badge.
  2. **Live Tool & Policy Feed:** Streaming log of `ActionRequest` + `PolicyDecision` badges (ALLOW green, GATE amber, BLOCK/LIMIT red) with rule IDs and latency in ms.
  3. **Approval Queue (ESCALATE items):** Shows proposed event + draft reply with one-click Approve & Send / Reject buttons.
  4. **Mock World Inspector:** Tabs to inspect Inbox, Outbox, Calendar, and inject sample or custom test emails directly from the UI.

## BUILD & VERIFICATION INSTRUCTIONS

- Generate all files cleanly with full implementations (no TODO stubs or placeholder `pass` blocks).
- Run `scripts/seed_demo.py` to populate `sandbox/calendar.db`, sample emails, and pre-recorded LLM cassettes in `tests/cassettes/` so `LLM_MODE=cassette` works out of the box.
- Run the entire pytest suite (`pytest -v`), including `test_policy_gateway.py`, `test_scheduler_dst.py`, `test_kill_switch.py`, `test_replay_and_audit.py`, and `test_trap_pack.py`, and fix any failures until 100% pass.
- Execute one end-to-end sample run and save its verified trace to `traces/sample_book_run.jsonl`.
