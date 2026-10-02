#!/usr/bin/env bash
# =============================================================================
# Lab (paas-arqlab): crea el Secret api-consumers-secrets en apim-toolkit SIN pasar por Git.
# En clusters reales lo materializa ESO desde el Vault de plataforma (values/groups/domain-apim.yaml).
#
#   VD_OUT       .out/vault-dev de la PoC (approle/apim-subscriber-nonprd.{role_id,secret_id}: el rol del kit)
#   OUT          directorio de secretos del lab (default .out/local): token de la API y de callbacks
#   NS           namespace (default apim-toolkit)
#   SUBSCRIBER_URL / SUBSCRIBER_TOKEN_FILE  API Subscriber (default http://api-subscriber.apim-toolkit.svc:8080)
#
# No imprime secretos.
# =============================================================================
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
OUT=${OUT:-$ROOT/.out/local}
: "${VD_OUT:?definir VD_OUT (…/poc-credenciales-suscripcion/.out/vault-dev)}"
NS=${NS:-apim-toolkit}
SUBSCRIBER_URL=${SUBSCRIBER_URL:-http://api-subscriber.apim-toolkit.svc:8080}
R=apim-subscriber-nonprd
for f in "$VD_OUT/approle/$R.role_id" "$VD_OUT/approle/$R.secret_id"; do
  [[ -s "$f" ]] || { echo "falta $f" >&2; exit 1; }
done
umask 077; mkdir -p "$OUT"
[[ -s "$OUT/api-token" ]] || python3 -c "import secrets; print(secrets.token_urlsafe(32))" > "$OUT/api-token"
[[ -s "$OUT/callback-secret" ]] || python3 -c "import secrets; print(secrets.token_hex(32))" > "$OUT/callback-secret"

oc get ns "$NS" >/dev/null 2>&1 || oc create ns "$NS" >/dev/null
EXTRA=(--from-literal=subscriber_url="$SUBSCRIBER_URL")
[[ -s "${SUBSCRIBER_TOKEN_FILE:-}" ]] && EXTRA+=(--from-file=subscriber_token="$SUBSCRIBER_TOKEN_FILE")
oc -n "$NS" create secret generic api-consumers-secrets "${EXTRA[@]}" \
  --from-file=vault_role_id="$VD_OUT/approle/$R.role_id" \
  --from-file=vault_secret_id="$VD_OUT/approle/$R.secret_id" \
  --from-file=api_tokens="$OUT/api-token" \
  --from-file=callback_secret="$OUT/callback-secret" \
  --dry-run=client -o yaml | oc apply -f - >/dev/null
echo "OK: secret $NS/api-consumers-secrets (token de la API en $OUT/api-token, no se imprime)"
