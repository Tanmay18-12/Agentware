# MeetingPilot — Development Guide

> Practical handbook for running, testing and debugging the project. Audit date: **2026-11-27**.
> See [`PROJECT_OVERVIEW.md`](PROJECT_OVERVIEW.md) for architecture and
> [`PROJECT_STATUS.md`](PROJECT_STATUS.md) for known issues.

Commands are marked:

- **verified** — executed successfully during/prior to the audit,
- **unverified** — plausible but not executed in the audit,
- **needs infrastructure** — Docker or external service required,
- **needs credentials** — real API key required.

## 1. Prerequisites

| Requirement | Version used | Notes |
|---|---|---|
| Linux/macOS (Windows via WSL/Git Bash) | — | tested on Fedora Linux |
| Python | **3.14.8** (declared ≥3.11) | `uv` can download it |
| uv | recent | `/usr/bin/uv` here |
| Docker + Compose | 29.8.2 / Compose 5.7.1 | only for Mailpit/Radicale |
| Ports | 8000 dashboard, 1025 SMTP, 8025 Mailpit UI, 5232 CalDAV | loopback only |

No Node, Redis, Postgres, or API key is needed for the default demo.

## 2. First-time setup (verified commands)

```bash
cd <project root>
uv sync --extra dev          # creates .venv, installs runtime + pytest/hypothesis
cp .env.example .env         # optional; defaults already work without .env
uv run pytest                # 110 tests in ~18 s
```

`make setup` is a thin wrapper around `uv sync --extra dev`. Do not use `pip install -e .` — the
project is configured for uv + hatchling and `pytest` relies on `pythonpath = ["src"]`.

## 3. Environment variables

`Settings.from_env()` (`config.py:196-270`) reads `.env` first, then real environment variables
(environment wins). All variables are optional.

| Variable | Default | Meaning |
|---|---|---|
| `MOCK_MODE` | `1` | skips the P4 production domain allowlist; keep on for sandboxes |
| `LLM_MODE` | `cassette` | `cassette` \| `stub` \| `live` |
| `DRY_RUN` | `0` | P8: log the verdict, perform no effect |
| `OWNER_EMAIL`, `OWNER_NAME` | from `prefs.yaml` | identity used by adapters |
| `SANDBOX_DIR`, `TRACES_DIR`, `DATABASE_PATH`, `CONFIG_PATH` | `sandbox`, `traces`, … | paths |
| `HOST`, `PORT` | `127.0.0.1`, `8000` | dashboard bind (`PORT=0` falls back to 8000) |
| `MAIL_PROVIDER` | `mock` | `mock` \| `imap` \| `mailpit` |
| `CALENDAR_PROVIDER` | `mock` | `mock` \| `caldav` |
| `IMAP_HOST/PORT/USER/PASSWORD/SSL/MAILBOX/DRAFTS_MAILBOX` | `127.0.0.1`, `1143`, `meetingpilot`, `meetingpilot`, `0`, `INBOX`, `Drafts` | IMAP adapter |
| `SMTP_HOST/PORT/USER/PASSWORD/STARTTLS/SSL/TIMEOUT_S` | `127.0.0.1`, `1025`, empty, empty, `0`, `0`, `10` | SMTP sending |
| `MAIL_FROM` | owner email | envelope From for real sends |
| `MAILPIT_URL/USER/PASSWORD/TIMEOUT_S` | `http://127.0.0.1:8025`, empty, empty, `10` | Mailpit REST |
| `CALDAV_URL/USER/PASSWORD/CALENDAR/VERIFY_TLS/TIMEOUT_S` | `http://127.0.0.1:5232/owner/`, `owner`, `owner`, `meetingpilot`, `1`, `10` | CalDAV |
| `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `LLM_MODEL` | — / `https://api.openai.com/v1` / `gpt-4o-mini` | live reader only |

`.env` is gitignored. Never commit a real key. `LLM_MODE=live` without `OPENAI_API_KEY` fails the
run closed (`ReaderUnavailable` → ESCALATE).

## 4. Running the project

### Mock demo (no network, no keys) — **verified**

```bash
make demo            # = python scripts/seed_demo.py + meetingpilot demo
```

Expected output: 7 runs ending BOOKED, PROPOSED, BOOKED (option 2), QUARANTINED, ESCALATED,
ESCALATED, SKIPPED; all run chains + index verify; 3 `.eml` replies in `sandbox/outbox`; 2 approvals.

### Dashboard — **verified previously; not re-run in this audit**

```bash
make serve                       # http://127.0.0.1:8000
# or: uv run meetingpilot serve
```

If `PORT` is set to `0` in the shell, `cmd_serve` falls back to 8000 (`cli.py:337-348`).

### One message

```bash
uv run meetingpilot inject sample.json
uv run meetingpilot inject --text "Meet tomorrow at 15:00 IST for 30 min" --from dana@company.com
```

Inline injection uses Message-ID `cli-<ms>@meetingpilot.local`; re-running the same file/Message-ID
yields `DUPLICATE` (expected dedupe).

### Watch

