# MeetingPilot — Project Status

> Audit date: **2026-11-27**. All claims below were checked against the tree and by running the suite
> (`uv run pytest` → **110 passed**, ~18 s) unless marked **assumption** or **unverified**.
> Companion documents: [`PROJECT_OVERVIEW.md`](PROJECT_OVERVIEW.md),
> [`DEVELOPMENT_GUIDE.md`](DEVELOPMENT_GUIDE.md).

## 1. Development stage

Functional, submission-ready **hackathon MVP** for Track T1, with a working offline demo, four safety
bonuses, a 32-case Trap Pack, and opt-in real providers (Mailpit verified, CalDAV verified, IMAP
tested against a fake). It is **not** production software (single process, SQLite, no auth/TLS,
single owner).

Repository state: one commit (`4e46b76 Add project files from Freebuff`) on `master` with 66 modified
/ untracked paths after the build sessions. No branches, no tagged release, no CI.

## 2. Feature status

| Feature | Status | Files | Evidence / gap |
|---|---|---|---|
| Ingest (.eml/.json, HTML→text, hidden text, dedupe) | COMPLETE | `pipeline/ingest.py` | trap pack, dedupe test, demo |
| Prefilter (loops/bulk/no-reply/self/blocked/rate) | COMPLETE | `pipeline/prefilter.py` | demo SKIPPED case, trap pack |
| Injection/spoof screening before tools | COMPLETE | `pipeline/screen.py` | trap pack QUARANTINE cases (0 tool calls) |
| Reader: stub + cassette | COMPLETE | `pipeline/reader_llm.py` | 97-test suite, demo |
| Reader: live OpenAI-compatible | IMPLEMENTED, tested vs loopback stub | `reader_llm.py:115-177`, `tests/test_reader_live.py` | real external provider **unverified** |
| Validator (bounds, past, horizon, DST gap) | COMPLETE | `pipeline/validator.py` | scheduler/validator tests |
| Scheduler (phrases, grid, buffers, protected blocks, scoring) | COMPLETE | `pipeline/scheduler.py` | DST tests, demo, real conflict run |
| Decision matrix (BOOK/PROPOSE/CLARIFY/ESCALATE/FILE/QUARANTINE) | COMPLETE | `orchestrator.py:382` | demo + trap pack |
| Two-phase booking + race re-check | COMPLETE | `orchestrator.py:476-594` | `test_two_phase_hold_recheck_gates_a_race` |
| Post-commit verification/compensation | IMPLEMENTED, indirect coverage | `orchestrator.py:768` | no dedicated test for COMPENSATED |
| Approval queue + approve/reject endpoints | COMPLETE | `orchestrator.py:835`, `server.py:140`, `event_bus.py:273` | approve books + sends through the gateway, reject sends nothing, approvals survive kill/resume; double-decide is rejected (`tests/test_approvals.py`, `tests/test_server.py`) |
| Kill switch | COMPLETE | `kill_switch.py` | 4 tests incl. latency <1 s |
| Audit chain + replay + explain | COMPLETE | `event_bus.py:310-430`, `cli.py:245-319` | replay/tamper tests |
| Dashboard (SSE, feed, queue, inject, KILL) | COMPLETE for state/queue; no auth | `server.py`, `static/index.html` | `/api/state` now reads mail/calendar providers; verified against real Mailpit (inbox) and mock calendar; no browser-level run |
| Mock mail/calendar adapters | COMPLETE | `adapters/mock_*.py` | demo + tests |
| Mailpit adapter | COMPLETE (verified real) | `adapters/mailpit_mail.py` | read + SMTP reply + PROPOSE confirmed against Mailpit v1.34.0 |
| IMAP/SMTP adapter | IMPLEMENTED BUT UNVERIFIED (real server) | `adapters/imap_mail.py`, `smtp_mail.py` | fake IMAP/SMTP transport tests only |
| CalDAV adapter | COMPLETE for single events (verified real) | `adapters/caldav_calendar.py` | Radicale hold/confirm/cancel/freebusy + PROPOSE; foreign `TZID`/`DURATION` decoded; `RRULE` = first occurrence |
| Fault injection + fail-closed retries | COMPLETE | `adapters/fault_injector.py`, `policy_gateway.py:230-300` | 429/503/malformed/partial tests |
| Real-provider `watch` loop | IMPLEMENTED, UNVERIFIED | `cli.py:200-226` | never run as a loop |
| Trap Pack | COMPLETE | `tests/trap_pack/` | 32/32 pass, report regenerated |
| Gmail / Google Calendar OAuth | NOT IMPLEMENTED | — | plan COULD; use IMAP/CalDAV |
| Reschedule/cancel of existing events | NOT IMPLEMENTED (by design) | `orchestrator.py:398-410` | always ESCALATE |
| Attachment parsing (.ics invites) | NOT IMPLEMENTED | `pipeline/ingest.py` | non-`.ics` ignored |
| Multi-user / shared calendars | NOT IMPLEMENTED | — | single owner |
| Recurring events (`RRULE`/`EXDATE`) | NOT IMPLEMENTED | `caldav_calendar.py` | read as the first occurrence (documented limit) |

