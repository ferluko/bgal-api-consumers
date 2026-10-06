# CLAUDE.md — bgal-api-consumers

**API Consumers** del toolkit de APIM sobre Kuadrant: la credencial de cada namespace consumidor (App ID / App Key)
en Vault KV v2 (`apim-<tier>/consumers/<ns>/credentials`): alta, baja y rotación coordinada con el API Subscriber.
**Escribe sin leer keys y sin estado propio.** Versión 0.1.0 (`main`).

Contexto completo, estado y backlog: `~/Documents/Galicia/mc/doc/01_apim/RHCL-Kuadrant/api-toolkit-spec/PROMPT-claude-code-api-toolkit.md` (leerlo primero).
Arquitectura: `RHCL-Kuadrant/ADR-api-consumers-y-subscriber-sin-estado.md` (D1, D3, D4, D5). Hermano: `~/Documents/Galicia/mc/bgal-api-sub`
(suscripciones; runbooks del lab y e2e viven ahí). Resultados: `RHCL-Kuadrant/poc-credenciales-suscripcion/HALLAZGOS.md` (H-22, H-23).

## Reglas
- Contrato: `openapi/consumers.yaml` 0.1 (copia de `RHCL-Kuadrant/api-toolkit-spec/openapi/consumers.yaml`; mantener ambas iguales, hay test de rutas ↔ contrato).
- **Escribir sin leer keys (D3):** el cliente de Vault (`vault.py`) no tiene método para leer `data/`; solo escritura con check-and-set (`cas`) y metadata. No agregar lecturas de valores (test). Policy mínima validada contra Vault 2.1.1: `data/consumers/*` create/update · `metadata/consumers/*` read/list/update · sin delete/destroy.
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
| `render.py` | Path de la credencial y ExternalSecret `secret-apim` del consumidor |
| `integrations.py` · `config.py` · `main.py` | 3scale (stub) y callbacks · Settings `APICON_*` · rutas FastAPI |

## Comandos
- `make install` · `make test` (27; ~1 s) · `make lint` · `make run-dev` (Vault en memoria, Subscriber simulado, puerto 8081).
- `make it-vault` con `APICON_IT_VAULT_ADDR/ROLE_ID/SECRET_ID`: integración contra Vault real con la policy de D3 (para local: un `hashicorp/vault:2.1.1` en Docker con esa policy).
- `make helm-template CLUSTER=paas-arqlab` · `make helm-install CLUSTER=paas-arqlab` (namespace `apim-toolkit`, compartido con el Subscriber).
- Imagen (en la Mac, robot de quay `ferlukobgal+para_claudia`): `docker buildx build --platform linux/amd64 -t quay.io/ferlukobgal/bgal-api-consumers:<versión> -f Containerfile --push .`

## Lab (paas-arqlab)
Se edita en la Mac → `git push` → en darqtesting01, en `/app/bgal-api-consumers`, `git pull origin <rama>` (siempre con el remote: los clones del lab pueden tener también `bgal`, la copia en GHE). Despliegue vigente: `bgal-api-sub/docs/RUNBOOK-lab-ocp.md` (Helm).
- `deploy/scripts/lab-secret.sh`: Secret `api-consumers-secrets` (AppRole del kit desde `VD_OUT=/root/poc-cred/.out/vault-dev`, token del Subscriber).
- `deploy/scripts/lab-podman.sh prep|vault|run|status|logs|stop`: en podman en darqtesting01, puerto 18081, `OUT=/root/poc-cred/.out/consumers`; su `api-token` es el `CONSUMERS_TOKEN_FILE` del Subscriber.
- Vault de lab en modo dev: un reinicio de `vault-0` borra todo (`vault-dev/scripts/recover.sh`). Los consumidores dados de alta por el Subscriber 0.1 no tienen `app_id` en la metadata: el e2e usa `e2e-con-lab`.

## Pendiente
Rol AppRole propio con la policy de D3 en `vault-dev` (hoy usa `apim-subscriber-nonprd`, que además lee) · `max_versions` explícito · `Lease` para rotaciones con varias réplicas · 3scale real.