```bash
uv run meetingpilot watch                  # mock: moves sandbox/inbox files to sandbox/processed
MAIL_PROVIDER=mailpit uv run meetingpilot watch --interval 5   # real: polls the provider
```

Both loops were **not** executed end-to-end during the audit (they run forever); the provider polling
function is `cli._watch_provider` (`cli.py:200`).

### Real Mailpit + Radicale (verified 2026-11-27)

```bash
docker compose up -d                 # needs infrastructure; ports 1025/8025/5232
# .env (or inline env):
# MAIL_PROVIDER=mailpit
# CALENDAR_PROVIDER=caldav

uv run python scripts/send_sample_email.py "Can we meet on 2026-11-30 at 10:00 IST for 30 minutes?"
MAIL_PROVIDER=mailpit CALENDAR_PROVIDER=caldav uv run meetingpilot watch
curl -s http://127.0.0.1:8025/api/v1/messages     # see the threaded reply
curl -u owner:owner http://127.0.0.1:5232/owner/meetingpilot/   # calendar collection
```

Notes:

- Mailpit has **no IMAP** server; `mail_provider=mailpit` uses its REST API for reads and SMTP for
  replies. `mail_provider=imap` is for Dovecot/GreenMail/any IMAP server.
- Radicale config is `deploy/radicale/config` (auth `none`, rights `owner_only`); the compose volume
  has `:z` because Fedora's SELinux otherwise hides the config (found during the audit).
- `seed`/`demo` refuse to run while a real provider is selected (`cli._require_mock_providers`).
- Tear down with `docker compose down -v` (removes containers and the calendar volume).

### Live LLM — **needs credentials, unverified against a real provider**

```bash
LLM_MODE=live OPENAI_API_KEY=... uv run meetingpilot inject --text "..."
```

Live mode was tested only against a loopback HTTP stub (`tests/test_reader_live.py`): valid JSON,
one repair retry, two failures → `ReaderUnavailable`, missing key → fail closed, and a full BOOK run.

## 5. Tests

```bash
uv run pytest                                   # all 97
uv run pytest tests/test_real_adapters.py -v    # adapters + gateway + fault injection
uv run pytest -k trap_pack                      # 32 adversarial cases (trap pack)
uv run pytest tests/test_scheduler_dst.py -v    # DST + interval math
```

| File | What it covers |
|---|---|
| `tests/test_policy_gateway.py` | Hypothesis fuzzing of tool × stage × recipient × cc; P2/P3/P4/P6/P7/P8; kill; dry-run; output guard |
| `tests/test_scheduler_dst.py` | spring-forward/fall-back, ambiguous-time clarification, cross-timezone, buffers, protected blocks, race re-check, interval math |
| `tests/test_kill_switch.py` | in-flight cancellation latency, trace evidence, file flag + ack resume, approvals survive kill |
| `tests/test_replay_and_audit.py` | identical replay, tamper detection, explain, `human_interventions=0`, dedupe, index chain |
| `tests/test_reader_live.py` | live LLM against a loopback HTTP server (5 tests) |
| `tests/test_real_adapters.py` | IMAP (fake transport), CalDAV + Mailpit (real loopback HTTP), iCalendar `TZID`/`DURATION`/folded lines, gateway/fault-injection, provider selection |
| `tests/test_approvals.py` | approval queue: approve books + sends via the gateway, reject sends nothing, approvals survive kill/resume |
| `tests/test_server.py` | `/api/state` reads mail/calendar providers (not sandbox files); reject endpoint only decides pending approvals |
| `tests/trap_pack/test_trap_pack.py` | 32 cases → expected decision, no unregistered tool, no quarantine actions, no leaked addresses/titles; writes `trap_pack_report.json` |

Missing coverage (see `PROJECT_STATUS.md`): provider `watch` loop, real IMAP server, real OpenAI,
CalDAV `RRULE` expansion, browser-level dashboard interaction.

## 6. Manual test checklist

### A. Baseline

1. `make demo` → expected 7 outcomes, chains OK.
2. `uv run meetingpilot verify-chain` → every run + index `ok`.
3. `uv run meetingpilot explain <BOOKED run id>` → 10 stages, `policy_decision` events.
4. `uv run meetingpilot replay <run id>` → `"identical": true`.

### B. Safety

5. `uv run meetingpilot kill` → `HALTED`; `meetingpilot inject --text ...` should produce a HALTED
   run; `meetingpilot resume` without `--ack` fails; `--ack` returns RUNNING.
6. Inject `tests/trap_pack/cases.json` payloads manually; confirm QUARANTINE cases execute no tools
   (`explain` shows no `tool_call`).
7. `DRY_RUN=1` + approved send → `P8` ALLOW, no outbox file.

### C. Boundaries

8. Inject malformed JSON (`.json` missing `from`) → QUARANTINED/malformed run.
9. Inject 600 KB `.eml` → malformed (512 KB cap).
10. Request a past date, `25:00`, or a nonexistent DST time → CLARIFY/ESCALATE, never a past slot.
11. Request 6 attendees / blocked domain / VIP sender → ESCALATE with rule IDs.
12. Duplicate Message-ID twice → second is `DUPLICATE`.

