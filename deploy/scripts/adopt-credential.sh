#!/usr/bin/env bash
# =============================================================================
# Adopta una credencial preexistente (Unmanaged) o corrige el app_id de una adopción, con la API Consumers.
#
# Una credencial que dejó otro proceso (DevSecOps, 3scale) en el path de la convención (secret-apim-<tenant>-v2) no
# tiene la metadata de la API Consumers: se informa Unmanaged y el API Subscriber no la suscribe (422 "adoptarla antes
# de suscribir"). Adoptarla escribe su custom_metadata (app_id, origin: adopted, sigla, owner…) y reserva el app_id en
# el índice de unicidad. Corre en el pod de la API Consumers, con su configuración y su acceso a Vault
# (adopt_credential.py por stdin). No lee datos (D3): el app_id lo informa quien puede verlo (DevSecOps, o el dueño
# desde su Secret secret-apim-<tenant>). Muestra el plan y pide confirmación antes de escribir.
# Desde 0.4.0 es lo mismo que POST|PATCH /v1/consumers/{ns}/adoption de la API (y necesita esa imagen en el pod).
#
#   deploy/scripts/adopt-credential.sh adopt      <ns> <ambiente> <tenant> <app_id>
#   deploy/scripts/adopt-credential.sh fix-app-id <ns> <ambiente> <tenant> <app_id_actual> <app_id_correcto>
#
# Variables:
#   NS                namespace de la API Consumers (default: donde esté el Deployment api-consumers)
#   SIGLA OWNER REF   adopt: sigla (default: prefijo del namespace), dueño y ticket, para la metadata
#   PLATFORM          dónde está la credencial: openshift (default) o eks
#   EKS_CLUSTER       con eks, el nombre del cluster de EKS (eks-<cluster>/<ns>/secret-apim-<tenant>-v2)
#   DRY_RUN=1         solo el plan · YES=1 sin confirmación
#   SKIP_INDEX=1      no reserva el app_id en el índice (una reserva anterior a 0.4.0 de esta misma credencial; la de
#                     la otra copia de un namespace migrado a EKS se comparte sola)
#   RELEASE_OLD=1     fix-app-id: borra la reserva del app_id viejo si la hizo la adopción (force: aunque no conste).
#                     Requiere delete en el índice: la policy de la API Consumers no lo tiene (pedírselo a DevSecOps)
#
# fix-app-id se niega con suscripciones vivas del namespace y tenant (su label app-id es el viejo): darlas de baja antes.
# Después: el dueño prueba contra el gateway con su APP_ID y APP_KEY. Adoptada, la API Consumers puede rotarla y darla
# de baja: acordarlo con el dueño; si la credencial también está en una app de 3scale, no rotarla hasta sincronizarla.
# =============================================================================
set -euo pipefail
DIR=$(cd "$(dirname "$0")" && pwd)
usage() { sed -n 's/^#   deploy/  deploy/p' "$0" >&2; exit 2; }
case "${1:-}" in
  adopt) [[ $# -eq 5 ]] || usage ;;
  fix-app-id) [[ $# -eq 6 ]] || usage ;;
  *) usage ;;
esac
oc whoami >/dev/null 2>&1 || { echo "oc no está logueado" >&2; exit 1; }
NS=${NS:-$(oc get deploy -A --field-selector metadata.name=api-consumers -o jsonpath='{.items[0].metadata.namespace}' 2>/dev/null || true)}
[[ -n "$NS" ]] || { echo "no encontré el Deployment api-consumers: definir NS" >&2; exit 1; }
[[ "$(oc -n "$NS" get deploy api-consumers -o jsonpath='{.status.readyReplicas}' 2>/dev/null)" -ge 1 ]] 2>/dev/null \
  || { echo "la API Consumers no está lista en $NS" >&2; exit 1; }
echo "--    API Consumers en $NS ($(oc whoami --show-server))"

run() { # run plan|apply <args> → adopt_credential.py en el pod (las opciones no son secretas)
  oc -n "$NS" exec -i deploy/api-consumers -- env SIGLA="${SIGLA:-}" OWNER="${OWNER:-}" REF="${REF:-}" \
    PLATFORM="${PLATFORM:-}" EKS_CLUSTER="${EKS_CLUSTER:-}" SKIP_INDEX="${SKIP_INDEX:-}" RELEASE_OLD="${RELEASE_OLD:-}" \
    python3 - "$@" < "$DIR/adopt_credential.py"
}

run plan "$@" || exit $?
if [[ "${DRY_RUN:-}" == 1 ]]; then echo "--    DRY_RUN=1: no se escribió nada"; exit 0; fi
if [[ "${YES:-}" != 1 ]]; then
  [[ -t 0 ]] || { echo "sin terminal para confirmar: YES=1" >&2; exit 1; }
  read -r -p "¿Aplicar? (escribí si) " ans
  [[ "$ans" == si ]] || { echo "--    cancelado: no se escribió nada"; exit 1; }
fi
run apply "$@"
