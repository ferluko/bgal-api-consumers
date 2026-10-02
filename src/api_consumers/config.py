"""Configuración por variables de entorno (prefijo APICON_) y archivos de secretos."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SECRETS_DIR = "/etc/api-consumers/secrets"


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
    vault_mount: str = ""  # default: apim-<tier>
    vault_verify: bool = True
    # Índices de unicidad (app-id, hash de key) escritos con cas=0. Lab: dentro de consumers/ (policy existente);
    # objetivo del ADR: un prefijo propio (p. ej. "index").
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
