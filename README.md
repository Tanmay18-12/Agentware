# MeetingPilot

An unattended agent that turns a meeting-request email into a booked calendar slot, a threaded
reply and a filed follow-up — and refuses to do anything it should not.

Built for **Hackathon Raptors · Agentware 2026 · Track T1 (Inbox & Calendar)**.

```
$ cp .env.example .env
$ uv sync --extra dev
$ make demo          # seed sandbox + full offline end-to-end run
```

`make demo` needs **no API keys and no network**. `MOCK_MODE=1` and `LLM_MODE=cassette` are the
defaults: the mailbox is `sandbox/inbox/`, the calendar is `sandbox/calendar.db`, and the Reader
LLM replays recorded model output (switch to `LLM_MODE=live` with `OPENAI_API_KEY` for real calls).

Real providers are opt-in and still login-free: `MAIL_PROVIDER=mailpit|imap` and
`CALENDAR_PROVIDER=caldav` (see [below](#running-against-open-source-mail-and-calendar)).

Dashboard: `make serve` → <http://127.0.0.1:8000>

## Architecture

```
TRIGGERS                     ORCHESTRATOR (10 stages, deterministic)
mailbox poll ──► 1 Ingest  2 Prefilter  3 Screen  4 Reader LLM (0 tools)
CLI inject   ──► 5 Validate  6 Scheduler  7 Decide  8 Act  9 Verify  10 Record
                                     │
                                     ▼
                       POLICY GATEWAY (single choke point, P1–P10)
                          │                    │
                          ▼                    ▼
                 provider adapters      approval queue (human, optional)

every step ──► EVENT BUS ──┬─► hash-chained JSONL audit (traces/)
                           ├─► SQLite state, budgets, idempotency
                           └─► live dashboard over SSE + cost meter
```

- **LLM proposes, code disposes.** The only model that sees raw email text has zero tools and must
  return schema-valid JSON. Dates, slots and side effects are computed in Python.
- **One choke point.** `Orchestrator` never imports a provider. Every side effect is an
  `ActionRequest` evaluated by `PolicyGateway.execute()`.
- **Fail closed.** Exception, low confidence, schema failure or a blocked action ⇒ `ESCALATE`
  (draft + queue) or `QUARANTINE`, never "proceed anyway".
- **Kill switch everywhere.** Checked before every LLM call, every tool call, and between retries.

See [`AGENT.md`](AGENT.md) for the stage/tool tables and [`SAFETY.md`](SAFETY.md) for the
rule → code → test mapping behind the four safety features.

## CLI cheat sheet

```bash
meetingpilot seed                     # seed sandbox calendar + sample emails + cassettes
meetingpilot demo                     # run the full demonstrate script end-to-end
meetingpilot watch                    # poll sandbox/inbox/ forever
meetingpilot inject FILE|--text "..."  # drop one email into the pipeline
meetingpilot kill                     # trip the kill switch (also: dashboard button, touch .kill)
meetingpilot resume --ack             # clear HALTED state, requires explicit acknowledgement
meetingpilot explain RUN_ID           # human-readable timeline
meetingpilot replay RUN_ID            # re-run from recorded inputs, assert identical decisions
meetingpilot verify-chain [RUN_ID]    # detect tampering in the audit trail
meetingpilot serve                    # dashboard + SSE
```

## Safety, in one table

| Feature | Mechanism | Where |
|---|---|---|
| Guardrails | tool registry + stage scoping, P1–P10 at one gateway | `src/meetingpilot/policy_gateway.py` |
| Live monitoring | FastAPI + SSE, single-file dashboard, cost meter, queue | `src/meetingpilot/server.py`, `static/index.html` |
| Kill switch | `asyncio.Event` + `.kill` file, TaskGroup cancellation, `resume --ack` | `src/meetingpilot/kill_switch.py` |
| Audit trail | append-only JSONL, `prev_hash` chain, `explain`/`replay`/`verify-chain` | `src/meetingpilot/event_bus.py` |

## Tests

```bash
make test     # gateway fuzzing, DST scheduler, kill latency, replay/tamper, trap pack,
              # IMAP/SMTP + Mailpit + CalDAV gateway and fault-injection tests
```

## Running against open-source mail and calendar

Judges can point the same pipeline at real, local, no-account servers. Mock stays the default.

```bash
docker compose up -d          # Mailpit (SMTP 1025, UI/API 8025) + Radicale (CalDAV 5232)

# .env
MAIL_PROVIDER=mailpit         # mock | imap | mailpit
CALENDAR_PROVIDER=caldav      # mock | caldav

uv run meetingpilot watch     # poll the real inbox
uv run python scripts/send_sample_email.py "Can we meet tomorrow at 10:00 IST for 30 min?"
uv run meetingpilot serve     # dashboard (reads the same run state)
```

- **Mailpit** has no IMAP server (only SMTP, POP3 and an HTTP API), so `mail_provider=mailpit`
  reads through `MAILPIT_URL` and replies over SMTP. Replies and receipts appear in
  <http://127.0.0.1:8025>; the dashboard's Outbox keeps a local `.eml` mirror.
- **IMAP** (`MAIL_PROVIDER=imap`) works with Dovecot, GreenMail or any RFC 3501 server; set
  `IMAP_HOST/PORT/USER/PASSWORD`, `IMAP_SSL`, `SMTP_*`.
- **CalDAV** (`CALENDAR_PROVIDER=caldav`) works with Radicale, Baikal or Nextcloud. The adapter
  creates the `meetingpilot` collection on first use; `CALDAV_URL` includes the principal, e.g.
  `http://127.0.0.1:5232/owner/`.
- `seed`/`demo` still build the mock world and refuse to run while a real provider is selected.
- Add `MOCK_MODE=0` with real providers if you want the production domain allowlist enforced;
  mock mode deliberately skips that one P4 check so the sandbox stays frictionless.

To get a conflict in Radicale, add a busy event with any CalDAV client, or:

```bash
curl -u owner:owner -X PUT http://127.0.0.1:5232/owner/meetingpilot/busy.ics \
  -H 'Content-Type: text/calendar' \
  --data-binary $'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:busy-1\r\nDTSTART:20261130T043000Z\r\nDTEND:20261130T053000Z\r\nSUMMARY:Busy\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n'
```

## Honest limits

- Mock remains the default. Real adapters are IMAP/SMTP, Mailpit's REST API and CalDAV; Gmail and
  Google Calendar OAuth are not implemented (use an IMAP/CalDAV bridge if you need them).
- The CalDAV reader expands `RRULE`/`EXDATE` for free/busy (DAILY/WEEKLY/MONTHLY/YEARLY, `INTERVAL`,
  `COUNT`, `UNTIL`, `BYDAY`); `BYSETPOS`/`BYMONTHDAY` are not implemented, and the dashboard still
  lists one row per series.
- Follow-ups always go to `sandbox/followups.jsonl`; Mailpit has no folders, so its drafts and
  labels are local files too. IMAP labels are folder **copies**, never moves or deletes.
- Only non-`.ics` attachments are ignored, not parsed.
- One calendar, one owner. No multi-user, no delegation, no shared mailboxes.
- Multi-party external invites are escalated, not auto-confirmed.
- `LLM_MODE=live` sends sanitized email text to a third-party model; keep `cassette` for demos.

## License

MIT.
