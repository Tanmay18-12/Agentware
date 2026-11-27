#!/usr/bin/env python3
"""Seed the mock world: calendar week, sample emails and LLM cassettes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from meetingpilot.config import Settings  # noqa: E402
from meetingpilot.demo_world import seed  # noqa: E402


def main() -> int:
    settings = Settings.from_env()
    result = seed(settings)
    print(json.dumps(result, indent=2))
    print("\nSeeded. Next: `make demo` or `meetingpilot serve`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
