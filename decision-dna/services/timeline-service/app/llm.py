"""
services/timeline-service/app/llm.py

Bounded-retry async chat helper.

Replaces the previous behaviour where ANY exception permanently reassigned the
module-global OpenAI client to a local stub, for the life of the process, with
no path back. A single transient timeout or 429 turned the service into a
canned-text generator that still answered HTTP 200.

Policy now:
  attempt -> bounded exponential backoff -> attempt -> ... -> LLMUnavailable

The caller turns LLMUnavailable into an explicit 503. Stub output is only ever
produced when LLM_STUB_FALLBACK is explicitly enabled, and when it is the
caller must mark the response `degraded` so the contract stays honest.

This module is intentionally duplicated per service: each service Dockerfile
does `COPY . .` from its own directory, so a shared top-level package would
never reach the container. See AUDIT notes on `shared/`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
from typing import Any, List, Optional

log = logging.getLogger("timeline-service")


class LLMUnavailable(RuntimeError):
    """Raised when the chat provider could not be reached within the retry budget.

    `permanent` is True when the provider rejected the request outright — a bad
    model id, a bad key, no access — rather than failing transiently. Those are
    configuration errors: retrying cannot fix them.
    """

    def __init__(self, message: str, attempts: int, last_error: Optional[BaseException] = None,
                 permanent: bool = False):
        super().__init__(message)
        self.attempts = attempts
        self.last_error = last_error
        self.permanent = permanent


# HTTP statuses where the provider has told us the request itself is wrong.
# Retrying them just turns an instant failure into a slow one and triples the
# log noise. 404 in particular is what an unknown-or-inaccessible model id
# returns, e.g. Groq's:
#   404 {'error': {'message': "The model `<id>` does not exist or you do not
#        have access to it.", 'code': 'model_not_found'}}
NON_RETRYABLE_STATUS = {400, 401, 403, 404, 405, 422}

# Same idea by exception class, for clients that do not expose status_code.
NON_RETRYABLE_TYPES = {
    "BadRequestError", "AuthenticationError", "PermissionDeniedError",
    "NotFoundError", "UnprocessableEntityError",
}


def status_of(exc: BaseException) -> Optional[int]:
    """Best-effort HTTP status from a provider exception."""
    for attr in ("status_code", "http_status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


# Both of these arrive as HTTP 429, and they are opposites:
#
#   rate_limit_exceeded  - too many requests/tokens in the window. Wait, retry.
#   insufficient_quota   - the account is out of credit. Waiting changes nothing;
#                          only topping up the balance does.
#
# Treating the second as retryable is what made an out-of-credit account look
# like a flaky provider.
QUOTA_MARKERS = (
    "insufficient_quota",
    "exceeded your current quota",
    "billing_hard_limit_reached",
    "check your plan and billing",
)


def quota_exhausted(exc: BaseException) -> bool:
    """True when a 429 means 'no credit left' rather than 'too fast'."""
    if status_of(exc) != 429:
        return False
    blob = str(exc).lower()
    code = ""
    body = getattr(getattr(exc, "response", None), "json", None)
    if callable(body):
        try:
            code = str(((body() or {}).get("error") or {}).get("type", "")).lower()
        except Exception:  # noqa: BLE001
            code = ""
    return any(marker in blob or marker in code for marker in QUOTA_MARKERS)


def is_rate_limited(exc: BaseException) -> bool:
    """A genuine, waitable rate limit — 429 that is not a quota exhaustion."""
    return status_of(exc) == 429 and not quota_exhausted(exc)


_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|s|m|h)")


def _parse_duration(value: str) -> Optional[float]:
    """Parse OpenAI's rate-limit reset format: '20ms', '1s', '6m0s', '1h2m'."""
    total, matched = 0.0, False
    for amount, unit in _DURATION.findall(value.strip().lower()):
        matched = True
        total += float(amount) * {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}[unit]
    if matched:
        return total
    try:
        return float(value.strip())  # bare seconds, e.g. Retry-After: 30
    except (TypeError, ValueError):
        return None


