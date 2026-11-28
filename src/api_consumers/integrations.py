"""Callbacks de la rotación. 3scale está en `threescale.py` (applications, rotación e importación)."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time

import httpx

log = logging.getLogger(__name__)


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def deliver_callback(url: str, payload: dict, secret: str, retries: int, timeout_s: float) -> bool:
    body = json.dumps(payload, separators=(",", ":"), default=str).encode()
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Api-Consumers-Signature"] = sign(body, secret)
    for attempt in range(1, retries + 1):
        try:
            r = httpx.post(url, content=body, headers=headers, timeout=timeout_s)
            if 200 <= r.status_code < 300:
                return True
            log.warning("callback %s respondió %s (intento %d)", url, r.status_code, attempt)
        except httpx.HTTPError as e:
            log.warning("callback %s falló: %s (intento %d)", url, type(e).__name__, attempt)
        time.sleep(min(0.5 * 2**attempt, 10))
    return False
