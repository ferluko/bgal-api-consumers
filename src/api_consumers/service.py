"""Lógica de la API Consumers (ADR-api-consumers-y-subscriber-sin-estado, D1/D3/D4).

- Dueña de la credencial del consumidor: alta, baja y rotación en Vault. Una credencial por namespace y tenant, con la
  convención de DevSecOps: `openshift-<ambiente>/<ns>/secret-apim-<tenant>-v2`, claves `app_id_<tenant>` y
  `app_key_<tenant>` (06/10 y 07/10).
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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from . import render
from .config import TENANTS, Settings
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


@dataclass(frozen=True)
class Cred:
    """Una credencial: namespace × ambiente × tenant. `path` es el path completo en Vault (con el mount)."""

    namespace: str
    environment: str  # como lo informa el pedido (QA, PRD…)
    tenant: str
    path: str
    app_id_field: str
    app_key_field: str

    @property
    def label(self) -> str:
        return f"{self.namespace} ({self.environment}, {self.tenant})"


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
        self._locks: dict[str, threading.RLock] = defaultdict(threading.RLock)
        self._guard = threading.Lock()
        self._driving: set[str] = set()

    # ================================================================ helpers
    def cred(self, namespace: str, environment: str | None, tenant: str | None) -> Cred:
        """La credencial de un namespace para un ambiente y tenant. 422 si el tenant o el ambiente no son válidos."""
        tenant = (tenant or "").strip().lower()
        if tenant not in TENANTS:
            raise unprocessable(f"tenant {tenant or 'ausente'}: tiene que ser b2b o b2c")
        env = self.s.vault_env(environment)
        if not env:
            raise unprocessable(f"ambiente {environment or 'ausente'} sin path de Vault (APICON_VAULT_ENVS)")
        return Cred(
            namespace=namespace,
            environment=(environment or "").strip(),
            tenant=tenant,
            path=self.s.credential_path_template.format(env=env, namespace=namespace, tenant=tenant),
            app_id_field=self.s.app_id_field.format(tenant=tenant),
            app_key_field=self.s.app_key_field.format(tenant=tenant),
        )

    def _lock(self, c: Cred) -> threading.RLock:
        with self._guard:
            return self._locks[c.path]

    def _meta(self, c: Cred) -> dict | None:
        return self.vault.metadata(c.path)

    def _index(self, kind: str, value: str, ns: str) -> bool:
        """Unicidad atómica: crea `<mount>/<index_prefix>/<kind>/<value>` con cas=0. False si ya existía."""
        try:
            self.vault.write(f"{self.s.index_root}/{kind}/{value}", {"namespace": ns, "created_at": now()}, cas=0)
            return True
        except CasMismatch:
            return False

    def _register_rotation(self, c: Cred) -> None:
        """Registro para retomar rotaciones al arrancar (`recover`) sin recorrer todos los namespaces: una entrada por
        credencial, con su identidad en la custom_metadata (la API Consumers no lee datos)."""
        entry = f"{self.s.index_root}/rotations/{c.path.replace('/', '--')}"
        try:
            self.vault.write(entry, {"created_at": now()}, cas=0)
        except CasMismatch:
            pass
        self.vault.set_metadata(
            entry, {"namespace": c.namespace, "environment": c.environment, "tenant": c.tenant, "path": c.path}
        )

    # ================================================================== views
    def consumer_view(self, cr: Cred, md: dict, manifest: str | None = None) -> dict:
        c = md["custom"]
        rot = c.get("rotation_id") if c.get("rotation_state") not in TERMINAL else None
        state = "Revoked" if c.get("state") == "revoked" else "Rotating" if rot else "Active"
        return _drop_none(
            {
                "namespace": cr.namespace,
                "environment": cr.environment,
                "tenant": cr.tenant,
                "sigla": c.get("sigla"),
                "tier": c.get("tier"),
                "owner": c.get("owner"),
                "appId": c.get("app_id"),
                "vaultPath": cr.path,
                "vaultProperty": cr.app_key_field,
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

    def rotation_view(self, cr: Cred, md: dict) -> dict:
        c = md["custom"]
        err = c.get("rotation_error")
        return _drop_none(
            {
                "id": c.get("rotation_id"),
                "namespace": cr.namespace,
                "environment": cr.environment,
                "tenant": cr.tenant,
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

    def secret_apim(self, cr: Cred, md: dict | None = None) -> str:
        md = md or self._meta(cr)
        if not md or md["custom"].get("state") == "revoked":
            raise not_found(f"consumidor {cr.label}")
        return render.secret_apim_externalsecret(
            consumer_ns=cr.namespace,
            tenant=cr.tenant,
            app_id=md["custom"].get("app_id", ""),
            vault_key=cr.path,
            app_id_field=cr.app_id_field,
            app_key_field=cr.app_key_field,
            store_kind=self.s.consumer_store_kind,
            store_name=self.s.consumer_store,
            refresh=self.s.secret_apim_refresh,
        )

    # ============================================================= consumidor
    def create_consumer(self, req: dict, idem_key: str) -> tuple[int, dict]:
        ns = req["namespace"]
        cr = self.cred(ns, req.get("environment"), req.get("tenant"))
        h = _hash(req)
        ts = req.get("threescale") or {}
        if ts.get("syncCredentials") and self.s.threescale_mode == "disabled":
            raise not_implemented("sincronización con 3scale no habilitada (APICON_THREESCALE_MODE)")
        with self._lock(cr):
            md = self._meta(cr)
            if md and md["custom"].get("state") != "revoked":
                c = md["custom"]
                if c.get("idempotency_key") == idem_key:
                    if c.get("request_hash") != h:
                        raise unprocessable("Idempotency-Key reutilizada con un payload distinto")
                    return 200, self.consumer_view(cr, md, self.secret_apim(cr, md))
                raise conflict(f"el namespace {ns} ya tiene credencial para {cr.environment} y {cr.tenant}")

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
                cr.path,
                {cr.app_id_field: app_id, cr.app_key_field: app_key},
                cas=md["current_version"] if md else 0,
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
                cr.path,
                {
                    "namespace": ns,
                    "environment": cr.environment,
                    "tenant": cr.tenant,
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
            md = self._meta(cr)
            return 201, self.consumer_view(cr, md, self.secret_apim(cr, md))

    def get_consumer(self, cr: Cred) -> dict:
        md = self._meta(cr)
        if not md:
            raise not_found(f"consumidor {cr.label}")
        return self.consumer_view(cr, md)

    def delete_consumer(self, cr: Cred, idem_key: str) -> dict:
        ns = cr.namespace
        with self._lock(cr):
            md = self._meta(cr)
            if not md:
                raise not_found(f"consumidor {cr.label}")
            c = md["custom"]
            if c.get("state") == "revoked":
                return self.consumer_view(cr, md)
            if c.get("rotation_id") and c.get("rotation_state") not in TERMINAL:
                raise conflict(f"{cr.label} tiene una rotación en curso ({c.get('rotation_state')})")
            if [s for s in self.subscriber.list_subscriptions(ns) if s.get("tenant") in (None, cr.tenant)]:
                raise conflict(f"{cr.label} tiene suscripciones vivas: darlas de baja antes")
            # tombstone sin leer la key: el app_id sale de la metadata (la policy no tiene delete)
            self.vault.write(
                cr.path, {cr.app_id_field: c.get("app_id", ""), "revoked_at": now()}, cas=md["current_version"]
            )
            self.vault.set_metadata(
                cr.path, {"state": "revoked", "revoked_at": now(), "revoke_idempotency_key": idem_key}
            )
            return self.consumer_view(cr, self._meta(cr))

    # ============================================================== rotación
    def _set_rot(self, cr: Cred, **kw: Any) -> None:
        """Actualiza claves rotation_* de la metadata; un valor vacío la borra (Vault no admite vacíos)."""
        vals = {f"rotation_{k}": v for k, v in kw.items()}
        drop = {k for k, v in vals.items() if v in ("", None)}
        keep = {k: v for k, v in vals.items() if k not in drop}
        self.vault.set_metadata(cr.path, {**keep, "rotation_updated_at": now()}, drop=drop)

    def _fail(self, cr: Cred, msg: str) -> None:
        log.warning("rotación de %s → Failed: %s", cr.label, msg)
        self._set_rot(cr, state="Failed", error=msg)
        self._callback(cr)

    def _callback(self, cr: Cred) -> None:
        md = self._meta(cr)
        url = md["custom"].get("rotation_callback") if md else None
        if url and md["custom"].get("rotation_state") in CALLBACK_STATES:
            deliver_callback(
                url,
                self.rotation_view(cr, md),
                self.s.callback_secret,
                self.s.callback_retries,
                self.s.callback_timeout_s,
            )

    def start_rotation(self, cr: Cred, req: dict, idem_key: str) -> dict:
        payload = {"ns": cr.namespace, "path": cr.path, **req}
        h = _hash(payload)
        with self._lock(cr):
            md = self._meta(cr)
            if not md:
                raise not_found(f"consumidor {cr.label}")
            c = md["custom"]
            if c.get("state") == "revoked":
                raise conflict(f"{cr.label} está revocado")
            if c.get("rotation_id") and c.get("rotation_idem") == idem_key:
                if c.get("rotation_hash") != h:
                    raise unprocessable("Idempotency-Key reutilizada con un payload distinto")
                return self.rotation_view(cr, md)
            if c.get("rotation_id") and c.get("rotation_state") not in TERMINAL:
                raise conflict(f"{cr.label} ya tiene una rotación en curso ({c.get('rotation_state')})")
            ts = now()
            deadline = (datetime.now(timezone.utc) + timedelta(days=self.s.rotation_deadline_days)).isoformat(
                timespec="seconds"
            )
            self._register_rotation(cr)
            self.vault.set_metadata(
                cr.path,
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
        self.runner.submit(self._drive, cr)
        return self.rotation_view(cr, self._meta(cr))

    def get_rotation(self, cr: Cred, rotation_id: str) -> dict:
        md = self._meta(cr)
        if not md or md["custom"].get("rotation_id") != rotation_id:
            raise not_found(f"rotación {rotation_id}")
        return self.rotation_view(cr, md)

    def update_rotation(self, cr: Cred, rotation_id: str, action: str) -> dict:
        with self._lock(cr):
            md = self._meta(cr)
            if not md or md["custom"].get("rotation_id") != rotation_id:
                raise not_found(f"rotación {rotation_id}")
            st = md["custom"].get("rotation_state")
            if action == "complete":
                if st == "Completed":
                    return self.rotation_view(cr, md)
                if st not in ("AwaitingConsumer", "Completing"):
                    raise conflict(f"la rotación está en {st}: complete solo desde AwaitingConsumer")
                self._set_rot(cr, state="Completing", error="")
            else:
                if st == "Aborted":
                    return self.rotation_view(cr, md)
                if st not in ("Requested", "OverlapOpening", "Aborting"):
                    raise conflict(
                        f"la rotación está en {st}: después de escribir la key nueva no hay abort sin leer la vieja; "
                        "completarla y, si hace falta, rotar de nuevo (roll-forward)"
                    )
                self._set_rot(cr, state="Aborting", error="")
        self.runner.submit(self._drive, cr)
        return self.rotation_view(cr, self._meta(cr))

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

    def _drive(self, cr: Cred) -> None:
        """Avanza la máquina de estados de la rotación hasta un estado de espera o final."""
        ns, tenant = cr.namespace, cr.tenant
        with self._guard:
            if cr.path in self._driving:
                return
            self._driving.add(cr.path)
        try:
            for _ in range(10):
                md = self._meta(cr)
                c = md["custom"]
                st, rid = c.get("rotation_state"), c.get("rotation_id")
                if st == "Requested":
                    try:
                        pinned = int(c["rotation_pinned_version"])
                        op = self.subscriber.open_overlap(ns, tenant, rid, pinned, f"{rid}-open")
                    except Problem as e:
                        if e.status == 409:
                            self._fail(cr, e.detail)
                            return
                        raise
                    with self._lock(cr):
                        if self._meta(cr)["custom"].get("rotation_state") == "Requested":
                            self._set_rot(cr, state="OverlapOpening", op=op["id"])
                elif st == "OverlapOpening":
                    res = self._wait(c["rotation_op"])
                    if not self._write_new_key(cr, res):
                        continue
                elif st == "KeyWritten":
                    ok = False
                    try:
                        ok = self.subscriber.refresh(ns, tenant)
                    except Problem as e:
                        log.warning("refresh de %s: %s", cr.label, e.detail)
                    note = "" if ok else "refresh no confirmado: el Secret principal toma la key en su refreshInterval"
                    self._set_rot(cr, state="AwaitingConsumer", error=note)
                    self._callback(cr)
                    return
                elif st in ("Completing", "Aborting"):
                    key = f"{rid}-{'close' if st == 'Completing' else 'abort'}"
                    op = self.subscriber.close_overlap(ns, tenant, key)
                    res = self._wait(op["id"]) if op else "Succeeded"
                    if res != "Succeeded":
                        self._set_rot(cr, error=f"el API Subscriber no confirmó el cierre de la ventana ({res})")
                        return
                    if st == "Completing":
                        self.vault.set_metadata(cr.path, {"rotated_at": now()})
                    self._set_rot(cr, state="Completed" if st == "Completing" else "Aborted", error="")
                    self._callback(cr)
                    return
                else:
                    return
        except Problem as e:  # API Subscriber caído: el estado queda para reintentar (PATCH o recover)
            log.warning("rotación de %s en pausa: %s", cr.label, e.detail)
            self._set_rot(cr, error=e.detail)
        finally:
            with self._guard:
                self._driving.discard(cr.path)

    def _write_new_key(self, cr: Cred, overlap_state: str) -> bool:
        """Paso 3 de D4. Devuelve True si avanzó; False si la rotación cambió de estado (p. ej. abort) o falló."""
        with self._lock(cr):
            md = self._meta(cr)
            c = md["custom"]
            if c.get("rotation_state") != "OverlapOpening":
                return False  # abortada mientras se esperaba al Subscriber
            if overlap_state != "Succeeded":
                self._fail(cr, f"la ventana no se confirmó ({overlap_state}); no se escribió la key nueva")
                return False
            pinned = int(c["rotation_pinned_version"])
            v = md["versions"].get(pinned)
            if not v or v["destroyed"] or v["deleted"]:
                self._fail(cr, f"la versión fijada {pinned} ya no existe en Vault (max_versions)")
                return False
            app_key = generate_app_key()
            self._index("key-fp", fingerprint(app_key), cr.namespace)
            try:
                data = {cr.app_id_field: c.get("app_id", ""), cr.app_key_field: app_key}
                new = self.vault.write(cr.path, data, cas=pinned)
            except CasMismatch:
                self._fail(cr, f"la credencial cambió en Vault después de fijar la versión {pinned}")
                return False
            if c.get("threescale_state") in ("InSync", "Failed"):
                try:
                    self.threescale.set_credentials(
                        c.get("threescale_tenant", ""), c.get("threescale_app", ""), c.get("app_id", ""), app_key
                    )
                    self.vault.set_metadata(cr.path, {"threescale_state": "InSync"})
                except Problem as e:
                    self.vault.set_metadata(cr.path, {"threescale_state": "Failed"})
                    log.warning("sync 3scale falló para %s: %s", cr.label, e.detail)
            del app_key
            self._set_rot(cr, state="KeyWritten", new_version=new)
            return True

    def recover(self) -> None:
        """Al arrancar: retoma las rotaciones que quedaron en un paso automático (registro de `_register_rotation`)."""
        root = f"{self.s.index_root}/rotations"
        for entry in self.vault.list(root):
            if entry.endswith("/"):
                continue
            ident = (self.vault.metadata(f"{root}/{entry}") or {}).get("custom") or {}
            try:
                cr = self.cred(ident.get("namespace", ""), ident.get("environment"), ident.get("tenant"))
            except Problem:
                continue
            md = self._meta(cr)
            if md and md["custom"].get("rotation_state") in DRIVEN:
                self.runner.submit(self._drive, cr)
