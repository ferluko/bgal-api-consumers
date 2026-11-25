"""Modelos de request (validación). Las respuestas se arman en service.*_view según openapi/consumers.yaml."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DNS1123 = r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$"


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImportFrom3scale(_Base):
    tenant: str | None = None
    applicationId: str | None = None


class ThreeScaleSync(_Base):
    syncCredentials: bool = False
    tenant: str | None = None
    applicationId: str | None = None


class ConsumerRequest(_Base):
    """Alta de la credencial de un namespace para un ambiente y un tenant. Un namespace puede tener una por tenant (y
    por plataforma: la copia de un namespace migrado de OpenShift a EKS). Path según `platform` (0.4.0):
    openshift-<ambiente>/<ns>/secret-apim-<tenant>-v2 o eks-<eksCluster>/<ns>/secret-apim-<tenant>-v2."""

    namespace: str = Field(pattern=DNS1123)
    sigla: str = Field(min_length=1)
    tier: Literal["nonprd", "prd"]
    environment: str = Field(min_length=1, max_length=16)
    tenant: Literal["b2b", "b2c"]
    platform: Literal["openshift", "eks"] | None = None  # default openshift
    eksCluster: str | None = Field(default=None, pattern=DNS1123)  # solo con eks (obligatorio ahí)
    owner: str | None = None
    servicenowRef: str | None = None
    importFrom3scale: ImportFrom3scale | None = None
    threescale: ThreeScaleSync | None = None


class AdoptionRequest(_Base):
    """Onboarding de una credencial preexistente (`Unmanaged`) con el app_id que informa su dueño o DevSecOps: lo que
    hacía `adopt-credential.sh adopt` (0.4.0). Nunca la key."""

    appId: str = Field(min_length=1, max_length=63)
    sigla: str | None = None
    owner: str | None = None
    servicenowRef: str | None = None
    skipIndex: bool = False  # no reserva el app_id en el índice (reserva vieja de la misma credencial)


class AppIdFix(_Base):
    """Corrección del app_id de una adopción (`adopt-credential.sh fix-app-id`, 0.4.0)."""

    appId: str = Field(min_length=1, max_length=63)
    previousAppId: str = Field(min_length=1, max_length=63)
    releasePrevious: bool = False  # borra la reserva del app_id viejo si la hizo la adopción


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
