"""
shared/utils/helpers.py
Common utilities used across all services.
"""

import logging
import uuid
from datetime import datetime
from typing import Any, Dict

import httpx


# ── Logger ────────────────────────────────────────────────────

def get_logger(service_name: str) -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format=f"[%(asctime)s] [{service_name}] %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger(service_name)


# ── ID Generation ─────────────────────────────────────────────

def generate_id(prefix: str = "") -> str:
    uid = str(uuid.uuid4()).replace("-", "")[:16]
    return f"{prefix}_{uid}" if prefix else uid


# ── HTTP Client ───────────────────────────────────────────────

async def call_service(
    url: str,
    method: str = "POST",
    payload: Dict[str, Any] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    """Generic async HTTP call between microservices."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        if method == "POST":
            response = await client.post(url, json=payload)
        elif method == "GET":
            response = await client.get(url, params=payload)
        else:
            raise ValueError(f"Unsupported method: {method}")
        response.raise_for_status()
        return response.json()


# ── Date Helpers ──────────────────────────────────────────────

def parse_date(date_str: str) -> datetime:
    formats = [
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y",
        "%B %d, %Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date: {date_str}")


def format_date_display(dt: datetime) -> str:
    return dt.strftime("%B %d, %Y")


# ── Text Helpers ──────────────────────────────────────────────

def truncate(text: str, max_len: int = 200) -> str:
    return text[:max_len] + "..." if len(text) > max_len else text


def clean_text(text: str) -> str:
    """Remove excessive whitespace and normalize."""
    import re
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r' {2,}', ' ', text)
    return text.strip()