"""Paths de Vault y el ExternalSecret secret-apim del consumidor (ADR-gitops §5.4, opción C-a)."""

from __future__ import annotations

import yaml


def credentials_key(namespace: str) -> str:
    """Path de la credencial en el mount apim-<tier> (lo leen ESO del gateway y el secret-apim del consumidor)."""
    return f"consumers/{namespace}/credentials"


def secret_apim_externalsecret(
    *, consumer_ns: str, app_id: str, vault_key: str, store_kind: str, store_name: str, refresh: str, vault_path: str
) -> str:
    obj = {
        "apiVersion": "external-secrets.io/v1",
        "kind": "ExternalSecret",
        "metadata": {"name": "secret-apim", "namespace": consumer_ns},
        "spec": {
            "refreshInterval": refresh,
            "secretStoreRef": {"kind": store_kind, "name": store_name},
            "target": {
                "name": "secret-apim",
                "creationPolicy": "Owner",
                "deletionPolicy": "Retain",
                "template": {
                    "type": "Opaque",
                    "metadata": {
                        "labels": {"apim.bgal/credential-role": "consumer", "app-id": app_id},
                        "annotations": {"apim.bgal/vault-path": vault_path},
                    },
                    "data": {"APP_ID": "{{ .app_id }}", "APP_KEY": "{{ .app_key }}"},
                },
            },
            "dataFrom": [{"extract": {"key": vault_key}}],
        },
    }
    header = "# secret-apim del consumidor: lo commitea el flujo de alta de namespace (ADR-gitops §5.4 C-a).\n"
    return header + yaml.safe_dump(obj, sort_keys=False, default_flow_style=False)