## 3. Known bugs and risks

### CRITICAL

None found in the mock path or in the verified real-provider paths.

### HIGH

**BUG-01 — FIXED 2026-11-27: CalDAV parsed `DTSTART;TZID=...` (and any non-UTC local time) as UTC.**
The codec now parses property parameters, resolves IANA/path-prefixed/Windows TZIDs through
`zoneinfo`, falls back to the first `VTIMEZONE` `TZOFFSETTO` for unknown ids, honours `DURATION`
when `DTEND` is absent, treats `VALUE=DATE` as an all-day event, and unfolds folded lines
(`caldav_calendar.py`, codec section). Regression tests: `test_parse_ics_honours_tzid_duration_and_folding`,
`test_parse_ics_uses_vtimezone_offset_for_unknown_tzid`, and the socket-level
`test_caldav_freebusy_respects_foreign_tzid_event`. Also verified against a real Radicale
container: a foreign `DTSTART;TZID=Asia/Kolkata:20261130T100000` + `DURATION:PT30M` event shows
04:30–05:00Z in free/busy (previously 10:00–10:30Z).

**RISK-01 — Dashboard/API have no authentication and bind is configurable.**
`server.py` exposes `/api/inject`, `/api/kill`, `/api/approve`. Defaults to `127.0.0.1`
(`config.py` `host`), which is acceptable locally; setting `HOST=0.0.0.0` exposes an agent that can
send mail and book events. *Mitigation now*: document loopback-only; *fix before staging*: token auth
+ HTTPS.

### MEDIUM

**BUG-02 — FIXED 2026-11-27: PROPOSE ignored same-day later slots when the exact request conflicted.**
When the requested window has no candidate, `compute_slots` now searches the remainder of the
requested day first (from the requested start to the end of working hours), ranks it, and only then
appends following working days — so a busy 10:00 no longer pushes the proposal to tomorrow.
Ranking still applies the back-to-back penalty, so 10:45 can outrank 10:30 next to a 10:30 meeting.
Regression tests: `test_conflict_offers_same_day_later_slots_first` and the full-run
`test_conflict_run_proposes_same_day_slots`. Verified against real Radicale: a busy 10:00–10:30 IST
event yields proposals at 10:45, 11:00 and 11:15 IST on the same day (previously the next day).

**BUG-03 — FIXED 2026-11-27: fall-back DST ambiguity could be booked instead of clarified.**
`validate_intent` now sets `hard_fail` for ambiguous local timestamps (stage 5 → CLARIFY), and
`resolve_windows` reports ambiguous phrase-resolved starts; stage 6 (`orchestrator._run_stages`)
turns that into `NEEDS_CLARIFICATION` and asks the requester for an explicit UTC offset before any
booking tool runs. Regression tests: `test_fall_back_iso_timestamp_is_a_hard_fail`,
`test_fall_back_phrase_is_surfaced_as_an_issue`, and the end-to-end
`test_ambiguous_request_runs_clarify_instead_of_booking`.

**BUG-04 — FIXED 2026-11-27: the dashboard showed sandbox files instead of provider state.**
`_state` now awaits `mail.list_new("inbox"|"drafts")` and `calendar.list_events()` (normalized rows,
provider failures degrade to an empty list), reports `mail_provider`/`calendar_provider` in `mode`,
and the dashboard title/pill show them. Mailpit routes `drafts` to its local draft store instead of
listing inbox messages. Verified with a real Mailpit container: `/api/state` listed the delivered
mail (subject/from/size) rather than `sandbox/inbox` files. Tests: `tests/test_server.py`.
Outbox remains the sandbox `*.eml` mirror, which is the adapters' genuine sent copy for every
provider.