def retry_after_seconds(exc: BaseException) -> Optional[float]:
    """How long the provider asked us to wait, if it said so.

    Backing off blindly ignores the answer the provider already gave us in
    `Retry-After` / `x-ratelimit-reset-*`.
    """
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if not headers:
        return None
    for name in ("retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
        raw = headers.get(name)
        if raw:
            seconds = _parse_duration(str(raw))
            if seconds is not None and seconds >= 0:
                return seconds
    return None


def is_retryable(exc: BaseException) -> bool:
    """True when another attempt could plausibly succeed.

    Transient: timeouts, connection resets, 429 rate limits, 5xx.
    Permanent: malformed request, bad credentials, unknown/inaccessible model,
    and an exhausted billing quota.
    """
    if quota_exhausted(exc):
        return False
    if type(exc).__name__ in NON_RETRYABLE_TYPES:
        return False
    status = status_of(exc)
    if status is not None and status in NON_RETRYABLE_STATUS:
        return False
    return True


def describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:300]}"


def is_unknown_model(exc: BaseException) -> bool:
    """True when the provider says the requested model does not exist for this key."""
    if status_of(exc) != 404:
        return False
    blob = str(exc).lower()
    return "model" in blob and ("not_found" in blob or "does not exist" in blob)


# Preferred chat models, best first, across both providers. Used only when the
# configured model is rejected and auto-fallback is on.
#
# Groq retired the Llama families from its catalogue; as of Aug 2026 its
# production chat models are the GPT-OSS pair plus the compound systems. A
# pinned model id is a perishable thing, which is why discovery reads the live
# catalogue rather than trusting this list alone.
CHAT_MODEL_PREFERENCES = (
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "groq/compound",
    "groq/compound-mini",
    "gpt-4o-mini",
    "gpt-4o",
)

# Never auto-select these: they are not general chat models.
MODEL_EXCLUDE_MARKERS = (
    "embedding", "whisper", "tts", "guard", "safeguard", "moderation",
    "dall-e", "vision-preview", "audio", "realtime", "rerank",
)


def pick_chat_model(available: List[str], configured: str = "") -> Optional[str]:
    """Choose a replacement chat model from a provider's catalogue.

    Preference order first; otherwise the first plausible chat model. Returns
    None when the catalogue holds nothing usable.
    """
    usable = [
        m for m in available
        if m != configured and not any(x in m.lower() for x in MODEL_EXCLUDE_MARKERS)
    ]
    for preferred in CHAT_MODEL_PREFERENCES:
        if preferred in usable:
            return preferred
    return usable[0] if usable else None


async def list_available_models(client: Any) -> List[str]:
    """Model ids this key can see. Empty list when the call is unsupported."""
    try:
        page = client.models.list()
        if asyncio.iscoroutine(page) or isinstance(page, asyncio.Future):
            page = await page
        return [m.id for m in getattr(page, "data", []) or []]
    except Exception as exc:  # noqa: BLE001
        log.warning("could not list provider models: %s", describe(exc))
        return []


