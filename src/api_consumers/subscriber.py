"""Cliente del API Subscriber (ADR API Consumers D4/D5): suscripciones vivas y ventana de rotación.

La API Consumers orquesta la rotación; el Subscriber materializa la ventana en Git (`-prev` fijados a una versión de
KV v2) y confirma con su gate. Nunca viaja una key: solo el número de versión.
"""

from __future__ import annotations

import logging
from typing import Protocol

import httpx

from .config import Settings
from .errors import Problem

log = logging.getLogger(__name__)


class Subscriber(Protocol):
    def list_subscriptions(self, namespace: str) -> list[dict]: ...
    def open_overlap(self, namespace: str, rotation_id: str, vault_version: int, idem_key: str) -> dict: ...
    def close_overlap(self, namespace: str, idem_key: str) -> dict | None:
        """Operación de cierre, o None si no había ventana abierta (404)."""

    def refresh(self, namespace: str) -> bool: ...
    def operation(self, op_id: str) -> dict: ...


def _unavailable(what: str, detail: str) -> Problem:
    return Problem(503, "Subscriber Unavailable", f"API Subscriber ({what}): {detail}")


class HttpSubscriber:
    def __init__(
        self,
        url: str,
        token: str,
        verify: bool | str = True,
        timeout_s: float = 15,
        refresh_timeout_s: int = 60,
        transport=None,
    ):
        if not url:
            raise ValueError("APICON_SUBSCRIBER_URL es obligatorio con APICON_SUBSCRIBER_MODE=http")
        self.refresh_timeout_s = refresh_timeout_s
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._http = httpx.Client(
            base_url=url.rstrip("/"), headers=headers, verify=verify, timeout=timeout_s, transport=transport
        )

    def _req(self, what: str, method: str, path: str, **kw) -> httpx.Response:
        try:
            return self._http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise _unavailable(what, type(e).__name__) from e

    def list_subscriptions(self, namespace: str) -> list[dict]:
        r = self._req("list", "GET", "/v1/subscriptions", params={"consumerNamespace": namespace})
        if r.status_code != 200:
            raise _unavailable("list", f"HTTP {r.status_code}")
        return r.json().get("items") or []

    def open_overlap(self, namespace: str, rotation_id: str, vault_version: int, idem_key: str) -> dict:
        r = self._req(
            "overlap",
            "PUT",
            f"/v1/credential-overlaps/{namespace}",
            json={"rotationId": rotation_id, "vaultVersion": vault_version},
            headers={"Idempotency-Key": idem_key},
        )
        if r.status_code == 409:
            raise Problem(409, "Conflict", f"el API Subscriber rechazó la ventana: {r.json().get('detail', '')}")
        if r.status_code != 202:
            raise _unavailable("overlap", f"HTTP {r.status_code}")
        return r.json()

    def close_overlap(self, namespace: str, idem_key: str) -> dict | None:
        r = self._req(
            "overlap", "DELETE", f"/v1/credential-overlaps/{namespace}", headers={"Idempotency-Key": idem_key}
        )
        if r.status_code == 404:
            return None
        if r.status_code != 202:
            raise _unavailable("overlap", f"HTTP {r.status_code}")
        return r.json()

    def refresh(self, namespace: str) -> bool:
        r = self._req(
            "refresh",
            "POST",
            f"/v1/credential-overlaps/{namespace}/refresh",
            params={"timeoutSeconds": self.refresh_timeout_s},
            timeout=self.refresh_timeout_s + 15,
        )
        if r.status_code not in (200, 504):
            raise _unavailable("refresh", f"HTTP {r.status_code}")
        return r.status_code == 200

    def operation(self, op_id: str) -> dict:
        r = self._req("operation", "GET", f"/v1/operations/{op_id}")
        if r.status_code != 200:
            raise _unavailable("operation", f"HTTP {r.status_code}")
        return r.json()


class FakeSubscriber:
    """Tests y desarrollo: simula el API Subscriber (operaciones que terminan al instante)."""

    def __init__(self) -> None:
        self.subs: dict[str, int] = {}  # namespace → suscripciones vivas
        self.overlaps: dict[str, tuple[str, int]] = {}
        self.ops: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.fail_open = False
        self.fail_close = False
        self.refresh_ok = True

    def _op(self, kind: str, target: str, ok: bool) -> dict:
        op = {"id": f"op-{len(self.ops) + 1}", "kind": kind, "target": target, "state": "Succeeded" if ok else "Failed"}
        self.ops[op["id"]] = op
        return op

    def list_subscriptions(self, namespace: str) -> list[dict]:
        return [{"id": f"sub-x{i}--{namespace}"} for i in range(self.subs.get(namespace, 0))]

    def open_overlap(self, namespace: str, rotation_id: str, vault_version: int, idem_key: str) -> dict:
        self.calls.append(("open", namespace, rotation_id, vault_version))
        cur = self.overlaps.get(namespace)
        if cur and cur[0] != rotation_id:
            raise Problem(409, "Conflict", "otra ventana abierta")
        if not self.fail_open:
            self.overlaps[namespace] = (rotation_id, vault_version)
        return self._op("overlapOpen", namespace, not self.fail_open)

    def close_overlap(self, namespace: str, idem_key: str) -> dict | None:
        self.calls.append(("close", namespace))
        if namespace not in self.overlaps:
            return None
        if not self.fail_close:
            del self.overlaps[namespace]
        return self._op("overlapClose", namespace, not self.fail_close)

    def refresh(self, namespace: str) -> bool:
        self.calls.append(("refresh", namespace))
        return self.refresh_ok

    def operation(self, op_id: str) -> dict:
        return self.ops[op_id]


def build_subscriber(s: Settings) -> Subscriber:
    if s.subscriber_mode == "fake":
        return FakeSubscriber()
    verify: bool | str = s.subscriber_ca_file or s.subscriber_verify
    return HttpSubscriber(
        s.subscriber_url,
        s.subscriber_token,
        verify=verify,
        timeout_s=s.subscriber_timeout_s,
        refresh_timeout_s=s.subscriber_refresh_timeout_s,
    )
