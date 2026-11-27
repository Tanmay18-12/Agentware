.PHONY: setup seed demo test lint clean serve watch infra-up infra-down infra-logs

setup:            ## create the venv and install deps
	uv sync --extra dev

seed:             ## seed the sandbox calendar, samples and LLM cassettes
	uv run python scripts/seed_demo.py

demo: seed        ## full offline end-to-end run (no API keys, no network)
	uv run meetingpilot demo

test:             ## full suite: gateway, DST, kill switch, audit, trap pack
	uv run pytest -v

serve:            ## dashboard at http://127.0.0.1:8000
	uv run meetingpilot serve

watch:            ## poll sandbox/inbox forever
	uv run meetingpilot watch

lint:             ## compile check (no external linter required)
	uv run python -m compileall -q src tests scripts

infra-up:         ## Mailpit + Radicale for MAIL_PROVIDER=mailpit CALENDAR_PROVIDER=caldav
	docker compose up -d

infra-down:
	docker compose down

infra-logs:
	docker compose logs -f

clean:
	rm -rf sandbox/*.db sandbox/outbox/* sandbox/drafts/* traces/*.jsonl .kill .pytest_cache
