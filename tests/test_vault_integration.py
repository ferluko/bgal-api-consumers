"""Integración con un Vault real (opcional). Se salta si no está APICON_IT_VAULT_ADDR.

Policy de D3 (ADR API Consumers) sobre la convención de DevSecOps: openshift-<amb>/data/+/secret-apim-* create+update
(SIN read), openshift-<amb>/metadata/... read+list+update, y el índice en apim-<tier>. Verifica que la API Consumers
funciona con eso y que efectivamente no puede leer una key.
"""

import os
import time

import pytest

from api_consumers.config import Settings
from api_consumers.integrations import ThreeScaleStub
from api_consumers.service import Runner, Service
from api_consumers.subscriber import FakeSubscriber
from api_consumers.vault import CasMismatch, HvacVault

ADDR = os.environ.get("APICON_IT_VAULT_ADDR")
pytestmark = pytest.mark.skipif(not ADDR, reason="sin Vault de integración (APICON_IT_VAULT_ADDR)")


def _settings() -> Settings:
    return Settings(
        vault_addr=ADDR,
        vault_auth="approle",
        vault_role_id=os.environ["APICON_IT_ROLE_ID"],
        vault_secret_id=os.environ["APICON_IT_SECRET_ID"],
        worker_mode="inline",
        rotation_poll_s=0.01,
    )


def test_cas_metadata_y_sin_lectura_de_datos():
    hv = HvacVault(_settings())
    ns = f"it-{int(time.time() * 1000)}"
    path = f"openshift-qas/{ns}/secret-apim-b2c-v2"
    assert hv.metadata(path) is None
    assert hv.write(path, {"app_id": "abcd1234", "app_key": "k1"}, cas=0) == 1
    with pytest.raises(CasMismatch):
        hv.write(path, {"app_id": "abcd1234", "app_key": "otra"}, cas=0)
    hv.set_metadata(path, {"sigla": "it", "owner": ""})
    hv.set_metadata(path, {"rotation_state": "Requested"})
    hv.set_metadata(path, {"state": "active"}, drop={"rotation_state"})
    md = hv.metadata(path)
    assert md["current_version"] == 1 and md["custom"] == {"sigla": "it", "state": "active"}
    assert hv.write(path, {"app_id": "abcd1234", "app_key": "k2"}, cas=1) == 2
    assert set(hv.metadata(path)["versions"]) == {1, 2}
    assert ns + "/" in hv.list("openshift-qas")
    import hvac

    with pytest.raises(hvac.exceptions.Forbidden):  # la policy no deja leer valores
        hv.client.secrets.kv.v2.read_secret_version(
            path=path.split("/", 1)[1], mount_point="openshift-qas", raise_on_deleted_version=False
        )


def test_alta_rotacion_y_baja_con_la_policy_minima():
    s = _settings()
    sub = FakeSubscriber()
    svc = Service(s, HvacVault(s), sub, ThreeScaleStub(), Runner("inline", 1))
    ns = f"it-{int(time.time() * 1000)}"
    req = {"namespace": ns, "sigla": "it", "tier": "nonprd", "environment": "QA", "tenant": "b2c"}
    status, c = svc.create_consumer(req, "RITM-IT-1")
    assert status == 201 and c["vaultVersion"] == 1
    cr = svc.cred(ns, "QA", "b2c")
    sub.subs[ns] = 1
    rot = svc.start_rotation(cr, {}, "ROT-IT-1")
    assert rot["state"] == "AwaitingConsumer" and rot["newVersion"] == 2
    assert svc.update_rotation(cr, rot["id"], "complete")["state"] == "Completed"
    sub.subs[ns] = 0
    assert svc.delete_consumer(cr, "DEL-IT-1")["state"] == "Revoked"
