"""Lógica de la API Consumers (ADR-api-consumers-y-subscriber-sin-estado, D1/D3/D4).

- Dueña de la credencial del consumidor: alta, baja y rotación en Vault (`apim-<tier>/consumers/<ns>/credentials`).
- **Escribe sin leer keys:** solo genera la key, la escribe con check-and-set y la olvida. El `app_id` y todo el estado
  (consumidor y rotación) viven en la `custom_metadata` del secreto. Sin base de datos.
- **Rotación coordinada con el API Subscriber:** (1) fija la versión N vigente; (2) el Subscriber abre la ventana
  (`-prev` fijados a N) y confirma; (3) recién ahí se escribe N+1 con `cas=N` y se pide el refresh de los Secrets
  principales; (4) al completar, el Subscriber cierra la ventana. Abort solo antes de (3) (roll-forward).
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

from . import render
from .config import Settings
from .errors import Problem, conflict, not_found, not_implemented, unprocessable
from .integrations import ThreeScale, deliver_callback
from .subscriber import Subscriber
from .vault import CasMismatch, Vault, fingerprint, generate_app_id, generate_app_key, now

log = logging.getLogger(__name__)

ROT_KEYS = (
    "rotation_id",
    "rotation_state",
    "rotation_pinned_version",
    "rotation_new_version",
    "rotation_op",
    "rotation_deadline",
    "rotation_started_at",
    "rotation_updated_at",
    "rotation_error",
    "rotation_callback",
    "rotation_idem",
    "rotation_hash",
)
TERMINAL = {"Completed", "Aborted", "Failed"}
DRIVEN = {"Requested", "OverlapOpening", "KeyWritten", "Completing", "Aborting"}
CALLBACK_STATES = {"AwaitingConsumer", "Completed", "Aborted", "Failed"}


def _hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


class Runner:
    def __init__(self, mode: str, threads: int):
        self._pool = ThreadPoolExecutor(max_workers=threads, thread_name_prefix="rot") if mode == "background" else None

    def submit(self, fn: Callable[..., None], *args: Any) -> None:
        if self._pool is None:
            fn(*args)
        else:
            self._pool.submit(self._safe, fn, *args)

    @staticmethod
    def _safe(fn: Callable[..., None], *args: Any) -> None:
        try:
            fn(*args)
        except Exception:
            log.exception("tarea %s falló", fn.__name__)

    def shutdown(self) -> None:
        if self._pool:
            self._pool.shutdown(wait=False, cancel_futures=True)


class Service:
    def __init__(
        self, settings: Settings, vault: Vault, subscriber: Subscriber, threescale: ThreeScale, runner: Runner
    ):
        self.s = settings
        self.vault = vault
        self.subscriber = subscriber
        self.threescale = threescale
        self.runner = runner
        self._ns_locks: dict[str, threading.RLock] = defaultdict(threading.RLock)
        self._guard = threading.Lock()
        self._driving: set[str] = set()

    # ================================================================ helpers
    def _lock(self, ns: str) -> threading.RLock:
        with self._guard:
            return self._ns_locks[ns]

    @staticmethod
    def _path(ns: str) -> str:
        return render.credentials_key(ns)

    def _meta(self, ns: str) -> dict | None:
        return self.vault.metadata(self._path(ns))

    def _index(self, kind: str, value: str, ns: str) -> bool:
        """Unicidad atómica: crea `<index_prefix>/<kind>/<value>` con cas=0. False si ya existía."""
        try:
            self.vault.write(f"{self.s.index_prefix}/{kind}/{value}", {"namespace": ns, "created_at": now()}, cas=0)
            return True
        except CasMismatch:
            return False

    # ================================================================== views
    def consumer_view(self, ns: str, md: dict, manifest: str | None = None) -> dict:
        c = md["custom"]
        rot = c.get("rotation_id") if c.get("rotation_state") not in TERMINAL else None
        state = "Revoked" if c.get("state") == "revoked" else "Rotating" if rot else "Active"
        return _drop_none(
            {
                "namespace": ns,
                "sigla": c.get("sigla"),
                "tier": c.get("tier"),
                "owner": c.get("owner"),
                "appId": c.get("app_id"),
                "vaultPath": f"{self.s.mount}/{self._path(ns)}",
                "vaultVersion": md["current_version"],
                "state": state,
                "origin": c.get("origin"),
                "createdAt": c.get("created_at"),
                "rotatedAt": c.get("rotated_at"),
                "rotationId": rot,
                "credentialSync": {"threescale": c.get("threescale_state", "NotApplicable")},
                "secretApimManifest": manifest,
            }
        )

    def rotation_view(self, ns: str, md: dict) -> dict:
        c = md["custom"]
        err = c.get("rotation_error")
        return _drop_none(
            {
                "id": c.get("rotation_id"),
                "namespace": ns,
                "state": c.get("rotation_state"),
                "pinnedVersion": int(c["rotation_pinned_version"]) if c.get("rotation_pinned_version") else None,
                "newVersion": int(c["rotation_new_version"]) if c.get("rotation_new_version") else None,
                "subscriberOperationId": c.get("rotation_op"),
                "deadline": c.get("rotation_deadline"),
                "startedAt": c.get("rotation_started_at"),
                "updatedAt": c.get("rotation_updated_at"),
                "error": Problem(409, "Rotation Error", err).to_dict() if err else None,
            }
        )

    def secret_apim(self, ns: str, md: dict | None = None) -> str:
        md = md or self._meta(ns)
        if not md or md["custom"].get("state") == "revoked":
            raise not_found(f"consumidor {ns}")
        return render.secret_apim_externalsecret(
            consumer_ns=ns,
            app_id=md["custom"].get("app_id", ""),
            vault_key=self._path(ns),
            store_kind=self.s.consumer_store_kind,
            store_name=self.s.consumer_store,
            refresh=self.s.secret_apim_refresh,
            vault_path=f"{self.s.mount}/{self._path(ns)}",
        )

    # ============================================================= consumidor
    def create_consumer(self, req: dict, idem_key: str) -> tuple[int, dict]:
        ns = req["namespace"]
        h = _hash(req)
        ts = req.get("threescale") or {}
        if ts.get("syncCredentials") and self.s.threescale_mode == "disabled":
            raise not_implemented("sincronización con 3scale no habilitada (APICON_THREESCALE_MODE)")
        with self._lock(ns):
            md = self._meta(ns)
            if md and md["custom"].get("state") != "revoked":
                c = md["custom"]
                if c.get("idempotency_key") == idem_key:
                    if c.get("request_hash") != h:
                        raise unprocessable("Idempotency-Key reutilizada con un payload distinto")
                    return 200, self.consumer_view(ns, md, self.secret_apim(ns, md))
                raise conflict(f"el namespace {ns} ya tiene credencial")

            imp = req.get("importFrom3scale")
            if imp:
                app_id, app_key = self.threescale.get_credentials(imp.get("tenant", ""), imp.get("applicationId", ""))
                if not self._index("app-ids", app_id, ns):
                    raise conflict(f"el App ID importado {app_id} ya está en uso por otro consumidor")
                if not self._index("key-fp", fingerprint(app_key), ns):
                    raise conflict("la credencial importada colisiona con la de otro consumidor (H-03)")
            else:
                for _ in range(10):
                    app_id = generate_app_id()
                    if self._index("app-ids", app_id, ns):
                        break
                else:  # pragma: no cover - 2^32 ids
                    raise Problem(503, "Unavailable", "no se pudo reservar un App ID")
                while True:
                    app_key = generate_app_key()
                    if self._index("key-fp", fingerprint(app_key), ns):
                        break

            self.vault.write(
                self._path(ns), {"app_id": app_id, "app_key": app_key}, cas=md["current_version"] if md else 0
            )
            ts_meta: dict[str, str] = {"threescale_state": "NotApplicable"}
            if ts.get("syncCredentials"):
                ts_meta = {"threescale_tenant": ts.get("tenant"), "threescale_app": ts.get("applicationId")}
                try:
                    self.threescale.set_credentials(ts.get("tenant", ""), ts.get("applicationId", ""), app_id, app_key)
                    ts_meta["threescale_state"] = "InSync"
                except Problem as e:
                    ts_meta["threescale_state"] = "Failed"
                    log.warning("sync 3scale falló para %s: %s", ns, e.detail)
            del app_key
            self.vault.set_metadata(
                self._path(ns),
                {
                    "namespace": ns,
                    "sigla": req["sigla"],
                    "tier": req["tier"],
                    "owner": req.get("owner"),
                    "servicenow_ref": req.get("servicenowRef"),
                    "app_id": app_id,
                    "state": "active",
                    "origin": "3scale-import" if imp else "generated",
                    "created_at": now(),
                    "created_by": "api-consumers",
                    "idempotency_key": idem_key,
                    "request_hash": h,
                    **ts_meta,
                },
                drop={*ROT_KEYS, "revoked_at", "rotated_at"},
            )
            md = self._meta(ns)
            return 201, self.consumer_view(ns, md, self.secret_apim(ns, md))

    def get_consumer(self, ns: str) -> dict:
        md = self._meta(ns)
        if not md:
            raise not_found(f"consumidor {ns}")
        return self.consumer_view(ns, md)

    def delete_consumer(self, ns: str, idem_key: str) -> dict:
        with self._lock(ns):
            md = self._meta(ns)
            if not md:
                raise not_found(f"consumidor {ns}")
            c = md["custom"]
            if c.get("state") == "revoked":
                return self.consumer_view(ns, md)
            if c.get("rotation_id") and c.get("rotation_state") not in TERMINAL:
                raise conflict(f"{ns} tiene una rotación en curso ({c.get('rotation_state')})")
            if self.subscriber.list_subscriptions(ns):
                raise conflict(f"{ns} tiene suscripciones vivas: darlas de baja antes")
            # tombstone sin leer la key: el app_id sale de la metadata (la policy no tiene delete)
            self.vault.write(
                self._path(ns), {"app_id": c.get("app_id", ""), "revoked_at": now()}, cas=md["current_version"]
            )
            self.vault.set_metadata(
                self._path(ns), {"state": "revoked", "revoked_at": now(), "revoke_idempotency_key": idem_key}
            )
            return self.consumer_view(ns, self._meta(ns))

    # ============================================================== rotación
    def _set_rot(self, ns: str, **kw: Any) -> None:
        """Actualiza claves rotation_* de la metadata; un valor vacío la borra (Vault no admite vacíos)."""
        vals = {f"rotation_{k}": v for k, v in kw.items()}
        drop = {k for k, v in vals.items() if v in ("", None)}
        keep = {k: v for k, v in vals.items() if k not in drop}
        self.vault.set_metadata(self._path(ns), {**keep, "rotation_updated_at": now()}, drop=drop)

    def _fail(self, ns: str, msg: str) -> None:
        log.warning("rotación de %s → Failed: %s", ns, msg)
        self._set_rot(ns, state="Failed", error=msg)
        self._callback(ns)

    def _callback(self, ns: str) -> None:
        md = self._meta(ns)
        url = md["custom"].get("rotation_callback") if md else None
        if url and md["custom"].get("rotation_state") in CALLBACK_STATES:
            deliver_callback(
                url,
                self.rotation_view(ns, md),
                self.s.callback_secret,
                self.s.callback_retries,
                self.s.callback_timeout_s,
            )

    def start_rotation(self, ns: str, req: dict, idem_key: str) -> dict:
        payload = {"ns": ns, **req}
        h = _hash(payload)
        with self._lock(ns):
            md = self._meta(ns)
            if not md:
                raise not_found(f"consumidor {ns}")
            c = md["custom"]
            if c.get("state") == "revoked":
                raise conflict(f"{ns} está revocado")
            if c.get("rotation_id") and c.get("rotation_idem") == idem_key:
                if c.get("rotation_hash") != h:
                    raise unprocessable("Idempotency-Key reutilizada con un payload distinto")
                return self.rotation_view(ns, md)
            if c.get("rotation_id") and c.get("rotation_state") not in TERMINAL:
                raise conflict(f"{ns} ya tiene una rotación en curso ({c.get('rotation_state')})")
            ts = now()
            deadline = (datetime.now(timezone.utc) + timedelta(days=self.s.rotation_deadline_days)).isoformat(
                timespec="seconds"
            )
            self.vault.set_metadata(
                self._path(ns),
                {
                    "rotation_id": str(uuid.uuid4()),
                    "rotation_state": "Requested",
                    "rotation_pinned_version": md["current_version"],
                    "rotation_deadline": deadline.replace("+00:00", "Z"),
                    "rotation_started_at": ts,
                    "rotation_updated_at": ts,
                    "rotation_callback": req.get("callbackUrl"),
                    "rotation_idem": idem_key,
                    "rotation_hash": h,
                },
                drop=set(ROT_KEYS),
            )
        self.runner.submit(self._drive, ns)
        return self.rotation_view(ns, self._meta(ns))

    def get_rotation(self, ns: str, rotation_id: str) -> dict:
        md = self._meta(ns)
        if not md or md["custom"].get("rotation_id") != rotation_id:
            raise not_found(f"rotación {rotation_id}")
        return self.rotation_view(ns, md)

    def update_rotation(self, ns: str, rotation_id: str, action: str) -> dict:
        with self._lock(ns):
            md = self._meta(ns)
            if not md or md["custom"].get("rotation_id") != rotation_id:
                raise not_found(f"rotación {rotation_id}")
            st = md["custom"].get("rotation_state")
            if action == "complete":
                if st == "Completed":
                    return self.rotation_view(ns, md)
                if st not in ("AwaitingConsumer", "Completing"):
                    raise conflict(f"la rotación está en {st}: complete solo desde AwaitingConsumer")
                self._set_rot(ns, state="Completing", error="")
            else:
                if st == "Aborted":
                    return self.rotation_view(ns, md)
                if st not in ("Requested", "OverlapOpening", "Aborting"):
                    raise conflict(
                        f"la rotación está en {st}: después de escribir la key nueva no hay abort sin leer la vieja; "
                        "completarla y, si hace falta, rotar de nuevo (roll-forward)"
                    )
                self._set_rot(ns, state="Aborting", error="")
        self.runner.submit(self._drive, ns)
        return self.rotation_view(ns, self._meta(ns))

    def _wait(self, op_id: str) -> str:
        """Espera una operación del API Subscriber hasta un estado final o el timeout del paso."""
        deadline = time.monotonic() + self.s.rotation_step_timeout_s
        while True:
            st = self.subscriber.operation(op_id).get("state")
            if st in ("Succeeded", "Failed", "PartiallySynced"):
                return st
            if time.monotonic() >= deadline:
                return "Timeout"
            time.sleep(self.s.rotation_poll_s)

    def _drive(self, ns: str) -> None:
        """Avanza la máquina de estados de la rotación hasta un estado de espera o final."""
        with self._guard:
            if ns in self._driving:
                return
            self._driving.add(ns)
        try:
            for _ in range(10):
                md = self._meta(ns)
                c = md["custom"]
                st, rid = c.get("rotation_state"), c.get("rotation_id")
                if st == "Requested":
                    try:
                        op = self.subscriber.open_overlap(ns, rid, int(c["rotation_pinned_version"]), f"{rid}-open")
                    except Problem as e:
                        if e.status == 409:
                            self._fail(ns, e.detail)
                            return
                        raise
                    with self._lock(ns):
                        if self._meta(ns)["custom"].get("rotation_state") == "Requested":
                            self._set_rot(ns, state="OverlapOpening", op=op["id"])
                elif st == "OverlapOpening":
                    res = self._wait(c["rotation_op"])
                    if not self._write_new_key(ns, res):
                        continue
                elif st == "KeyWritten":
                    ok = False
                    try:
                        ok = self.subscriber.refresh(ns)
                    except Problem as e:
                        log.warning("refresh de %s: %s", ns, e.detail)
                    note = "" if ok else "refresh no confirmado: el Secret principal toma la key en su refreshInterval"
                    self._set_rot(ns, state="AwaitingConsumer", error=note)
                    self._callback(ns)
                    return
                elif st in ("Completing", "Aborting"):
                    op = self.subscriber.close_overlap(ns, f"{rid}-{'close' if st == 'Completing' else 'abort'}")
                    res = self._wait(op["id"]) if op else "Succeeded"
                    if res != "Succeeded":
                        self._set_rot(ns, error=f"el API Subscriber no confirmó el cierre de la ventana ({res})")
                        return
                    if st == "Completing":
                        self.vault.set_metadata(self._path(ns), {"rotated_at": now()})
                    self._set_rot(ns, state="Completed" if st == "Completing" else "Aborted", error="")
                    self._callback(ns)
                    return
                else:
                    return
        except Problem as e:  # API Subscriber caído: el estado queda para reintentar (PATCH o recover)
            log.warning("rotación de %s en pausa: %s", ns, e.detail)
            self._set_rot(ns, error=e.detail)
        finally:
            with self._guard:
                self._driving.discard(ns)

    def _write_new_key(self, ns: str, overlap_state: str) -> bool:
        """Paso 3 de D4. Devuelve True si avanzó; False si la rotación cambió de estado (p. ej. abort) o falló."""
        with self._lock(ns):
            md = self._meta(ns)
            c = md["custom"]
            if c.get("rotation_state") != "OverlapOpening":
                return False  # abortada mientras se esperaba al Subscriber
            if overlap_state != "Succeeded":
                self._fail(ns, f"la ventana no se confirmó ({overlap_state}); no se escribió la key nueva")
                return False
            pinned = int(c["rotation_pinned_version"])
            v = md["versions"].get(pinned)
            if not v or v["destroyed"] or v["deleted"]:
                self._fail(ns, f"la versión fijada {pinned} ya no existe en Vault (max_versions)")
                return False
            app_key = generate_app_key()
            self._index("key-fp", fingerprint(app_key), ns)
            try:
                new = self.vault.write(self._path(ns), {"app_id": c.get("app_id", ""), "app_key": app_key}, cas=pinned)
            except CasMismatch:
                self._fail(ns, f"la credencial cambió en Vault después de fijar la versión {pinned}")
                return False
            if c.get("threescale_state") in ("InSync", "Failed"):
                try:
                    self.threescale.set_credentials(
                        c.get("threescale_tenant", ""), c.get("threescale_app", ""), c.get("app_id", ""), app_key
                    )
                    self.vault.set_metadata(self._path(ns), {"threescale_state": "InSync"})
                except Problem as e:
                    self.vault.set_metadata(self._path(ns), {"threescale_state": "Failed"})
                    log.warning("sync 3scale falló para %s: %s", ns, e.detail)
            del app_key
            self._set_rot(ns, state="KeyWritten", new_version=new)
            return True

    def recover(self) -> None:
        """Al arrancar: retoma las rotaciones que quedaron en un paso automático."""
        for entry in self.vault.list("consumers"):
            ns = entry.rstrip("/")
            if not entry.endswith("/") or ns.startswith("_"):
                continue
            md = self._meta(ns)
            if md and md["custom"].get("rotation_state") in DRIVEN:
                self.runner.submit(self._drive, ns)
