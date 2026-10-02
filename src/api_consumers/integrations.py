"""3scale (convivencia) y callbacks. En el MVP, 3scale es un stub con la interfaz definitiva."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Protocol

import httpx

from .config import Settings
from .errors import not_implemented, unprocessable

log = logging.getLogger(__name__)


class ThreeScale(Protocol):
    def get_credentials(self, tenant: str, application_id: str) -> tuple[str, str]: ...
    def set_credentials(self, tenant: str, application_id: str, app_id: str, app_key: str) -> None: ...


class ThreeScaleDisabled:
    def get_credentials(self, tenant: str, application_id: str) -> tuple[str, str]:
        raise not_implemented("importación desde 3scale no habilitada en esta versión (APICON_THREESCALE_MODE)")

    def set_credentials(self, tenant: str, application_id: str, app_id: str, app_key: str) -> None:
        raise not_implemented("sincronización de credenciales con 3scale no habilitada (APICON_THREESCALE_MODE)")


class ThreeScaleStub:
    """Tests/e2e: guarda en memoria lo que se escribiría en 3scale."""

    def __init__(self) -> None:
        self.apps: dict[tuple[str, str], tuple[str, str]] = {}

    def get_credentials(self, tenant: str, application_id: str) -> tuple[str, str]:
        if (tenant, application_id) not in self.apps:
            raise unprocessable(f"aplicación {application_id} no existe en 3scale ({tenant})")
        return self.apps[(tenant, application_id)]

    def set_credentials(self, tenant: str, application_id: str, app_id: str, app_key: str) -> None:
        self.apps[(tenant, application_id)] = (app_id, app_key)


def build_threescale(s: Settings) -> ThreeScale:
    return ThreeScaleStub() if s.threescale_mode == "stub" else ThreeScaleDisabled()


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
