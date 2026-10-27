# CLAUDE.md — bgal-api-consumers

**API Consumers** del toolkit de APIM sobre Kuadrant: la credencial de cada namespace consumidor (App ID / App Key)
en Vault KV v2 con la convención de DevSecOps (`openshift-<ambiente>/<ns>/secret-apim-<tenant>-v2`, claves
`app_id_<tenant>`/`app_key_<tenant>`): alta, baja y rotación coordinada con el API Subscriber. Una credencial por
namespace y tenant. **Escribe sin leer keys y sin estado propio.** Versión 0.2.2 (paths del Vault corporativo con `/data/`; credenciales preexistentes `Unmanaged`).

Contexto completo, estado y backlog: `~/Documents/Galicia/mc/doc/01_apim/RHCL-Kuadrant/api-toolkit-spec/PROMPT-claude-code-api-toolkit.md` (leerlo primero).
Arquitectura: `RHCL-Kuadrant/ADR-api-consumers-y-subscriber-sin-estado.md` (D1, D3, D4, D5). Hermano: `~/Documents/Galicia/mc/bgal-api-sub`
(suscripciones; runbooks del lab y e2e viven ahí). Resultados: `RHCL-Kuadrant/poc-credenciales-suscripcion/HALLAZGOS.md` (H-22, H-23).

## Reglas
- Contrato: `openapi/consumers.yaml` 0.2.2-draft (copia de `RHCL-Kuadrant/api-toolkit-spec/openapi/consumers.yaml`; mantener ambas iguales, hay test de rutas ↔ contrato).
- **Credenciales preexistentes (0.2.2):** la API Consumers solo opera sobre lo que creó o importó (`created_by: api-consumers` o `app_id` en la metadata; `_managed`). Una credencial que dejó otro proceso (DevSecOps, 3scale) se informa `Unmanaged` y rotarla, darla de baja o darla de alta da 409 **antes de escribir**: con `app_id` vacío le rompería la credencial al consumidor real (27/10, `agfi-agrofinan-qas` en paas-dev). Adoptarlas es pendiente (con el job de sembrar el índice).
- **Escribir sin leer keys (D3):** el cliente de Vault (`vault.py`) no tiene método para leer `data/`; solo escritura con check-and-set (`cas`) y metadata. No agregar lecturas de valores (test). Policy mínima validada contra Vault 2.1.1 (con el path viejo): `data/…` create/update · `metadata/…` read/list/update · sin delete/destroy. Con la convención nueva: `openshift-<amb>/data/+/secret-apim-*` create/update, `openshift-<amb>/metadata/*` read/list/update y el índice en `apim-<tier>`.
- **Identidad de la credencial:** `Cred` (namespace, environment, tenant → path). `environment` y `tenant` van en el alta y como query obligatoria en todos los recursos de un consumidor. Paths de Vault completos (`split_mount` en `HvacVault`): con `/data/`, el mount es todo lo anterior (Vault corporativo, 26/10: `openshift-<amb>/<ns>/data/secret-apim-<tenant>-v2`, namespace de Vault `openshift-<amb>` con un KV por namespace de OpenShift); sin `/data/`, el primer segmento (Vault de lab, índices `apim-<tier>/…`).
- El App Key existe en el proceso solo al generarlo (alta y rotación) y al sincronizarlo con 3scale; nunca en respuestas, logs (redacción), metadata ni llamadas al API Subscriber (solo viajan números de versión).
- **Sin estado propio:** consumidor y rotación en la `custom_metadata` del secreto. Vault rechaza valores vacíos: se omiten; para borrar una clave se usa `drop` (`_set_rot` con valor vacío).
- Unicidad con índices escritos con `cas=0` bajo `APICON_INDEX_PREFIX` (lab: `consumers/_index/`, para que los cubra la policy del kit; objetivo: prefijo propio).
- Rotación (D4): fijar `current_version` = N → `PUT /v1/credential-overlaps/{ns}` en el Subscriber → esperar su operación → verificar que N exista → escribir N+1 con `cas=N` → `POST …/refresh` → `AwaitingConsumer` → `complete` → `DELETE` de la ventana → `Completed`. Si la ventana no se confirma, N ya no existe o alguien escribió en el medio: `Failed` **sin escribir la key nueva**. Abort solo antes de `KeyWritten` (roll-forward).
- Baja: 409 con suscripciones vivas (consulta al Subscriber) o con rotación en curso; tombstone sin `app_key`, el `app_id` sale de la metadata.
- Antes de commitear: `make test lint`. Chart: `make helm-template CLUSTER=paas-arqlab`. Subir versión en `pyproject.toml`, `src/api_consumers/__init__.py` y `Chart.yaml` en cada cambio de comportamiento; no pisar un tag publicado.
- Credenciales y URLs como archivos `APICON_*` desde el Secret (`APICON_SECRETS_DIR`), nunca en values ni en el entorno.
- No usar el término "bot". Nunca force-push ni secretos en Git. Commits en español.

