# CLAUDE.md — bgal-api-consumers

MVP de la **API Consumers** (toolkit de APIM sobre Kuadrant): la credencial del consumidor (App ID / App Key por
namespace) en Vault. Contexto y backlog:
`~/Documents/Galicia/mc/doc/01_apim/RHCL-Kuadrant/api-toolkit-spec/PROMPT-claude-code-api-toolkit.md` (leerlo primero).
Arquitectura: `RHCL-Kuadrant/ADR-api-consumers-y-subscriber-sin-estado.md` (D1, D3, D4, D5). Hermano: `bgal-api-sub`.

Reglas de este repo:
- Contrato: `openapi/consumers.yaml` (copia de `RHCL-Kuadrant/api-toolkit-spec/openapi/consumers.yaml`; mantener ambas iguales).
- **Escribir sin leer keys (D3):** el cliente de Vault no tiene método para leer datos; solo escritura con check-and-set y metadata. No agregar lecturas de `data/` (`tests/test_no_leak_contract.py`).
- El App Key nunca sale del proceso: ni respuestas, ni logs, ni metadata, ni llamadas al API Subscriber (que solo recibe números de versión).
- **Sin estado propio:** el estado del consumidor y de la rotación vive en la `custom_metadata` de Vault. Vault rechaza valores vacíos: se omiten.
- Rotación (D4): fijar la versión N → el API Subscriber abre la ventana (`-prev` fijados a N) y confirma → recién ahí N+1 con `cas=N` → refresh → AwaitingConsumer → complete cierra la ventana. Sin abort después de escribir la key nueva (roll-forward).
- Vault sin delete: la baja escribe un tombstone (el `app_id` sale de la metadata).
- Antes de commitear: `make test lint`. Chart: `make helm-template CLUSTER=paas-arqlab`. Imagen: `quay.io/ferlukobgal/bgal-api-consumers` (build `--platform linux/amd64`).
- Deploy con la jerarquía de 7 capas de values de `gitops/` (`deploy/values/`). Credenciales y URLs como archivos desde el Secret, nunca en values ni en el entorno.
- No usar el término "bot" (es API Publisher / API Subscriber / API Consumers). Nunca force-push ni secretos en Git.
