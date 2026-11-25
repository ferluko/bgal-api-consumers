"""Onboarding de una credencial preexistente (adopción) y corrección del app_id de una adopción (0.4.0, 25/11).

Es lo que hacía `deploy/scripts/adopt-credential.sh` (28/10) a mano desde el bastión, ahora también por la API
(`POST|PATCH /v1/consumers/{ns}/adoption`): al desafectar 3scale, el pipeline de DevSecOps crea la credencial en Vault
y llama acá para que la API Consumers la administre. El script sigue, como envoltorio de este módulo.

Escribe solo la `custom_metadata` del secreto y la reserva del app_id en el índice de unicidad. **No lee datos** (D3):
el app_id lo informa quien puede verlo (DevSecOps, o el dueño del namespace desde su Secret `secret-apim-<tenant>`).

Un namespace migrado de OpenShift a EKS tiene el mismo secreto en dos paths (24/11): la reserva del app_id guarda su
dueño (namespace y tenant) en la metadata del índice, y la adopción de la otra copia la comparte
(`adopted_index: shared`). Una reserva anterior a 0.4.0 no tiene dueño: se rechaza y queda `skipIndex`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .errors import Problem
from .vault import CasMismatch, now, split_mount

if TYPE_CHECKING:
    from .config import Settings
    from .service import Cred

# el app_id es el valor del label app-id de los ExternalSecrets del API Subscriber
LABEL = re.compile(r"^[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$")
APP_ID_8HEX = re.compile(r"^[0-9a-f]{8}$")  # formato de 3scale y de los generados por la API Consumers
NAMESPACE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
TITLES = {404: "Not Found", 409: "Conflict", 422: "Unprocessable Entity"}
log = logging.getLogger(__name__)


class Refused(Problem):
    """No corresponde escribir; el detalle dice por qué. El endpoint lo devuelve con su status; el script, como FAIL."""

    def __init__(self, detail: str, status: int = 409):
        super().__init__(status, TITLES.get(status, "Conflict"), detail)


@dataclass
class Plan:
    mode: str
    namespace: str
    tenant: str
    path: str
    version: int
    current: dict[str, str]
    metadata: dict[str, str]  # merge sobre la custom_metadata
    reserve: str | None  # entrada del índice que se crea con cas=0
    index: str = "reserved"  # reserved | shared (la otra copia de un namespace migrado) | skipped
    release: str | None = None  # entrada del índice que se borra (fix-app-id con RELEASE_OLD)
    notes: list[str] = field(default_factory=list)


def reserve(vault, entry: str, ns: str, tenant: str | None = None) -> bool:
    """Crea la entrada del índice con cas=0; False si ya existía. Con `tenant`, deja el dueño en la metadata
    (best-effort: una policy sin `update` en la metadata de `app-ids/` deja la reserva sin dueño, como antes de
    0.4.0)."""
    try:
        vault.write(entry, {"namespace": ns, "created_at": now()}, cas=0)
    except CasMismatch:
        return False
    if tenant:
        try:
            vault.set_metadata(entry, {"namespace": ns, "tenant": tenant})
        except Exception as e:  # noqa: BLE001 - la reserva ya vale; sin dueño, la copia migrada pide skipIndex
            log.warning("%s: reservado sin dueño en la metadata (%s): falta update en app-ids", entry, type(e).__name__)
    return True


def _index(vault, entry: str, ns: str, tenant: str, skip: bool) -> tuple[str | None, str, list[str]]:
    """(entrada a reservar, estado del índice, notas). Refused si el app_id es de otro consumidor."""
    if skip:
        return None, "skipped", []
    try:
        md = vault.metadata(entry)
    except Exception as e:  # noqa: BLE001 - sin read en la metadata del índice: decide el cas=0 de apply
        log.warning("%s: no se pudo leer la reserva (%s)", entry, type(e).__name__)
        md = None
    if not md:
        return entry, "reserved", []
    who = md.get("custom") or {}
    if who.get("namespace") == ns and who.get("tenant") == tenant:
        return None, "shared", [f"{entry}: lo reservó {ns} ({tenant}), la otra copia de esta credencial; se comparte"]
    holder = (
        f"de {who['namespace']} ({who.get('tenant') or '?'})"
        if who.get("namespace")
        else "sin dueño en la metadata (reserva anterior a 0.4.0)"
    )
    raise Refused(f"{entry}: el app_id ya está reservado, {holder}; si es de esta misma credencial: skipIndex")


def build_plan(mode: str, cr: Cred, ids: list[str], opts: dict, s: Settings, vault, subscriber) -> Plan:
    """Valida contra la metadata actual y arma lo que se escribiría. Refused si no corresponde. No escribe nada."""
    from .service import TERMINAL, _managed  # import diferido: service usa este módulo

    if mode not in ("adopt", "fix-app-id"):
        raise Refused(f"modo {mode!r}: adopt | fix-app-id", 422)
    if len(ids) != (1 if mode == "adopt" else 2):
        raise Refused("adopt lleva un app_id; fix-app-id, el actual y el correcto", 422)
    ns, tenant, path = cr.namespace, cr.tenant, cr.path
    if not NAMESPACE.match(ns):
        raise Refused(f"namespace {ns!r} inválido", 422)
    for a in ids:
        if not LABEL.match(a):
            raise Refused(f"app_id {a!r}: no sirve como valor del label app-id", 422)
    new = ids[-1]
    md = vault.metadata(path)
    if not md:
        raise Refused(f"{path}: no existe", 404)
    c = md["custom"]
    notes = []
    if not APP_ID_8HEX.match(new):
        notes.append(f"app_id {new}: no tiene el formato de 3scale ni el de la API Consumers (8 hex); revisalo")
    release = None
    if mode == "adopt":
        if _managed(c):
            raise Refused(
                f"{path}: ya está administrada (app_id {c.get('app_id') or '?'}, origin {c.get('origin') or '?'}); "
                "para corregir el app_id de una adopción: fix-app-id"
            )
        if not opts.get("OWNER"):
            notes.append("sin OWNER: la metadata queda sin dueño")
    else:
        old = ids[0]
        if old == new:
            raise Refused("el app_id actual y el correcto son iguales", 422)
        if c.get("origin") != "adopted":
            raise Refused(
                f"{path}: no es una adopción (origin {c.get('origin') or 'ninguno'}): el app_id de una credencial "
                "generada o importada está en los datos y no se corrige en la metadata"
            )
        if c.get("app_id") != old:
            raise Refused(f"{path}: el app_id actual es {c.get('app_id') or 'ninguno'}, no {old}")
        if c.get("rotation_state") and c["rotation_state"] not in TERMINAL:
            raise Refused(f"{path}: rotación {c.get('rotation_id')} en curso ({c['rotation_state']})")
        # el label app-id de una suscripción viva se fijó con el app_id viejo: corregir la metadata no la cambia
        live = [x.get("id", "?") for x in subscriber.list_subscriptions(ns) if (x.get("tenant") or tenant) == tenant]
        if live:
            raise Refused(f"{ns} tiene suscripciones vivas con el app_id {old}; baja antes: {', '.join(live)}")
        mode_release = opts.get("RELEASE_OLD", "")
        if mode_release:
            if mode_release != "force" and c.get("adopted_index") != "reserved":
                raise Refused(
                    f"no consta que la reserva de {old} la haya hecho esta adopción (adopted_index "
                    f"{c.get('adopted_index') or 'ausente'}): RELEASE_OLD=force solo si estás seguro"
                )
            release = f"{s.index_root}/app-ids/{old}"
        else:
            notes.append(f"la reserva de {old} en el índice queda (RELEASE_OLD=1 / releasePrevious para borrarla)")
    entry, state, index_notes = _index(
        vault, f"{s.index_root}/app-ids/{new}", ns, tenant, opts.get("SKIP_INDEX") == "1"
    )
    marker = {"adopted_index": state}
    if mode == "adopt":
        meta = {
            "namespace": ns,
            "environment": cr.environment,
            "tenant": tenant,
            "platform": cr.platform,
            "eks_cluster": cr.cluster,
            "sigla": opts.get("SIGLA") or ns.split("-")[0].upper(),
            "tier": s.tier,
            "owner": opts.get("OWNER", ""),
            "servicenow_ref": opts.get("REF", ""),
            "app_id": new,
            "state": "active",
            "origin": "adopted",
            "adopted_at": now(),
            "adopted_by": opts.get("ADOPTED_BY") or "adopt-credential",
            "threescale_state": "NotApplicable",
            **marker,
        }
    else:
        meta = {"app_id": new, "app_id_corrected_from": ids[0], "app_id_corrected_at": now(), **marker}
    return Plan(mode, ns, tenant, path, md["current_version"], c, meta, entry, state, release, notes + index_notes)


def describe(p: Plan) -> list[str]:
    cur = ", ".join(f"{k}={str(v)[:60]}" for k, v in sorted(p.current.items())) or "vacía"
    index = {
        "reserved": f"reserva {p.reserve} (cas=0)",
        "shared": "comparte la reserva de la otra copia de esta credencial",
        "skipped": "sin reserva (SKIP_INDEX=1)",
    }[p.index]
    out = [
        f"--    credencial {p.path} (versión {p.version}; no se leen ni se tocan los datos)",
        f"--    metadata actual: {cur}",
        "--    escribe en la metadata:",
        *(f"        {k}: {p.current.get(k, '—')} → {v}" for k, v in p.metadata.items() if v and v != p.current.get(k)),
        f"--    índice: {index}",
    ]
    if p.release:
        out.append(f"--    índice: borra {p.release}")
    return out + [f"WARN  {n}" for n in p.notes]


def plan_view(p: Plan) -> dict:
    """El plan para la respuesta de un dryRun (lo que `describe` muestra en el script)."""
    view = {
        "dryRun": True,
        "mode": p.mode,
        "vaultPath": p.path,
        "vaultVersion": p.version,
        "changes": {k: str(v) for k, v in p.metadata.items() if v and v != p.current.get(k)},
        "index": p.index,
        "reserve": p.reserve,
        "release": p.release,
        "notes": p.notes,
    }
    return {k: v for k, v in view.items() if v is not None}


def _release(vault, path: str) -> None:
    """Borra una entrada del índice (metadata y versiones). La policy de la API Consumers no tiene delete."""
    if hasattr(vault, "client"):  # HvacVault
        mount, rel = split_mount(path)
        vault._call(vault.client.secrets.kv.v2.delete_metadata_and_all_versions, path=rel, mount_point=mount)
    else:  # MemoryVault (tests)
        for d in (vault.current, vault.data, vault.meta):
            d.pop(path, None)


def apply(p: Plan, vault) -> list[str]:
    """Escribe lo del plan y devuelve las líneas del resultado (OK / WARN)."""
    from .service import _managed

    out = []
    if p.reserve:
        if not reserve(vault, p.reserve, p.namespace, p.tenant):
            raise Refused(
                f"{p.reserve}: el app_id ya está reservado (es de otro consumidor, o de una corrida anterior: "
                "skipIndex / SKIP_INDEX=1)"
            )
        out.append(f"OK    reservado {p.reserve}")
    vault.set_metadata(p.path, p.metadata)
    c = (vault.metadata(p.path) or {}).get("custom") or {}
    state = "Active" if _managed(c) else "Unmanaged"
    out.append(f"OK    {p.path}: app_id {c.get('app_id')}, origin {c.get('origin')} → {state}")
    if p.release:
        try:
            _release(vault, p.release)
            out.append(f"OK    borrado {p.release}")
        except Exception as e:  # noqa: BLE001 - Forbidden con la policy de D3: la adopción ya quedó hecha
            out.append(
                f"WARN  {p.release}: no se pudo borrar ({type(e).__name__}); la policy de la API Consumers no tiene "
                "delete: pedírselo a DevSecOps"
            )
    return out


def notes(p: Plan, out: list[str]) -> list[str]:
    """Avisos para la respuesta de la API: los del plan y los WARN de lo que se escribió."""
    return p.notes + [line[6:] for line in out if line.startswith("WARN")]
