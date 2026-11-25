"""Adopción manual de una credencial preexistente (`Unmanaged`), o corrección del app_id de una adopción.

Lo corre `adopt-credential.sh` dentro del pod de la API Consumers (por stdin), con la configuración y el acceso a Vault
de la API. Desde 0.4.0 es un envoltorio de `api_consumers.adoption`, la misma lógica que el endpoint
`POST|PATCH /v1/consumers/{ns}/adoption` (el pod tiene que tener una imagen 0.4.0 o posterior). Escribe solo la
`custom_metadata` del secreto y la reserva del app_id en el índice de unicidad. **No lee datos** (D3).

  python3 - plan|apply adopt      <ns> <ambiente> <tenant> <app_id>
  python3 - plan|apply fix-app-id <ns> <ambiente> <tenant> <app_id_actual> <app_id_correcto>

Variables: SIGLA (default: prefijo del namespace), OWNER y REF (adopt); PLATFORM (openshift | eks) y EKS_CLUSTER
para el path; SKIP_INDEX=1 (no reserva el app_id); RELEASE_OLD=1|force (fix-app-id: borra la reserva del app_id viejo).
`plan` valida y muestra lo que haría; `apply` vuelve a validar y escribe. Sale con 0 si está bien, 1 si se niega, 2 por
uso.
"""

from __future__ import annotations

import os
import sys

from api_consumers import adoption
from api_consumers.adoption import Plan, Refused, apply, describe  # noqa: F401 (los usan los tests y el operador)
from api_consumers.config import Settings, get_settings
from api_consumers.errors import Problem
from api_consumers.service import make_cred
from api_consumers.subscriber import build_subscriber
from api_consumers.vault import build_vault

OPTS = ("SIGLA", "OWNER", "REF", "PLATFORM", "EKS_CLUSTER", "SKIP_INDEX", "RELEASE_OLD")


def build_plan(
    mode: str, ns: str, environment: str, tenant: str, ids: list[str], opts: dict, s: Settings, vault, subscriber
) -> Plan:
    """La credencial del namespace (con PLATFORM/EKS_CLUSTER) y el plan de `api_consumers.adoption`."""
    try:
        cr = make_cred(s, ns, environment, tenant, opts.get("PLATFORM") or None, opts.get("EKS_CLUSTER") or None)
    except Problem as e:
        raise Refused(e.detail, e.status) from None
    return adoption.build_plan(mode, cr, ids, opts, s, vault, subscriber)


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
    except Problem as e:  # Refused, o el API Subscriber o Vault no responden
        print(f"FAIL  {e.detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