**RISK-02 — Some blocking work remains inside async endpoints.**
The mock calendar uses synchronous SQLite in `list_events`, and `_outbox` scans files; with many
concurrent clients this can block the event loop. Local-only today.

### LOW

| ID | Finding | Location |
|---|---|---|
| BUG-05 | `prefilter` records an extra hit when the hourly sender limit trips, extending the block | `pipeline/prefilter.py:37-39` |
| BUG-06 | Invalid `MAIL_PROVIDER`/`CALENDAR_PROVIDER` raises an uncaught traceback instead of a clean CLI error | `cli.py:53-71`, `main` `:397` |
| BUG-07 | Mailpit `read()` returns the Mailpit ID as `message_id` (IMAP returns the RFC Message-ID) | `adapters/mailpit_mail.py:69-84` |
| BUG-08 | CalDAV `_ready` is cached forever; a Radicale restart mid-process yields 409/PUT failures until restart | `adapters/caldav_calendar.py:225-250` |
| BUG-09 | `EventBus.emit` reads `seq` and `prev_hash` in separate lock acquisitions; concurrent emits for one run could collide on `(run_id, seq)` | `event_bus.py:337-352` |
| BUG-10 | A new `httpx.AsyncClient` is created per request (no pooling) | `caldav_calendar.py:200-208`, `mailpit_mail.py:36-47` |
| BUG-11 | `AGENT.md` says “68 tests”; `SAFETY.md` line references are stale after the cleanup (e.g. P4 now `:300`, P5 `:342`, P6 `:380`, kill watcher `:86`, `emit` `:337`) | `AGENT.md:97`, `SAFETY.md:16-23` |
| BUG-12 | `.env.example` starts with a stray `[TEMPLATE]` line | `.env.example:1` |
| BUG-13 | `tests/test_policy_gateway.py` creates a world in `tempfile.mkdtemp()` at import time and never cleans it | `tests/test_policy_gateway.py:32` |
| BUG-14 | `MockCalendar.confirm_event` accepts but ignores `agent_tag` (protocol parity) | `adapters/mock_calendar.py:137` |
| BUG-15 | Live-reader cost is a hardcoded `$0.15 / 1M tokens` estimate, not model pricing | `pipeline/reader_llm.py:176` |

### INFORMATIONAL

- `opentelemetry-api` and other packages in `uv pip list` are transitive (FastAPI), not direct project
  dependencies; nothing is imported but missing from `pyproject.toml`.
- No `TODO`/`FIXME` markers exist in `src/`, `tests/`, `scripts/` or `static/`.
- `MeetingPilot_Master_Plan.md` has no Section 12; the docs build on Sections 1–11 only.

## 4. Technical debt

1. **CalDAV scope is a subset of the protocol** (no `RRULE`/`EXDATE`, no discovery, no per-observance
   VTIMEZONE selection). Single events from other clients are decoded correctly now, but recurring
   series are still read as their first occurrence.
2. **Two adapters duplicate local-fallback behaviour** (`MailpitMail` delegates drafts/labels/
   follow-ups to `MockMail`); acceptable but worth documenting as intentional.
3. **No structured logging** — diagnosis relies on JSONL traces and stdout; operators get no log
   levels or request IDs.
4. **No migrations** — `StateStore._migrate` only does `CREATE TABLE IF NOT EXISTS`; schema changes
   require deleting `sandbox/meetingpilot.db`.
5. **In-process-only kill switch** — the `.kill` file covers cross-process triggering, but there is no
   supervisor/worker split yet.
6. **Single-run concurrency semantics** — the orchestrator serialises stages per run; the store/bus
   are not designed for many concurrent runs in one process.
7. **Test coverage gaps** (see §5) — real IMAP/LLM, CalDAV recurrence, browser-level dashboard.
8. **Docs drift** — `AGENT.md`/`SAFETY.md` line numbers and test counts predate the adapter work.

## 5. Test coverage

Current: **103 tests, all passing**. Breakdown: gateway 10 (incl. 60-example Hypothesis property),
scheduler/DST/validator 19, kill switch 4, replay/audit 6, live reader 5, real adapters 27
(incl. the TZID/DURATION and ambiguity regressions), approvals 3, dashboard 2, trap pack 33
(32 cases + report), plus helpers.

