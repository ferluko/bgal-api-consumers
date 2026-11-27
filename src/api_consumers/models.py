"""Modelos de request (validación). Las respuestas se arman en service.*_view según openapi/consumers.yaml."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DNS1123 = r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$"


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImportFrom3scale(_Base):
    """Migración desde 3scale (0.5.0): el app_id y la key de la application del consumidor en el admin de 3scale de
    `cluster` (cuenta de la sigla). La application se elige por `applicationId` (id interno de 3scale) o por el
    servicio (`serviceId` o `systemName`)."""

    cluster: str = Field(min_length=1, max_length=63)
    systemName: str | None = Field(default=None, min_length=1)  # noqa: N815
    serviceId: str | None = Field(default=None, min_length=1)  # noqa: N815
    applicationId: str | None = Field(default=None, min_length=1)  # noqa: N815

    @model_validator(mode="after")
    def _which(self) -> ImportFrom3scale:
        if not (self.applicationId or self.serviceId or self.systemName):
            raise ValueError("importFrom3scale requiere applicationId, serviceId o systemName")
        return self


class ConsumerRequest(_Base):
    """Alta de la credencial de un namespace para un ambiente y un tenant (path de APICON_CREDENTIAL_PATH_TEMPLATE:
    openshift-<ambiente>/<ns>/secret-apim-<tenant>-v2, o <ambiente>/<ns>/… en el namespace de Vault `apim`). Un
    namespace puede tener una por tenant."""

    namespace: str = Field(pattern=DNS1123)
    sigla: str = Field(min_length=1)
    tier: Literal["nonprd", "prd"]
    environment: str = Field(min_length=1, max_length=16)
    tenant: Literal["b2b", "b2c"]
    owner: str | None = None
    servicenowRef: str | None = None
    importFrom3scale: ImportFrom3scale | None = None


class RotationRequest(_Base):
    callbackUrl: str | None = None


class RotationAction(_Base):
    action: Literal["complete", "abort"]


class ThreeScaleApplicationRequest(_Base):
    """Lo que manda el API Subscriber (0.11.0) para la application de una suscripción en 3scale. Nunca la key."""

    planId: str = Field(min_length=1)  # noqa: N815
    consumerSigla: str | None = None  # noqa: N815
    approvalRef: str | None = None  # noqa: N815
    name: str | None = None  # subscriptionId del API Subscriber (va en la descripción de la application)
