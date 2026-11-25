# bgal-api-consumers — API Consumers (MVP)

MVP en Python de la **API Consumers** del toolkit de APIM sobre Kuadrant (RHCL): la credencial de cada namespace
consumidor (App ID / App Key) en Vault KV v2, con la convención de DevSecOps (06/10 y 07/10):
`openshift-<ambiente>/<ns>/secret-apim-<tenant>-v2`, claves `app_id_<tenant>` y `app_key_<tenant>`. Una credencial por
namespace y tenant (`b2b`/`b2c`); el ambiente sale del `environment` del pedido (QA → `qas`, STG → `int`). En EKS
(0.4.0), `eks-<eksCluster>/<ns>/secret-apim-<tenant>-v2`: un namespace de Vault por cluster, sin el ambiente en el path.

- **Contrato:** [`openapi/consumers.yaml`](openapi/consumers.yaml), v0.4.0-draft (copia de `RHCL-Kuadrant/api-toolkit-spec/openapi/consumers.yaml`).
- **Diseño:** `ADR-api-consumers-y-subscriber-sin-estado.md` (D1, D3, D4, D5), en el repo de documentación.
- **Hermano:** [`bgal-api-sub`](https://github.com/ferluko/bgal-api-sub) (suscripciones; sin acceso a Vault).

```
Null / DevOps Connect / DevSecOps ──► API Consumers ──write (cas, sin read)──► Vault KV v2  openshift-<amb>/<ns>/secret-apim-<tenant>-v2
                                          │        └─ metadata: app_id, estado, rotación
                                          │  rotación: fijar vN → abrir ventana → vN+1 → refresh → cerrar
                                          ▼
                                     API Subscriber ──► Git (-prev fijados a vN) ──► Argo ──► ESO ──► Authorino
```

## Invariantes

- **Escribe sin leer keys (D3), salvo para 3scale.** El cliente de Vault escribe con check-and-set y lee metadata.
  Probado contra Vault 2.1.1 con la policy mínima (`data`: create/update; `metadata`: read/list/update): todo funciona y
  leer un valor da `403`. **Excepción del 29/10 (0.3.0):** para dar de alta la application de una suscripción en 3scale
  con la misma key, la API Consumers la lee (`read_field`, solo desde `threescale.py`) con el mismo rol, al que se le
  suma `read`.
- **La key nunca sale del proceso.** Ni respuestas, ni logs (redacción), ni metadata, ni llamadas al API Subscriber,
  que solo recibe números de versión. Hay un test que la busca en todo eso.
- **Sin estado propio.** Consumidor y rotación viven en la `custom_metadata` del secreto. Unicidad de App ID y
  detección de keys duplicadas (H-03) con índices escritos con `cas=0` en el mount propio de APIM
  (`apim-<tier>/consumers/_index/…` en el lab), donde también queda el registro de rotaciones que retoma `recover`.
- **Identidad de la credencial (0.2.0):** namespace + `environment` + `tenant`. Van en el alta y como query en los
  recursos del consumidor (`GET /v1/consumers/{ns}?environment=QA&tenant=b2c`). El ExternalSecret del consumidor es
  `secret-apim-<tenant>`. La ventana de rotación en el API Subscriber (≥ 0.6.0) también es por tenant.
- **Plataforma (0.4.0, 25/11):** el alta recibe `platform` (`openshift`, default, o `eks`) y, con `eks`, `eksCluster`
  (el nombre del cluster de EKS: `arqc-delta-136`, `arqc-epsilon-136`, `apps-qa-use1-01`, `apps-prd-use1-01`…; no se
  valida contra una lista). Los recursos de un consumidor de EKS llevan `platform=eks&eksCluster=<cluster>` en la
  query; sin `platform` son los de OpenShift, que es lo que pide el API Subscriber. La credencial de OpenShift y la de
  EKS del mismo namespace, ambiente y tenant son dos: un namespace migrado tiene una en cada path.

  | Plataforma | Namespace de Vault | Path de la credencial |
  |---|---|---|
  | `openshift` | `admin/openshift-<ambiente>` | `openshift-<ambiente>/<ns>/data/secret-apim-<tenant>-v2` |
  | `eks` | `admin/eks-<cluster>` | `eks-<cluster>/<ns>/data/secret-apim-<tenant>-v2` |
- **Rotación coordinada (D4):**
  1. Fija la versión vigente N.
  2. El API Subscriber abre la ventana: un `-prev` por suscripción y cluster, fijado a N con `remoteRef.version`.
  3. Recién con la ventana confirmada, escribe N+1 con `cas=N` (falla si alguien escribió en el medio o si N ya no existe) y pide el refresh de los Secrets principales.
  4. `AwaitingConsumer` → `complete` → el Subscriber cierra la ventana.

  Si la ventana no se confirma, la key nueva no se escribe. Abort solo antes del paso 3 (después, roll-forward).
- **Baja:** con suscripciones vivas, 409. Escribe un tombstone sin `app_key` (el `app_id` sale de la metadata); las
  versiones anteriores quedan para auditoría (sin `delete`).

## Desarrollo

```bash
make install        # pip install -e '.[dev]'
make test           # alta, baja, rotación (casos de falla), no-filtrado, contrato OpenAPI, chart
make lint
make run-dev        # API en :8081 (token "dev"): Vault en memoria, API Subscriber simulada
```

Contra un Vault real con la policy mínima de D3:

```bash
APICON_IT_VAULT_ADDR=... APICON_IT_ROLE_ID=... APICON_IT_SECRET_ID=... make it-vault
```

Configuración (`APICON_*`, lista completa en `src/api_consumers/config.py`): Vault (`VAULT_ADDR`, `VAULT_AUTH`
`kubernetes|approle|token`, `VAULT_MOUNT`, `INDEX_PREFIX`), API Subscriber (`SUBSCRIBER_URL`, `SUBSCRIBER_TOKEN`),
rotación (`ROTATION_POLL_S`, `ROTATION_STEP_TIMEOUT_S`, `ROTATION_DEADLINE_DAYS`), `THREESCALE_MODE`, `API_TOKENS`,
`CALLBACK_SECRET`. Lo sensible llega como archivos `APICON_<CAMPO>` montados desde el Secret `api-consumers-secrets`
en `/etc/api-consumers/secrets`.

## Despliegue

Chart en `deploy/charts/api-consumers/`, con la misma jerarquía de 7 capas de values que `gitops/`
(`make helm-template CLUSTER=paas-arqlab`). En los clusters de APIM: Vault corporativo con auth de Kubernetes
(TokenReview) y secretos propios por ESO.

Despliegue reproducible en cualquier cluster, junto con el API Subscriber y los mocks: `bgal-api-sub/deploy/scripts/toolkit-up.sh <cluster>`
(values de `deploy/values/clusters/<cluster>.yaml` de este repo, más la dirección del Vault y el Secret desde el
`.env` del cluster, que no va a Git). Con el Vault corporativo (`clusters/paas-dev.yaml`), el path lleva `/data/`
después del mount (`openshift-{env}/{namespace}/data/secret-apim-{tenant}-v2`): la API Consumers parte ahí y ESO
usa la key tal cual.

Lab (darqtesting01, podman, al lado del API Subscriber):

```bash
VD_OUT=…/.out/vault-dev SUBSCRIBER_TOKEN_FILE=<OUT del Subscriber>/api-token ./deploy/scripts/lab-podman.sh prep
./deploy/scripts/lab-podman.sh vault && ./deploy/scripts/lab-podman.sh run
```

### Credenciales preexistentes (`Unmanaged`): onboarding

Una credencial que dejó otro proceso (DevSecOps, 3scale) no tiene la metadata de la API Consumers: se informa
`Unmanaged` y el API Subscriber no la suscribe (422 "adoptarla antes de suscribir"). Al desafectar 3scale, el pipeline
de DevSecOps crea la credencial en Vault y la da de alta en la API Consumers (adopción, 0.4.0), con el `app_id` que está
en sus datos (la API Consumers no los lee):

```bash
curl -s -X POST "$API_CON_URL/v1/consumers/<ns>/adoption?environment=INT&tenant=b2c&platform=eks&eksCluster=arqc-epsilon-136" \
  -H "Authorization: Bearer $API_CON_TOKEN" -H "Content-Type: application/json" -H "Idempotency-Key: <id>" \
  -d '{"appId": "<app_id>", "owner": "<equipo>", "servicenowRef": "<UR>"}'
```

Escribe la metadata (`origin: adopted`) y reserva el app_id en el índice, sin tocar los datos ni crear una versión.
`dryRun=true` devuelve el plan sin escribir; `PATCH` (`{"appId", "previousAppId", "releasePrevious"}`) corrige el
app_id de una adopción y se niega con suscripciones vivas. Si el app_id ya lo reservó el mismo namespace y tenant (la
otra copia de un namespace migrado de OpenShift a EKS), la reserva se comparte; de otro consumidor, 409. Una vez
adoptada, la API Consumers puede rotarla y darla de baja: acordarlo con el dueño.

Lo mismo a mano desde el bastión (el pod tiene que tener la imagen 0.4.0 o posterior; `PLATFORM=eks EKS_CLUSTER=<c>`
para EKS):

```bash
OWNER="<equipo>" REF=<UR> ./deploy/scripts/adopt-credential.sh adopt <ns> <ambiente> <tenant> <app_id>
./deploy/scripts/adopt-credential.sh fix-app-id <ns> <ambiente> <tenant> <app_id_actual> <app_id_correcto>
```

Muestra el plan y pide confirmación (`DRY_RUN=1` solo el plan). Detalle y variables en el encabezado del script.

## Pendientes conocidos

- **Rol de Vault con la policy de D3** en el kit `vault-dev` (hoy el lab usa el AppRole `apim-subscriber-nonprd`, que además puede leer) y en HCP.
- **Índices fuera de `consumers/`** (`index/*`, como dice el ADR) cuando exista la policy propia.
- **`Lease`** para más de una réplica: hoy las rotaciones en curso las avanza el proceso que las aceptó (o el que arranca, con `recover`).
- **EKS en el API Subscriber:** busca la credencial sin `platform` (OpenShift) y arma el path del gateway con su
  template de OCP. Un consumidor que solo tiene credencial en EKS no se puede suscribir hasta que el Subscriber reciba
  el cluster y use el `vaultPath`. El KV de cada namespace en `eks-<cluster>` y las policies los dan la plataforma y
  DevSecOps.
- **3scale:** applications de las suscripciones desde 0.3.0 (`/v1/consumers/{ns}/threescale/{cluster}/applications/{apiId}`); falta la prueba contra el admin real y la importación de credenciales existentes.
