"""El ExternalSecret secret-apim-<tenant> del consumidor (ADR-gitops §5.4, opción C-a).

El path de la credencial lo arma `Settings.credential_path_template` (convención de DevSecOps); acá solo se renderiza.
"""

from __future__ import annotations

import yaml


def secret_apim_externalsecret(
    *,
    consumer_ns: str,
    tenant: str,
    app_id: str,
    vault_key: str,
    app_id_field: str,
    app_key_field: str,
    store_kind: str,
    store_name: str,
    refresh: str,
) -> str:
    """`vault_key` es el path completo (el primer segmento es el mount): la store del consumidor no fija mount."""
    name = f"secret-apim-{tenant}"
    obj = {
        "apiVersion": "external-secrets.io/v1",
        "kind": "ExternalSecret",
        "metadata": {"name": name, "namespace": consumer_ns},
        "spec": {
            "refreshInterval": refresh,
            "secretStoreRef": {"kind": store_kind, "name": store_name},
            "target": {
                "name": name,
                "creationPolicy": "Owner",
                "deletionPolicy": "Retain",
                "template": {
                    "type": "Opaque",
                    "metadata": {
                        "labels": {
                            "apim.bgal/credential-role": "consumer",
                            "app-id": app_id,
                            "apim.bgal/tenant": tenant,
                        },
                        "annotations": {"apim.bgal/vault-path": vault_key},
                    },
                    "data": {"APP_ID": "{{ ." + app_id_field + " }}", "APP_KEY": "{{ ." + app_key_field + " }}"},
                },
            },
            "dataFrom": [{"extract": {"key": vault_key}}],
        },
    }
    header = "# secret-apim del consumidor: lo commitea el flujo de alta de namespace (ADR-gitops §5.4 C-a).\n"
    return header + yaml.safe_dump(obj, sort_keys=False, default_flow_style=False)