## Mapa del código (`src/api_consumers/`)
| Módulo | Qué hace |
|---|---|
| `service.py` | Alta/baja/consulta, máquina de estados de la rotación (`_drive`, `_write_new_key`, `recover`), callbacks |
| `vault.py` | `HvacVault` y `MemoryVault` (sin lectura de datos; `write(cas)`, `metadata`, `set_metadata(drop)`, `list`) |
| `subscriber.py` | Cliente del API Subscriber (`HttpSubscriber`, `FakeSubscriber`) |
| `render.py` | ExternalSecret `secret-apim-<tenant>` del consumidor (el path lo arma `Settings.credential_path_template`) |
| `integrations.py` · `config.py` · `main.py` | 3scale (stub) y callbacks · Settings `APICON_*` · rutas FastAPI |

## Comandos
- `make install` · `make test` (34 + 2 de integración que se saltan sin Vault; ~1 s) · `make lint` · `make run-dev` (Vault en memoria, Subscriber simulado, puerto 8081).
- `make it-vault` con `APICON_IT_VAULT_ADDR/ROLE_ID/SECRET_ID`: integración contra Vault real con la policy de D3 (para local: un `hashicorp/vault:2.1.1` en Docker con esa policy).
- `make helm-template CLUSTER=paas-arqlab` · `make helm-install CLUSTER=paas-arqlab` (namespace del archivo del cluster o `apim-toolkit`, compartido con el Subscriber; `HELM_ARGS='--set …'`).
- **paas-dev** (`deploy/values/clusters/paas-dev.yaml`): Vault corporativo, path con `/data/`, índices en `apim-nonprod`, namespace `kuad-kuadrantorqs-dev`. La dirección del Vault no va en este repo (es **público**): la pasa `bgal-api-sub/deploy/scripts/toolkit-up.sh` desde su `deploy/env/<cluster>.conf`. Sin ella el chart falla.
- Imagen (en la Mac, robot de quay `ferlukobgal+para_claudia`): `docker buildx build --platform linux/amd64 -t quay.io/ferlukobgal/bgal-api-consumers:<versión> -f Containerfile --push .`

## Lab (paas-arqlab)
Se edita en la Mac → `git push` → en darqtesting01, en `/app/bgal-api-consumers`, `git pull origin <rama>` (siempre con el remote: los clones del lab pueden tener también `bgal`, la copia en GHE). Despliegue vigente: `bgal-api-sub/docs/RUNBOOK-lab-ocp.md` (Helm).
- `deploy/scripts/lab-secret.sh`: Secret `api-consumers-secrets` (AppRole del kit desde `VD_OUT=/root/poc-cred/.out/vault-dev`, token del Subscriber).
- `deploy/scripts/lab-podman.sh prep|vault|run|status|logs|stop`: en podman en darqtesting01, puerto 18081, `OUT=/root/poc-cred/.out/consumers`; su `api-token` es el `CONSUMERS_TOKEN_FILE` del Subscriber.
- Vault de lab en modo dev: un reinicio de `vault-0` borra todo (`vault-dev/scripts/recover.sh`). Los consumidores dados de alta por el Subscriber 0.1 no tienen `app_id` en la metadata: el e2e usa `e2e-con-lab`.

## Pendiente
**Antes de migrar desde 3scale, job de migración en dos pasos (por tier):** (1) **sembrar** `_index/app-ids/<app_id>` (`cas=0`) con los app_id ya provisionados en 3scale (solo app_id, no es secreto; con el rol de la API Consumers; idempotente: congelar la creación de apps en 3scale en el corte o re-sembrar hasta el corte) y (2) **adoptar** las credenciales preexistentes (`Unmanaged`): un rol aparte con `read` (DevSecOps o de un solo uso, nunca la API Consumers: D3) lee el `app_id_<tenant>` de los datos, lo compara con 3scale si la app existe (se frena ante una diferencia), escribe la metadata (`app_id`, `origin: adopted`, `created_by`, owner, sigla) y reserva el app_id en el índice; recién ahí se desbloquean rotación y baja. Sin el paso 1, un alta generada puede chocar con un app_id de 3scale (~2% con 10.000 + 10.000) y trabar su importación (409). Hoy no hay endpoint ni job · Rol propio con la policy de D3 (auth de Kubernetes, acordado con DevSecOps el 06/10; hoy usa el AppRole `apim-subscriber-nonprd` del kit, que además lee) · adoptar las credenciales que ya existen (`secret-appim-<tenant>-v2` de 3scale) sin leer el `app_id` · `max_versions` explícito · `Lease` para rotaciones con varias réplicas · 3scale real · el `recover` lee el registro de rotaciones del índice (antes recorría `consumers/`).
