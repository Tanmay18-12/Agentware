"""Fault injection wrapper: latency, 429, 500, timeout, malformed and partial responses."""

from __future__ import annotations

import asyncio
from typing import Any

from .base import ProviderTimeout, RateLimitError, ServerError

FAULT_KINDS = ("latency", "rate_limit", "server_error", "timeout", "malformed", "partial")


class FaultInjector:
    """Proxy that pops one queued fault per call, then delegates."""

    def __init__(self, inner: Any, faults: dict[str, list[str]] | None = None, latency_s: float = 0.5):
        self.inner = inner
        self.faults: dict[str, list[str]] = {k: list(v) for k, v in (faults or {}).items()}
        self.latency_s = latency_s

    def set_faults(self, method: str, kinds: list[str]) -> None:
        self.faults[method] = list(kinds)

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self.inner, name)
        if name.startswith("_") or not callable(attribute):
            return attribute

        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            queue = self.faults.get(name) or []
            kind = queue.pop(0) if queue else None
            if kind is None:
                return await attribute(*args, **kwargs)
            if kind == "latency":
                await asyncio.sleep(self.latency_s)
                return await attribute(*args, **kwargs)
            if kind == "rate_limit":
                raise RateLimitError(f"429 Too Many Requests from {name}")
            if kind == "server_error":
                raise ServerError(f"500 Internal Server Error from {name}")
            if kind == "timeout":
                raise ProviderTimeout(f"timeout calling {name}")
            if kind == "malformed":
                return {"malformed": True, "raw": "<<not json>>"}
            if kind == "partial":
                result = await attribute(*args, **kwargs)
                if isinstance(result, list):
                    return result[: max(1, len(result) // 2)]
                return {"partial": True}
            return await attribute(*args, **kwargs)

        return wrapper