### D. Providers (needs infrastructure)

13. `docker compose up -d`; send two mails; run `watch`; confirm replies appear in Mailpit.
14. Seed a busy event exactly at the requested time (see §7) → PROPOSE with 3 alternatives and
    **no** new calendar event.
15. Stop Radicale mid-run → retries then ESCALATE (no booking); stop Mailpit → `BLOCK PX`.

### E. Dashboard

16. Open `/`, inject from the form, watch the stage stepper and feed, click KILL/RESUME, approve a
    queued draft and confirm the outbox grows.
17. With real providers selected, the Inbox tab comes from the provider and the header shows the
    active mail/calendar adapters; Outbox stays the agent's local `.eml` send mirror.

## 7. Seeding a conflict in Radicale (verified)

```bash
curl -u owner:owner -X PUT http://127.0.0.1:5232/owner/meetingpilot/busy.ics \
  -H 'Content-Type: text/calendar' --data-binary $'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:busy-1\r\nDTSTART:20261130T043000Z\r\nDTEND:20261130T050000Z\r\nSUMMARY:Busy\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n'
```

Verified result: requesting a busy `10:00 IST` produces **PROPOSED** with same-day later slots first
(e.g. 10:45 / 11:00 / 11:15 IST), a "three times that work" reply in Mailpit, and no booking in
Radicale. Earlier runs fell back to the next working day; `PROJECT_STATUS.md` BUG-02 records the fix.

## 8. Debugging

- **Where logs live**: there is no logging framework. Structured evidence is in
  `traces/<run_id>.jsonl` (one JSON per event, hash-chained) and `traces/<run_id>.context.json`
  (envelope, intent, busy snapshot, screen result, candidate slots). CLI commands print JSON to stdout.
- **Run timeline**: `uv run meetingpilot explain RUN_ID`; verify tampering with `verify-chain RUN_ID`.
- **Which stage failed**: `explain` shows `stage_end` status; provider failures emit `retry`,
  `policy_violation` and `policy_decision` with explanation; the summary's `errors` field lists
  exceptions.
- **Policy decisions**: search for `"type": "policy_decision"` and read `rule_ids` + `explanation`.
- **Frontend**: browser DevTools → Network (SSE `/events`, `/api/state`), Console. The page has no
  build step; edit `static/index.html` and reload.
- **Database**: `sqlite3 sandbox/meetingpilot.db` then
  `SELECT run_id,outcome,decision,tool_calls FROM runs ORDER BY started_at DESC LIMIT 10;`
  and `SELECT * FROM approvals WHERE status='pending';` (read-only inspection).
- **Calendar (mock)**: `sqlite3 sandbox/calendar.db 'SELECT id,title,start_utc,end_utc,status,agent_tag FROM events;'`
- **Real providers**: `docker compose logs -f`; Mailpit UI at `:8025`; Radicale logs show
  `Successful login: 'owner' (none)` and per-method status lines.
- **Common failure points**: `MAIL_PROVIDER`/`CALENDAR_PROVIDER` typos (uncaught `ValueError`),
  missing Docker containers (ProviderTimeout → retries → BLOCK), `PORT=0` (falls back to 8000),
  re-injecting an already-seen Message-ID (DUPLICATE), Radicale 401 when the SELinux `:z` flag is
  missing from the compose volume.

## 9. Common errors → fixes

| Symptom | Cause | Fix |
|---|---|---|
| `ValueError: unknown MAIL_PROVIDER 'gmail'` traceback | invalid provider name | use `mock`, `imap`, or `mailpit` |
| `ReaderUnavailable: LLM_MODE=live requires OPENAI_API_KEY` | live mode without key | set key or use `cassette` |
| `BLOCK PX ... ProviderTimeout` | provider unreachable | start containers / check host/port |
| Radicale `401 Unauthorized` and `denyall` in logs | config not readable (SELinux) | volume needs `:ro,z` (already in compose) |
| `PORT` weirdness | shell exports `PORT=0` | unset it or rely on the fallback |
| `duplicate_skipped` | Message-ID already ingested | expected dedupe; use a new Message-ID |

## 10. Shutdown / restart

```bash
# stop dashboard: Ctrl-C in its terminal
docker compose down -v          # stop Mailpit/Radicale and drop their data volume
make clean                      # rm sandbox DBs, outbox/drafts, traces, .kill, caches
uv run meetingpilot resume --ack   # only if HALTED and you want to continue
```

`make clean` deletes traces — copy any audit evidence you need first. To start fresh: `make demo`
reseeds the mock world and cassettes.

## 11. Not verified in this audit

- `uv sync` on a clean machine (dependencies unchanged, lockfile present).
- Real IMAP server connectivity (adapter tested with an in-memory `imaplib` fake).
- Real external LLM provider calls.
- Dashboard visual behaviour and in-browser approval clicks (the state API and approval endpoints
  are covered by `tests/test_server.py`; the page itself was not driven in a browser).
- Long-running `watch` loops.
- Any Windows/macOS path or SELinux-disabled host.