Missing coverage, in priority order:

- CalDAV `RRULE`/`EXDATE` expansion (single-event `TZID`/`DURATION` is now covered).
- `Orchestrator.approve()` and `/api/approve|reject` end-to-end.
- Dashboard `_state`, SSE disconnect behaviour, `/api/inject` error paths.
- Real IMAP server (Dovecot/GreenMail) integration.
- Real LLM provider request/response.
- Kill switch during a *provider* call with real adapters.
- Post-commit `COMPENSATED` path.
- Concurrency: two runs in parallel sharing the store.

## 6. Architectural decisions and rationale

| Decision | Evidence | Rationale status |
|---|---|---|
| Mock-first offline default | `config.Settings`, README | documented in plan |
| Single PolicyGateway choke point | `policy_gateway.py`, `orchestrator._act` | documented |
| Reader LLM has zero tools, strict schema | `reader_llm.py`, `schemas.py` | documented |
| Quarantine before reader at score ≥ 0.5 | `orchestrator.py:252-268`, `screen.py:66` | inferred (moved during build) |
| Exact-window conflict → PROPOSE with next days | `scheduler.py:365-368` | inferred |
| Mailpit read via REST (no IMAP) | `mailpit_mail.py` | documented + upstream verified |
| Minimal CalDAV client | `caldav_calendar.py` | inferred (Radicale-first) |
| Keep mock providers as default; real opt-in | `.env.example`, `cli.build_*` | documented |
| Dashboard unauthenticated, loopback | `config.host`, `server.py` | inferred (local demo scope) |

Open decisions: whether CalDAV must support third-party client events (recommended yes); whether the
dashboard should require a token even on loopback; whether to persist provider cursor/state for
`watch`.

## 7. Prioritized roadmap

### Priority 1 — must fix before showing real calendars

1. **Decide/implement dashboard auth for non-loopback hosts** — RISK-01. Files: `server.py`,
   `config.py`, docs. Complexity: Medium.

*(Done 2026-11-27: CalDAV `TZID`/`VTIMEZONE`/`DURATION` — BUG-01; DST fall-back CLARIFY — BUG-03,
both with regression tests.)*

### Priority 2 — finish existing work

2. **Update `AGENT.md` / `SAFETY.md`** line refs and test counts — BUG-11. Complexity: Low.

### Priority 3 — quality

3. Structured logging with run/step IDs; keep JSONL as the audit source of truth.
4. Atomic event sequence allocation (BUG-09) and optional connection/HTTP pooling (BUG-10).
5. Provider cursor/`processed` state for `watch` so more than the latest N messages are handled.
6. RRULE support and ICS attachment parsing.
7. Clean up test temp dirs (BUG-13), `.env.example` (BUG-12).

### Priority 4 — future

8. Gmail/Google Calendar OAuth adapters (plan COULD), preference learning, multi-user calendars,
   reschedule/cancel of agent-created events with human approval.

## 8. Next five concrete tasks

1. Add dashboard authentication/token check before any non-loopback deployment (≈90 min).
2. Add a browser-level dashboard smoke test (inject → approve → outbox grows) (≈45 min).
3. Refresh `AGENT.md`/`SAFETY.md` references and test counts; record the Mailpit/CalDAV verification
   commands in the README (≈30 min).
4. Add real-IMAP (Dovecot) integration coverage; keep the fake transport as the fast path (≈90 min).
5. Implement `RRULE`/`EXDATE` expansion for free/busy so recurring series are not collapsed to their
   first occurrence (≈120 min).

## 9. Things to remember before resuming

- `MOCK_MODE=1` + `LLM_MODE=cassette` is the default; `make demo` must always stay green and offline.
- `make demo` and `seed` intentionally refuse to run with real providers.
- The gateway is the only place side effects may happen — never add a provider call in the
  orchestrator.
- `tests/trap_pack_report.json` is rewritten by the test suite; regenerate after changes.
- Traces and `sandbox/` are gitignored; copy evidence before `make clean`.
- Docker volumes need `:z` on SELinux hosts (already fixed in `docker-compose.yml`).
- `PROJECT_OVERVIEW.md` and `DEVELOPMENT_GUIDE.md` are the source of truth for setup/architecture;
  keep them in sync with code changes.
