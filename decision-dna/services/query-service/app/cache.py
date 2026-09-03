"""
services/query-service/app/cache.py

Small Redis-backed read-path cache.

Two rules make this safe:

1. Every key is namespaced by the *corpus version* — a counter that
   ingestion-service bumps (INCR dna:corpus_version) after a successful ingest.
   A re-ingest therefore invalidates every cached answer at once, without
   needing to enumerate keys.

2. The project filter is part of the key material, so a filtered and an
   unfiltered question can never share an entry.

The cache is strictly best-effort: any Redis problem is logged and ignored.
A cache must never be able to fail a query.

Duplicated per service on purpose - see the note in llm.py.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any, Optional

log = logging.getLogger("query-service")

CORPUS_VERSION_KEY = "dna:corpus_version"
_DEFAULT_VERSION = "0"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def make_key(namespace: str, version: str, *parts: Optional[str]) -> str:
    """Stable, collision-resistant key. Parts are normalised then hashed."""
    material = "\x1f".join((p or "").strip().lower() for p in parts)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"dna:v{version}:{namespace}:{digest}"


class ReadCache:
    """Best-effort JSON cache over an async redis client."""

    def __init__(self, client: Any, namespace: str, ttl_seconds: int):
        self._client = client
        self._namespace = namespace
        self.ttl = ttl_seconds

    @property
    def enabled(self) -> bool:
        return self._client is not None and self.ttl > 0

    async def corpus_version(self) -> str:
        if self._client is None:
            return _DEFAULT_VERSION
        try:
            value = await self._client.get(CORPUS_VERSION_KEY)
            return str(value) if value is not None else _DEFAULT_VERSION
        except Exception as exc:  # noqa: BLE001
            log.debug("cache: corpus_version lookup failed (%s)", exc)
            return _DEFAULT_VERSION

    async def get(self, *parts: Optional[str]) -> Optional[Any]:
        if not self.enabled:
            return None
        try:
            key = make_key(self._namespace, await self.corpus_version(), *parts)
            raw = await self._client.get(key)
            if raw is None:
                return None
            return json.loads(raw)
        except Exception as exc:  # noqa: BLE001 - cache must never break a request
            log.debug("cache: get failed (%s)", exc)
            return None

    async def set(self, value: Any, *parts: Optional[str]) -> None:
        if not self.enabled:
            return
        try:
            key = make_key(self._namespace, await self.corpus_version(), *parts)
            await self._client.set(key, json.dumps(value), ex=self.ttl)
        except Exception as exc:  # noqa: BLE001
            log.debug("cache: set failed (%s)", exc)