async def discover_chat_model(client: Any, configured: str) -> Optional[str]:
    """Ask the provider what it actually serves, and pick a usable chat model.

    A pinned model id goes stale when a provider retires it — the request then
    404s forever with no code change on our side. Reading the live catalogue
    turns that permanent outage into a logged substitution.
    """
    available = await list_available_models(client)
    if not available:
        return None
    chosen = pick_chat_model(available, configured)
    if chosen:
        log.warning(
            "chat model %r is not available; falling back to %r "
            "(provider offers: %s). Set this in .env to make it explicit.",
            configured, chosen, ", ".join(sorted(available)[:12]),
        )
    else:
        log.error("chat model %r is not available and the catalogue holds no "
                  "usable replacement (saw: %s)", configured, ", ".join(sorted(available)[:12]))
    return chosen


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# Retry budget. Deliberately small: the pipeline is user-facing and a long
# retry ladder just converts a fast failure into a slow one.
LLM_MAX_ATTEMPTS = _env_int("LLM_MAX_ATTEMPTS", 3)
LLM_BACKOFF_BASE_S = _env_float("LLM_BACKOFF_BASE_S", 0.5)
LLM_BACKOFF_MAX_S = _env_float("LLM_BACKOFF_MAX_S", 4.0)
LLM_STUB_FALLBACK = env_flag("LLM_STUB_FALLBACK", False)
# When the configured chat model is rejected as unknown, read the provider's
# catalogue and continue with an available one rather than failing every query.
CHAT_MODEL_AUTO_FALLBACK = env_flag("CHAT_MODEL_AUTO_FALLBACK", True)


def backoff_delay(attempt: int, *, jitter: bool = True) -> float:
    """Exponential backoff for a 1-indexed attempt number, capped."""
    delay = min(LLM_BACKOFF_BASE_S * (2 ** (attempt - 1)), LLM_BACKOFF_MAX_S)
    if jitter:
        delay *= 0.5 + random.random() / 2.0
    return delay


async def achat(
    client: Any,
    *,
    model: str,
    prompt: str,
    max_tokens: int,
    temperature: float = 0.3,
    max_attempts: int = None,
    label: str = "chat",
) -> str:
    """Await one chat completion, retrying transient failures a bounded number of times.

    `client` must expose `chat.completions.create(...)` returning an awaitable
    (openai.AsyncOpenAI) or a plain result (the local stub client).

    Never mutates any global. On exhaustion raises LLMUnavailable.
    """
    attempts = max_attempts if max_attempts is not None else LLM_MAX_ATTEMPTS
    attempts = max(1, attempts)
    messages: List[dict] = [{"role": "user", "content": prompt}]
    last_error: Optional[BaseException] = None

    for attempt in range(1, attempts + 1):
        try:
            result = client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                response = await result
            else:  # local stub client returns a plain object
                response = result

            if not getattr(response, "choices", None):
                raise RuntimeError("chat provider returned no choices")
            return response.choices[0].message.content or ""

        except Exception as exc:  # noqa: BLE001
            last_error = exc

            if not is_retryable(exc):
                # The provider rejected the request itself. Say what to change
                # instead of burning the retry budget on a verdict that will not
                # change, and name the model so the log points at the culprit.
                log.error(
                    "llm[%s] permanent provider error on attempt %d, not retrying "
                    "(model=%r): %s", label, attempt, model, describe(exc),
                )
                hint = (
                    "The account is out of API credit. Retrying cannot fix this — top up the "
                    "billing balance for this key, or switch provider in .env."
                    if quota_exhausted(exc) else
                    "This is a configuration problem, not an outage — check GROQ_CHAT_MODEL / "
                    "OPENAI_CHAT_MODEL and the matching API key in .env, then rebuild the service."
                )
                raise LLMUnavailable(
                    f"chat provider rejected the request for model '{model}': {describe(exc)}. {hint}",
                    attempts=attempt,
                    last_error=exc,
                    permanent=True,
                ) from exc

            if attempt >= attempts:
                break
            asked = retry_after_seconds(exc)
            delay = min(asked, LLM_BACKOFF_MAX_S) if asked is not None else backoff_delay(attempt)
            log.warning(
                "llm[%s] attempt %d/%d failed (%s); retrying in %.2fs%s",
                label, attempt, attempts, describe(exc), delay,
                " (provider asked)" if asked is not None else "",
            )
            await asyncio.sleep(delay)

    raise LLMUnavailable(
        f"chat provider unavailable after {attempts} attempt(s): {describe(last_error)}",
        attempts=attempts,
        last_error=last_error,
    )
