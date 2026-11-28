"""Cliente del API Subscriber (ADR API Consumers D4/D5): suscripciones vivas y ventana de rotación.

La API Consumers orquesta la rotación; el Subscriber materializa la ventana en Git (`-prev` fijados a una versión de
KV v2) y confirma con su gate. Nunca viaja una key: solo el número de versión.

0.5.0 (27/11): una API Consumers por tier atiende a un API Subscriber por ambiente (paas-svc-prdpg, kuad-toolkit-low:
dev, int y qas). `subscriber_endpoints` del Secret ({ambiente: {url, token}}) elige el de cada credencial
(`SubscriberRouter.for_env`); sin la clave, el `subscriber_url` de siempre atiende a todos.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Protocol

import httpx

from .config import Settings
from .errors import Problem

log = logging.getLogger(__name__)


class Subscriber(Protocol):
    """La credencial es por namespace y tenant (secret-apim-<tenant>-v2): la ventana de rotación también (Subscriber
    0.6.0 en adelante)."""

    def list_subscriptions(self, namespace: str) -> list[dict]: ...
    def open_overlap(
        self, namespace: str, tenant: str, rotation_id: str, vault_version: int, idem_key: str
    ) -> dict: ...
    def close_overlap(self, namespace: str, tenant: str, idem_key: str) -> dict | None:
        """Operación de cierre, o None si no había ventana abierta (404)."""

    def refresh(self, namespace: str, tenant: str) -> bool: ...
    def operation(self, op_id: str) -> dict: ...
    def for_env(self, environment: str | None) -> Subscriber:
        """El Subscriber del ambiente (el mismo, si hay uno solo)."""


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

    def for_env(self, environment: str | None) -> HttpSubscriber:
        return self

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

    def open_overlap(self, namespace: str, tenant: str, rotation_id: str, vault_version: int, idem_key: str) -> dict:
        r = self._req(
            "overlap",
            "PUT",
            f"/v1/credential-overlaps/{namespace}",
            json={"tenant": tenant, "rotationId": rotation_id, "vaultVersion": vault_version},
            headers={"Idempotency-Key": idem_key},
        )
        if r.status_code == 409:
            raise Problem(409, "Conflict", f"el API Subscriber rechazó la ventana: {r.json().get('detail', '')}")
        if r.status_code != 202:
            raise _unavailable("overlap", f"HTTP {r.status_code}")
        return r.json()

    def close_overlap(self, namespace: str, tenant: str, idem_key: str) -> dict | None:
        r = self._req(
            "overlap",
            "DELETE",
            f"/v1/credential-overlaps/{namespace}",
            params={"tenant": tenant},
            headers={"Idempotency-Key": idem_key},
        )
        if r.status_code == 404:
            return None
        if r.status_code != 202:
            raise _unavailable("overlap", f"HTTP {r.status_code}")
        return r.json()

    def refresh(self, namespace: str, tenant: str) -> bool:
        r = self._req(
            "refresh",
            "POST",
            f"/v1/credential-overlaps/{namespace}/refresh",
            params={"timeoutSeconds": self.refresh_timeout_s, "tenant": tenant},
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
        self.subs: dict[str, int] = {}  # namespace → suscripciones vivas (tenant b2c)
        self.overlaps: dict[tuple[str, str], tuple[str, int]] = {}  # (namespace, tenant) → (rotación, versión)
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
        return [{"id": f"sub-x{i}--{namespace}", "tenant": "b2c"} for i in range(self.subs.get(namespace, 0))]

    def open_overlap(self, namespace: str, tenant: str, rotation_id: str, vault_version: int, idem_key: str) -> dict:
        self.calls.append(("open", namespace, tenant, rotation_id, vault_version))
        cur = self.overlaps.get((namespace, tenant))
        if cur and cur[0] != rotation_id:
            raise Problem(409, "Conflict", "otra ventana abierta")
        if not self.fail_open:
            self.overlaps[(namespace, tenant)] = (rotation_id, vault_version)
        return self._op("overlapOpen", namespace, not self.fail_open)

    def close_overlap(self, namespace: str, tenant: str, idem_key: str) -> dict | None:
        self.calls.append(("close", namespace, tenant))
        if (namespace, tenant) not in self.overlaps:
            return None
        if not self.fail_close:
            del self.overlaps[(namespace, tenant)]
        return self._op("overlapClose", namespace, not self.fail_close)

    def refresh(self, namespace: str, tenant: str) -> bool:
        self.calls.append(("refresh", namespace, tenant))
        return self.refresh_ok

    def operation(self, op_id: str) -> dict:
        return self.ops[op_id]

    def for_env(self, environment: str | None) -> FakeSubscriber:
        return self


class SubscriberRouter:
    """Un API Subscriber por ambiente de Vault (`dev`, `int`, `qas`, `prd`) y, opcional, uno por default."""

    def __init__(
        self,
        by_env: dict[str, Subscriber],
        default: Subscriber | None = None,
        vault_env: Callable[[str | None], str | None] = lambda e: e,
    ):
        self.by_env = by_env
        self.default = default
        self._vault_env = vault_env

    def for_env(self, environment: str | None) -> Subscriber:
        env = self._vault_env(environment) or (environment or "").strip().lower()
        sub = self.by_env.get(env) or self.default
        if sub is None:
            raise Problem(
                422,
                "Unprocessable Entity",
                f"sin API Subscriber para el ambiente {environment or 'ausente'} "
                f"(APICON_SUBSCRIBER_ENDPOINTS: {', '.join(sorted(self.by_env)) or 'ninguno'})",
            )
        return sub

    # Sin ambiente (no se usa desde Service): el default
    def _one(self) -> Subscriber:
        return self.for_env(None)

    def list_subscriptions(self, namespace: str) -> list[dict]:
        return self._one().list_subscriptions(namespace)

    def open_overlap(self, namespace: str, tenant: str, rotation_id: str, vault_version: int, idem_key: str) -> dict:
        return self._one().open_overlap(namespace, tenant, rotation_id, vault_version, idem_key)

    def close_overlap(self, namespace: str, tenant: str, idem_key: str) -> dict | None:
        return self._one().close_overlap(namespace, tenant, idem_key)

    def refresh(self, namespace: str, tenant: str) -> bool:
        return self._one().refresh(namespace, tenant)

    def operation(self, op_id: str) -> dict:
        return self._one().operation(op_id)


def build_subscriber(s: Settings) -> Subscriber:
    if s.subscriber_mode == "fake":
        return FakeSubscriber()
    verify: bool | str = s.subscriber_ca_file or s.subscriber_verify

    def http(url: str, token: str) -> HttpSubscriber:
        return HttpSubscriber(
            url,
            token,
            verify=verify,
            timeout_s=s.subscriber_timeout_s,
            refresh_timeout_s=s.subscriber_refresh_timeout_s,
        )

    raw = json.loads(s.subscriber_endpoints) if s.subscriber_endpoints.strip() else {}
    if not raw:
        return http(s.subscriber_url, s.subscriber_token)
    by_env: dict[str, Subscriber] = {}
    for env, ep in raw.items():
        if not (ep or {}).get("url"):
            raise ValueError(f"APICON_SUBSCRIBER_ENDPOINTS: {env} sin url")
        by_env[s.vault_env(env) or env.strip().lower()] = http(ep["url"], ep.get("token") or "")
    default = http(s.subscriber_url, s.subscriber_token) if s.subscriber_url else None
    log.info("API Subscriber por ambiente: %s%s", ", ".join(sorted(by_env)), " + default" if default else "")
    return SubscriberRouter(by_env, default, s.vault_env)
