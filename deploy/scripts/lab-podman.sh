#!/usr/bin/env bash
# =============================================================================
# Lab: API Consumers en un contenedor (podman) en darqtesting01, al lado del API Subscriber, contra el Vault de lab
# (port-forward) de paas-arqlab. Mismo patrón que bgal-api-sub/deploy/scripts/lab-podman.sh.
#
#   ./deploy/scripts/lab-podman.sh prep     # token de la API, secretos como archivos (AppRole, API Subscriber), env
#   ./deploy/scripts/lab-podman.sh vault    # port-forward a Vault en segundo plano (se relanza solo si se cae)
#   ./deploy/scripts/lab-podman.sh run      # (re)crea el contenedor y espera /readyz
#   ./deploy/scripts/lab-podman.sh status | logs | stop | build
#
# Variables:
#   VD_OUT                 .out/vault-dev de la PoC (approle/apim-subscriber-nonprd.{role_id,secret_id})
#   SUBSCRIBER_URL         API Subscriber (default http://127.0.0.1:18080, su contenedor en este host)
#   SUBSCRIBER_TOKEN_FILE  token de la API del Subscriber (su $OUT/api-token)
#   IMAGE                  default quay.io/ferlukobgal/bgal-api-consumers:<versión de pyproject>
#   ENGINE                 podman (default) | docker
#   OUT                    default .out/local (gitignored)
#   PORT                   puerto local de la API (default 18081, solo 127.0.0.1)
#   VAULT_PORT             puerto local del port-forward a Vault (default 18200; el mismo que usaba el Subscriber 0.1)
#
# El token de esta API queda en $OUT/api-token: el API Subscriber lo usa como CONSUMERS_TOKEN_FILE.
# Secretos: solo en $OUT (700/600). Nada se imprime ni pasa por argv.
# =============================================================================
set -euo pipefail
umask 077

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
CMD=${1:-help}
ENGINE=${ENGINE:-podman}
OUT=${OUT:-$ROOT/.out/local}
VERSION=$(awk -F'"' '/^version *=/{print $2; exit}' "$ROOT/pyproject.toml")
IMAGE=${IMAGE:-quay.io/ferlukobgal/bgal-api-consumers:$VERSION}
NAME=${NAME:-api-consumers-local}
PORT=${PORT:-18081}
VAULT_PORT=${VAULT_PORT:-18200}
VAULT_NS=${VAULT_NS:-apim-vault-dev}
SUBSCRIBER_URL=${SUBSCRIBER_URL:-http://127.0.0.1:18080}
EXPECTED_CLUSTER=${EXPECTED_CLUSTER:-paas-arqlab}
R=apim-subscriber-nonprd   # rol AppRole del kit vault-dev (falta un rol con la policy mínima de D3)

RED=$'\e[31m'; GRN=$'\e[32m'; YLW=$'\e[33m'; RST=$'\e[0m'
ok()   { echo "${GRN}OK${RST}    $*"; }
warn() { echo "${YLW}WARN${RST}  $*"; }
die()  { echo "${RED}FAIL${RST}  $*" >&2; exit 1; }

require_lab() {
  local s; s=$(oc whoami --show-server 2>/dev/null) || die "oc no está logueado"
  [[ "$s" == *"$EXPECTED_CLUSTER"* ]] || die "cluster actual $s no es $EXPECTED_CLUSTER"
}
pf_alive() { [[ -s "$OUT/vault-pf.pid" ]] && kill -0 "$(cat "$OUT/vault-pf.pid")" 2>/dev/null; }
vault_up() { curl -s -o /dev/null -m 3 "http://127.0.0.1:$VAULT_PORT/v1/sys/health"; }
ready()    { curl -s -m 3 -w '\n%{http_code}' "http://127.0.0.1:$PORT/readyz" 2>/dev/null || true; }
port_busy(){ (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
secret_file() { printf '%s' "$2" > "$OUT/secrets/$1"; }

cmd_prep() {
  : "${VD_OUT:?definir VD_OUT (.out/vault-dev de la PoC)}"
  mkdir -p "$OUT"; chmod 700 "$OUT"
  for f in "$VD_OUT/approle/$R.role_id" "$VD_OUT/approle/$R.secret_id"; do [[ -s "$f" ]] || die "falta $f"; done
  [[ -s "$OUT/api-token" ]] || python3 -c "import secrets; print(secrets.token_urlsafe(32))" > "$OUT/api-token"
  rm -rf "$OUT/secrets"; mkdir -p "$OUT/secrets"
  secret_file APICON_API_TOKENS "$(<"$OUT/api-token")"
  secret_file APICON_VAULT_ROLE_ID "$(<"$VD_OUT/approle/$R.role_id")"
  secret_file APICON_VAULT_SECRET_ID "$(<"$VD_OUT/approle/$R.secret_id")"
  secret_file APICON_SUBSCRIBER_URL "$SUBSCRIBER_URL"
  if [[ -s "${SUBSCRIBER_TOKEN_FILE:-}" ]]; then
    secret_file APICON_SUBSCRIBER_TOKEN "$(<"$SUBSCRIBER_TOKEN_FILE")"
  else
    warn "sin SUBSCRIBER_TOKEN_FILE: la rotación y la baja fallan si el API Subscriber exige token"
  fi
  {
    echo "HOME=/tmp"
    echo "APICON_TIER=nonprd"
    echo "APICON_SECRETS_DIR=/etc/api-consumers/secrets"
    echo "APICON_LISTEN_HOST=127.0.0.1"
    echo "APICON_LISTEN_PORT=$PORT"
    echo "APICON_VAULT_MODE=hvac"
    echo "APICON_VAULT_ADDR=http://127.0.0.1:$VAULT_PORT"
    echo "APICON_VAULT_AUTH=approle"
    echo "APICON_SUBSCRIBER_MODE=http"
    echo "APICON_ROTATION_POLL_S=3"
  } > "$OUT/api-consumers.env"
  chmod -R go-rwx "$OUT"
  ok "secretos en $OUT/secrets ($(ls "$OUT/secrets" | tr '\n' ' '))"
  ok "token de esta API en $OUT/api-token (para el Subscriber: CONSUMERS_TOKEN_FILE=$OUT/api-token)"
}

cmd_vault() {
  require_lab
  if pf_alive && vault_up; then ok "port-forward a Vault activo en 127.0.0.1:$VAULT_PORT"; return; fi
  if vault_up; then ok "Vault ya responde en 127.0.0.1:$VAULT_PORT (otro port-forward)"; return; fi
  pf_alive && kill "$(cat "$OUT/vault-pf.pid")" 2>/dev/null || true
  nohup bash -c "while :; do oc -n '$VAULT_NS' port-forward svc/vault '$VAULT_PORT:8200' --address 127.0.0.1; sleep 2; done" \
    > "$OUT/vault-pf.log" 2>&1 &
  echo $! > "$OUT/vault-pf.pid"
  for _ in $(seq 20); do vault_up && break; sleep 1; done
  vault_up || die "Vault no responde en 127.0.0.1:$VAULT_PORT (ver $OUT/vault-pf.log)"
  ok "port-forward a Vault en 127.0.0.1:$VAULT_PORT"
}

cmd_run() {
  [[ -s "$OUT/api-consumers.env" && -d "$OUT/secrets" ]] || die "falta $OUT/api-consumers.env: correr '$0 prep'"
  vault_up || die "Vault no responde en 127.0.0.1:$VAULT_PORT: correr '$0 vault'"
  "$ENGINE" rm -f "$NAME" >/dev/null 2>&1 || true
  port_busy "$PORT" && die "127.0.0.1:$PORT ya está en uso: usar PORT=<libre> $0 run"
  local z="" opts
  [[ "$ENGINE" == podman ]] && z=",Z"
  [[ $EUID -eq 0 ]] && warn "podman como root: el contenedor corre como root (lab)"
  opts=(-d --name "$NAME" --network host --read-only --tmpfs /tmp --user 0:0 --env-file "$OUT/api-consumers.env"
        -e APICON_LISTEN_HOST=127.0.0.1 -e APICON_LISTEN_PORT="$PORT"
        -v "$OUT/secrets:/etc/api-consumers/secrets:ro$z")
  "$ENGINE" run "${opts[@]}" "$IMAGE" >/dev/null
  ok "contenedor $NAME ($IMAGE)"
  local r="" code=""
  for _ in $(seq 30); do
    r=$(ready); code=$(tail -1 <<<"$r")
    [[ "$code" == 200 && "$r" == *'"vault"'* ]] && break
    "$ENGINE" inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null | grep -q true || break
    sleep 2
  done
  if [[ "$code" != 200 || "$r" != *'"vault"'* ]]; then
    "$ENGINE" logs --tail 30 "$NAME" >&2 || true
    die "/readyz → ${code:-sin respuesta} ($(sed '$d' <<<"$r" | cut -c1-200))"
  fi
  ok "/readyz 200 en http://127.0.0.1:$PORT"
  echo
  echo "Para curl / e2e:  export API_CON_URL=http://127.0.0.1:$PORT API_CON_TOKEN=\$(cat $OUT/api-token)"
}

cmd_status() {
  echo "imagen:     $IMAGE"
  echo "contenedor: $("$ENGINE" inspect -f '{{.State.Status}} desde {{.State.StartedAt}}' "$NAME" 2>/dev/null || echo ausente)"
  echo "readyz:     $(curl -s -m 3 "http://127.0.0.1:$PORT/readyz" 2>/dev/null | cut -c1-120 || echo sin respuesta)"
  vault_up && echo "vault:      responde en 127.0.0.1:$VAULT_PORT" || echo "vault:      no responde"
}

cmd_stop() {
  "$ENGINE" rm -f "$NAME" >/dev/null 2>&1 && ok "contenedor $NAME eliminado" || true
  if pf_alive; then
    local p; p=$(cat "$OUT/vault-pf.pid")
    pkill -P "$p" 2>/dev/null || true; kill "$p" 2>/dev/null || true
    ok "port-forward detenido"
  fi
  rm -f "$OUT/vault-pf.pid"
}

case "$CMD" in
  prep)   cmd_prep ;;
  vault)  cmd_vault ;;
  run)    cmd_run ;;
  status) cmd_status ;;
  logs)   exec "$ENGINE" logs -f "$NAME" ;;
  stop)   cmd_stop ;;
  build)  "$ENGINE" build -t "$IMAGE" -f "$ROOT/Containerfile" "$ROOT" ;;
  *)      sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
