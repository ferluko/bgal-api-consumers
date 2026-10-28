"""Adopción manual de una credencial preexistente (`Unmanaged`), o corrección del app_id de una adopción.

Lo corre `adopt-credential.sh` dentro del pod de la API Consumers (por stdin), con la configuración y el acceso a Vault
de la API. Escribe solo la `custom_metadata` del secreto y la reserva del app_id en el índice de unicidad. **No lee
datos** (D3): el app_id lo informa quien puede verlo (DevSecOps, o el dueño del namespace desde su Secret
`secret-apim-<tenant>`). Es el paso 2 del job de migración hecho a mano, sin la comparación con 3scale.

  python3 - plan|apply adopt      <ns> <ambiente> <tenant> <app_id>
  python3 - plan|apply fix-app-id <ns> <ambiente> <tenant> <app_id_actual> <app_id_correcto>

Variables: SIGLA (default: prefijo del namespace), OWNER y REF (adopt); SKIP_INDEX=1 (no reserva el app_id: solo para
reintentar una corrida propia que ya lo reservó); RELEASE_OLD=1|force (fix-app-id: borra la reserva del app_id viejo).
`plan` valida y muestra lo que haría; `apply` vuelve a validar y escribe. Sale con 0 si está bien, 1 si se niega, 2 por
uso.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field

from api_consumers.config import TENANTS, Settings, get_settings
from api_consumers.errors import Problem
from api_consumers.service import TERMINAL, _managed
from api_consumers.subscriber import build_subscriber
from api_consumers.vault import CasMismatch, build_vault, now, split_mount

# el app_id es el valor del label app-id de los ExternalSecrets del API Subscriber
LABEL = re.compile(r"^[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$")
APP_ID_8HEX = re.compile(r"^[0-9a-f]{8}$")  # formato de 3scale y de los generados por la API Consumers
NAMESPACE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
OPTS = ("SIGLA", "OWNER", "REF", "SKIP_INDEX", "RELEASE_OLD")


class Refused(Exception):
    """No corresponde escribir; el mensaje dice por qué."""


@dataclass
class Plan:
    mode: str
    namespace: str
    path: str
    version: int
    current: dict[str, str]
    metadata: dict[str, str]  # merge sobre la custom_metadata
    reserve: str | None  # entrada del índice que se crea con cas=0
    release: str | None = None  # entrada del índice que se borra (fix-app-id con RELEASE_OLD)
    notes: list[str] = field(default_factory=list)


def build_plan(
    mode: str, ns: str, environment: str, tenant: str, ids: list[str], opts: dict, s: Settings, vault, subscriber
) -> Plan:
    """Valida contra la metadata actual y arma lo que se escribiría. Refused si no corresponde. No escribe nada."""
    if mode not in ("adopt", "fix-app-id"):
        raise Refused(f"modo {mode!r}: adopt | fix-app-id")
    if len(ids) != (1 if mode == "adopt" else 2):
        raise Refused("adopt lleva un app_id; fix-app-id, el actual y el correcto")
    if not NAMESPACE.match(ns):
        raise Refused(f"namespace {ns!r} inválido")
    tenant = tenant.strip().lower()
    if tenant not in TENANTS:
        raise Refused(f"tenant {tenant!r}: tiene que ser {' o '.join(TENANTS)}")
    venv = s.vault_env(environment)
    if not venv:
        raise Refused(f"ambiente {environment!r} sin path de Vault (APICON_VAULT_ENVS)")
    for a in ids:
        if not LABEL.match(a):
            raise Refused(f"app_id {a!r}: no sirve como valor del label app-id")
    new = ids[-1]
    path = s.credential_path_template.format(env=venv, namespace=ns, tenant=tenant)
    md = vault.metadata(path)
    if not md:
        raise Refused(f"{path}: no existe")
    c = md["custom"]
    notes = []
    if not APP_ID_8HEX.match(new):
        notes.append(f"app_id {new}: no tiene el formato de 3scale ni el de la API Consumers (8 hex); revisalo")
    skip = opts.get("SKIP_INDEX") == "1"
    reserve = None if skip else f"{s.index_root}/app-ids/{new}"
    marker = {"adopted_index": "skipped" if skip else "reserved"}
    release = None
    if mode == "adopt":
        if _managed(c):
            raise Refused(
                f"{path}: ya está administrada (app_id {c.get('app_id') or '?'}, origin {c.get('origin') or '?'}); "
                "para corregir el app_id de una adopción: fix-app-id"
            )
        if not opts.get("OWNER"):
            notes.append("sin OWNER: la metadata queda sin dueño")
        meta = {
            "namespace": ns,
            "environment": environment,
            "tenant": tenant,
            "sigla": opts.get("SIGLA") or ns.split("-")[0].upper(),
            "tier": s.tier,
            "owner": opts.get("OWNER", ""),
            "servicenow_ref": opts.get("REF", ""),
            "app_id": new,
            "state": "active",
            "origin": "adopted",
            "adopted_at": now(),
            "adopted_by": "adopt-credential",
            "threescale_state": "NotApplicable",
            **marker,
        }
    else:
        old = ids[0]
        if old == new:
            raise Refused("el app_id actual y el correcto son iguales")
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
        meta = {"app_id": new, "app_id_corrected_from": old, "app_id_corrected_at": now(), **marker}
        mode_release = opts.get("RELEASE_OLD", "")
        if mode_release:
            if mode_release != "force" and c.get("adopted_index") != "reserved":
                raise Refused(
                    f"no consta que la reserva de {old} la haya hecho esta adopción (adopted_index "
                    f"{c.get('adopted_index') or 'ausente'}): RELEASE_OLD=force solo si estás seguro"
                )
            release = f"{s.index_root}/app-ids/{old}"
        else:
            notes.append(f"la reserva de {old} en el índice queda (RELEASE_OLD=1 para borrarla)")
    return Plan(mode, ns, path, md["current_version"], c, meta, reserve, release, notes)


def describe(p: Plan) -> list[str]:
    cur = ", ".join(f"{k}={str(v)[:60]}" for k, v in sorted(p.current.items())) or "vacía"
    out = [
        f"--    credencial {p.path} (versión {p.version}; no se leen ni se tocan los datos)",
        f"--    metadata actual: {cur}",
        "--    escribe en la metadata:",
        *(f"        {k}: {p.current.get(k, '—')} → {v}" for k, v in p.metadata.items() if v and v != p.current.get(k)),
        f"--    índice: {'reserva ' + p.reserve + ' (cas=0)' if p.reserve else 'sin reserva (SKIP_INDEX=1)'}",
    ]
    if p.release:
        out.append(f"--    índice: borra {p.release}")
    return out + [f"WARN  {n}" for n in p.notes]


def _release(vault, path: str) -> None:
    """Borra una entrada del índice (metadata y versiones). La policy de la API Consumers no tiene delete."""
    if hasattr(vault, "client"):  # HvacVault
        mount, rel = split_mount(path)
        vault._call(vault.client.secrets.kv.v2.delete_metadata_and_all_versions, path=rel, mount_point=mount)
    else:  # MemoryVault (tests)
        for d in (vault.current, vault.data, vault.meta):
            d.pop(path, None)


def apply(p: Plan, vault) -> list[str]:
    out = []
    if p.reserve:
        try:
            vault.write(p.reserve, {"namespace": p.namespace, "created_at": now()}, cas=0)
        except CasMismatch:
            raise Refused(
                f"{p.reserve}: el app_id ya está reservado (es de otro consumidor, o de una corrida anterior: "
                "SKIP_INDEX=1)"
            ) from None
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


def main(argv: list[str], env=os.environ) -> int:
    if len(argv) < 6 or argv[0] not in ("plan", "apply"):
        print(__doc__.split("\n\n")[2], file=sys.stderr)
        return 2
    phase, mode, ns, environment, tenant, *ids = argv
    opts = {k: env.get(k, "").strip() for k in OPTS}
    s = get_settings()
    vault = build_vault(s)
    try:
        p = build_plan(mode, ns, environment, tenant, ids, opts, s, vault, build_subscriber(s))
        # apply vuelve a validar (el plan se mostró en una corrida anterior) y solo informa lo que escribió
        print("\n".join(apply(p, vault) if phase == "apply" else describe(p)))
    except Refused as e:
        print(f"FAIL  {e}")
        return 1
    except Problem as e:  # API Subscriber o Vault no responden
        print(f"FAIL  {e.detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
