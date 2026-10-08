"""Fixtures: Vault en memoria (sin lectura de datos), API Subscriber simulada, 3scale stub, worker inline."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api_consumers.config import Settings
from api_consumers.integrations import ThreeScaleStub
from api_consumers.main import create_app
from api_consumers.service import Runner, Service
from api_consumers.subscriber import FakeSubscriber
from api_consumers.vault import MemoryVault

TOKEN = "test-token"
NS = "sigla-consumidor-qa"
ENV, TENANT = "QA", "b2c"
Q = {"environment": ENV, "tenant": TENANT}
QS = "environment=QA&tenant=b2c"
# convención de DevSecOps: openshift-<ambiente>/<ns>/secret-apim-<tenant>-v2, claves app_id_<tenant>/app_key_<tenant>
PATH = f"openshift-qas/{NS}/secret-apim-{TENANT}-v2"
ID, KEY = "app_id_b2c", "app_key_b2c"
INDEX = "apim-nonprd/consumers/_index"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        api_tokens=TOKEN,
        vault_mode="memory",
        subscriber_mode="fake",
        threescale_mode="stub",
        worker_mode="inline",
        rotation_poll_s=0.01,
        rotation_step_timeout_s=0.1,
    )


def make_service(settings: Settings, vault: MemoryVault | None = None, sub: FakeSubscriber | None = None) -> dict:
    vault = vault or MemoryVault()
    sub = sub or FakeSubscriber()
    ts = ThreeScaleStub()
    svc = Service(settings, vault, sub, ts, Runner("inline", 1))
    return {"svc": svc, "vault": vault, "subscriber": sub, "threescale": ts}


@pytest.fixture
def parts(settings: Settings) -> dict:
    return make_service(settings)


@pytest.fixture
def client(settings: Settings, parts) -> TestClient:
    with TestClient(create_app(settings, parts["svc"])) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield c


_counter = {"n": 0}


def idem() -> dict:
    _counter["n"] += 1
    return {"Idempotency-Key": f"RITM{2000000 + _counter['n']}"}


def cred(parts: dict, ns: str = NS, tenant: str = TENANT):
    return parts["svc"].cred(ns, ENV, tenant)


def onboard(client: TestClient, ns: str = NS, key: str | None = None, **extra) -> dict:
    body = {"namespace": ns, "sigla": "sigla", "tier": "nonprd", **Q, **extra}
    r = client.post("/v1/consumers", json=body, headers={"Idempotency-Key": key} if key else idem())
    assert r.status_code == 201, r.text
    return r.json()
