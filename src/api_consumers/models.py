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
    namespace: str = Field(pattern=DNS1123)
    sigla: str = Field(min_length=1)
    tier: Literal["nonprd", "prd"]
    owner: str | None = None
    servicenowRef: str | None = None
    importFrom3scale: ImportFrom3scale | None = None
    threescale: ThreeScaleSync | None = None


class RotationRequest(_Base):
    callbackUrl: str | None = None


class RotationAction(_Base):
    action: Literal["complete", "abort"]
