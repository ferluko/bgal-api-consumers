"""Configuración por variables de entorno (prefijo APICON_) y archivos de secretos."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SECRETS_DIR = "/etc/api-consumers/secrets"
# environment (como lo informa ServiceNow o el alta del namespace) → ambiente de Vault (openshift-<ambiente>)
VAULT_ENVS = {"dev": "dev", "int": "int", "qa": "qas", "qas": "qas", "prd": "prd"}
TENANTS = ("b2b", "b2c")


class Settings(BaseSettings):
    """Fuentes, de mayor a menor prioridad: variables APICON_*, .env y archivos en el directorio de secretos.

    Lo sensible o propio de cada entorno (credenciales de Vault, URL y token del API Subscriber, tokens de la API) no
    tiene default en el código: llega como archivo `APICON_<CAMPO>` montado desde un Secret de Kubernetes en
    APICON_SECRETS_DIR (default /etc/api-consumers/secrets).
    """

    model_config = SettingsConfigDict(env_prefix="APICON_", env_file=".env", extra="ignore")

    tier: Literal["nonprd", "prd"] = "nonprd"

    # --- API ---------------------------------------------------------------
    api_tokens: str = Field(default="", description="Lista separada por comas. Vacío = sin auth (solo dev).")

    # --- Vault (KV v2): escribe con cas, nunca lee valores (ADR API Consumers D3) ---
    vault_mode: Literal["hvac", "memory"] = "hvac"
    vault_addr: str = "http://127.0.0.1:8200"
    vault_auth: Literal["token", "approle", "kubernetes"] = "kubernetes"
    vault_token: str = ""
    vault_role_id: str = ""
    vault_secret_id: str = ""
    vault_k8s_role: str = "api-consumers"
    vault_k8s_mount: str = "kubernetes"
    vault_namespace: str = ""
    vault_verify: bool = True
    # Credencial del consumidor: convención de DevSecOps (06/10 y 07/10). Una por namespace y tenant; el primer
    # segmento es el mount KV v2 (openshift-<ambiente>). Claves app_id_<tenant> / app_key_<tenant>.
    credential_path_template: str = "openshift-{env}/{namespace}/secret-apim-{tenant}-v2"
    app_id_field: str = "app_id_{tenant}"
    app_key_field: str = "app_key_{tenant}"
    vault_envs: dict[str, str] = Field(default_factory=lambda: dict(VAULT_ENVS))
    # Índices de unicidad (app-id, hash de key) y registro de rotaciones, escritos con cas=0 en un mount propio de
    # APIM (default apim-<tier>). Lab: dentro de consumers/ (policy del kit); objetivo del ADR: un prefijo propio.
    vault_mount: str = ""  # default: apim-<tier>
    index_prefix: str = "consumers/_index"

    # --- API Subscriber (ventana de rotación y suscripciones vivas) --------
    subscriber_mode: Literal["http", "fake"] = "http"
    subscriber_url: str = ""
    subscriber_token: str = ""
    subscriber_verify: bool = True
    subscriber_ca_file: str = ""
    subscriber_timeout_s: float = 15
    subscriber_refresh_timeout_s: int = 60

    # --- secret-apim del consumidor (ExternalSecret que devuelve el alta) ---
    consumer_store_kind: str = "ClusterSecretStore"
    consumer_store_name: str = ""  # default: vault-apim-consumer-<tier>
    secret_apim_refresh: str = "5m"  # noqa: S105 (intervalo de ESO, no un secreto)

    # --- Rotación ----------------------------------------------------------
    rotation_deadline_days: int = 7
    rotation_poll_s: float = 5
    rotation_step_timeout_s: float = 900  # espera de cada operación del API Subscriber

    # --- Integraciones -----------------------------------------------------
    threescale_mode: Literal["disabled", "stub"] = "disabled"
    callback_secret: str = ""
    callback_retries: int = 3
    callback_timeout_s: float = 5

    # --- Runtime -----------------------------------------------------------
    listen_host: str = "0.0.0.0"  # noqa: S104
    listen_port: int = 8080
    worker_mode: Literal["background", "inline"] = "background"
    worker_threads: int = 4
    log_level: str = "INFO"

    @field_validator(
        "vault_token",
        "vault_role_id",
        "vault_secret_id",
        "subscriber_url",
        "subscriber_token",
        "callback_secret",
        "api_tokens",
        mode="after",
    )
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @property
    def mount(self) -> str:
        return self.vault_mount or f"apim-{self.tier}"

    @property
    def index_root(self) -> str:
        return f"{self.mount}/{self.index_prefix.strip('/')}"

    def vault_env(self, environment: str | None) -> str | None:
        """openshift-<ambiente> para un environment (QA → qas); None si no está mapeado."""
        return {k.lower(): v for k, v in self.vault_envs.items()}.get((environment or "").strip().lower())

    @property
    def consumer_store(self) -> str:
        return self.consumer_store_name or f"vault-apim-consumer-{self.tier}"

    @property
    def tokens(self) -> set[str]:
        return {t.strip() for t in self.api_tokens.split(",") if t.strip()}


def secrets_dir() -> str | None:
    d = os.environ.get("APICON_SECRETS_DIR", SECRETS_DIR)
    return d if Path(d).is_dir() else None


@lru_cache
def get_settings() -> Settings:
    return Settings(_secrets_dir=secrets_dir())
