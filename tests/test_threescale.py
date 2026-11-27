"""0.3.0 (29/10): applications de 3scale de las suscripciones. La API Consumers las crea con el app_id y la app_key de
la credencial (única lectura de la key: excepción a D3) en el admin de 3scale de cada cluster; el API Subscriber 0.11.0
decide los clusters. En los tests, un admin de 3scale en memoria por cluster y tenant."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import KEY, NS, PATH, QS, TENANT, TOKEN, idem, make_service, onboard
from fastapi.testclient import TestClient

from api_consumers.main import create_app
from api_consumers.service import Service
from api_consumers.threescale import Applications, MemoryAdmin

ROOT = Path(__file__).resolve().parents[1]
ADMINS = [
    {"match": r"paas-(qas|dev)", "adminUrl": "https://{t}-admin.{cluster}.example"},
    {"match": r"paas-apps\d+-qapg", "adminUrl": "https://{t}-admin.{cluster}.example"},
]
SERVICES = {"100": "PRDC-greeting-echo", "200": "PRDC-otra"}
APP = f"/v1/consumers/{NS}/threescale/paas-qas/applications/greeting-echo"


@pytest.fixture
def ts(settings):
    s = settings.model_copy(
        update={
            "threescale_admins": json.dumps(ADMINS),
            "threescale_tokens": json.dumps({"paas-qas/b2c": "tok-qas", "paas-apps2-qapg": "tok-apps2"}),
            "threescale_account_email": "{sigla}@example.invalid",
        }
    )
    admins: dict[str, MemoryAdmin] = {}

    def factory(url: str, token: str) -> MemoryAdmin:
        return admins.setdefault(url, MemoryAdmin(SERVICES, plans=["basic", "bronze", "gold"]))

    parts = make_service(s)
    svc: Service = parts["svc"]
    svc.apps = Applications(s, parts["vault"], admin_factory=factory)
    with TestClient(create_app(s, svc)) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield c, parts, admins


def _put(c, plan="bronze", url=APP, q="serviceId=100"):
    return c.put(f"{url}?{QS}&{q}", json={"planId": plan, "consumerSigla": "SIGLA", "name": f"sub-greeting-echo--{NS}"})


def test_alta_con_la_key_de_vault_sin_devolverla(ts):
    c, parts, admins = ts
    cons = onboard(c)
    r = _put(c)
    assert r.status_code == 200, r.text
    view = r.json()
    assert (view["appId"], view["planId"], view["threescalePlan"], view["serviceId"]) == (
        cons["appId"],
        "bronze",
        "bronze",
        "100",
    )
    admin = admins["https://b2c-admin.paas-qas.example"]
    (app,) = admin.apps.values()
    key = parts["vault"].data[PATH][1][KEY]
    assert (
        app["keys"] == [key]
        and app["name"] == "SIGLAApp"
        and admin.accounts["SIGLA"]["email"] == "sigla@example.invalid"
    )
    assert key not in r.text, "la key nunca sale en la respuesta"
    assert c.get(f"{APP}?{QS}&serviceId=100").json()["applicationId"] == app["id"]


def test_put_idempotente_cambia_el_plan_y_delete(ts):
    c, _parts, admins = ts
    onboard(c)
    _put(c)
    r = _put(c, plan="gold")
    assert r.status_code == 200 and r.json()["planId"] == "gold"
    assert len(admins["https://b2c-admin.paas-qas.example"].apps) == 1, "no duplica la application"
    # plan del Subscriber que 3scale no tiene: el default del servicio, y el pedido queda en la descripción
    r = _put(c, plan="no-limits")
    assert (r.json()["planId"], r.json()["threescalePlan"]) == ("no-limits", "basic")
    assert c.delete(f"{APP}?{QS}&serviceId=100").status_code == 204
    assert c.get(f"{APP}?{QS}&serviceId=100").status_code == 404
    assert c.delete(f"{APP}?{QS}&serviceId=100").status_code == 404


def test_servicio_por_system_name_en_otro_cluster(ts):
    """El par pg/mz (u otro admin) puede tener otro id: se busca por system_name."""
    c, _parts, admins = ts
    onboard(c)
    url = f"/v1/consumers/{NS}/threescale/paas-apps2-qapg/applications/greeting-echo"
    r = _put(c, url=url, q="serviceId=999&systemName=PRDC-greeting-echo")
    assert r.status_code == 200, r.text
    assert "https://b2c-admin.paas-apps2-qapg.example" in admins


@pytest.mark.parametrize(
    ("url", "q", "status", "detail"),
    [
        (f"/v1/consumers/{NS}/threescale/paas-prdpg/applications/x", "serviceId=100", 422, "sin admin de 3scale"),
        (f"/v1/consumers/{NS}/threescale/paas-dev/applications/x", "serviceId=100", 422, "sin token"),
        (APP, "serviceId=404", 422, "inexistente"),
    ],
)
def test_rechazos(ts, url, q, status, detail):
    c, _parts, _admins = ts
    onboard(c)
    r = _put(c, url=url, q=q)
    assert r.status_code == status and detail in r.json()["detail"], r.text


def test_sin_admins_501_y_preexistente_409(client, parts):
    r = client.put(f"{APP}?{QS}&serviceId=100", json={"planId": "bronze"})
    assert r.status_code == 404  # sin consumidor
    onboard(client)
    assert client.put(f"{APP}?{QS}&serviceId=100", json={"planId": "bronze"}).status_code == 501


def test_rotacion_agrega_la_key_nueva_y_al_completar_saca_la_vieja(ts):
    c, parts, admins = ts
    onboard(c)
    _put(c)
    parts["subscriber"].subs[NS] = 1
    parts["subscriber"].list_subscriptions = lambda ns: [
        {
            "id": f"sub-greeting-echo--{ns}",
            "tenant": TENANT,
            "clusters": [{"cluster": "paas-qas", "platform": "3scale"}],
        }
    ]
    app = next(iter(admins["https://b2c-admin.paas-qas.example"].apps.values()))
    old = parts["vault"].data[PATH][1][KEY]
    rot = c.post(f"/v1/consumers/{NS}/rotations?{QS}", headers=idem()).json()
    assert rot["state"] == "AwaitingConsumer"
    new = parts["vault"].data[PATH][2][KEY]
    assert app["keys"] == [old, new], "solapamiento: las dos keys valen en 3scale"
    r = c.patch(f"/v1/consumers/{NS}/rotations/{rot['id']}?{QS}", json={"action": "complete"}, headers=idem())
    assert r.json()["state"] == "Completed"
    assert app["keys"] == [new] and parts["vault"].meta[PATH]["threescale_keys"] == "prune:1"


def test_la_lectura_de_la_key_vive_solo_en_threescale():
    """Excepción a D3 (29/10): Vault.read_field solo desde el cliente de 3scale."""
    users = [p.name for p in (ROOT / "src/api_consumers").glob("*.py") if ".read_field(" in p.read_text()]
    assert users == ["threescale.py"]


def test_plan_con_aprobacion_acepta_la_application(ts):
    """H-29: un plan de 3scale con aprobación deja la application pending (no autoriza tráfico). La aprobación ya la dio
    ServiceNow: la API Consumers la acepta."""
    c, _parts, admins = ts
    onboard(c)
    admin = MemoryAdmin(SERVICES, plans=["Prueba"], approval={"Prueba"})
    admins["https://b2c-admin.paas-qas.example"] = admin
    r = _put(c, plan="bronze")
    assert r.status_code == 200 and r.json()["state"] == "live", r.text
    assert next(iter(admin.apps.values()))["state"] == "live"


# ================================================== importación desde 3scale (0.5.0)
QA_ADMIN = "https://b2c-admin.paas-qas.example"
LEGACY_KEY = "e" * 32


def _legacy(admins, app_id="abcd1234", keys=(LEGACY_KEY,), service="100", sigla="SIGLA") -> dict:
    """Una application de 3scale de antes del toolkit: la cuenta de la sigla, su app_id y su key."""
    admin = admins.setdefault(QA_ADMIN, MemoryAdmin(SERVICES, plans=["basic", "bronze", "gold"]))
    acc = admin.create_account(sigla, f"{sigla}@example.invalid")
    app = admin.create_application(acc["id"], f"{service}-basic", f"{sigla}App", "legacy", app_id, keys[0])
    app["keys"] = list(keys)
    return app


def _import(c, ns=NS, **imp):
    body = {"namespace": ns, "sigla": "sigla", "tier": "nonprd", "environment": "QA", "tenant": TENANT}
    return c.post("/v1/consumers", json={**body, "importFrom3scale": {"cluster": "paas-qas", **imp}}, headers=idem())


def test_import_toma_app_id_y_key_de_3scale_y_la_application_sigue_siendo_la_misma(ts):
    c, parts, admins = ts
    app = _legacy(admins)
    r = _import(c, systemName="PRDC-greeting-echo")
    assert r.status_code == 201, r.text
    cons = r.json()
    assert (cons["appId"], cons["origin"], cons["state"]) == ("abcd1234", "3scale-import", "Active")
    assert LEGACY_KEY not in r.text, "la key nunca sale en la respuesta"
    assert parts["vault"].data[PATH][1] == {"app_id_b2c": "abcd1234", KEY: LEGACY_KEY}
    meta = parts["vault"].meta[PATH]
    assert meta["threescale_sigla"] == "SIGLA" and meta["threescale_import_cluster"] == "paas-qas"
    assert (meta["threescale_import_service"], meta["threescale_import_application"]) == ("100", app["id"])
    assert cons["secretApimManifest"], "el alta devuelve el ExternalSecret para el namespace de la app"
    # la suscripción que el API Subscriber pide después en 3scale encuentra la application importada: no duplica
    assert _put(c).status_code == 200 and len(admins[QA_ADMIN].apps) == 1


def test_import_ambiguo_por_servicio_pide_application_id(ts):
    c, _parts, admins = ts
    _legacy(admins, app_id="aaaa0001")
    other = _legacy(admins, app_id="aaaa0002", keys=("d" * 32,))  # otro namespace de la misma sigla
    r = _import(c, serviceId="100")
    assert r.status_code == 409 and "indicar applicationId" in r.json()["detail"], r.text
    r = _import(c, applicationId=other["id"])
    assert r.status_code == 201 and r.json()["appId"] == "aaaa0002", r.text


@pytest.mark.parametrize(
    ("setup", "imp", "status", "detail"),
    [
        ({"keys": ("e" * 32, "f" * 32)}, {"serviceId": "100"}, 409, "2 keys"),
        ({"sigla": "OTRA"}, {"serviceId": "100"}, 422, "no tiene cuenta"),
        ({"service": "200"}, {"serviceId": "100"}, 422, "no tiene esa application"),
        ({}, {"serviceId": "404"}, 422, "inexistente"),
        ({}, {"applicationId": "999"}, 422, "no tiene esa application"),
        ({"app_id": "user key/1"}, {"serviceId": "100"}, 422, "sirva de label"),
    ],
)
def test_import_rechazos_sin_escribir_en_vault(ts, setup, imp, status, detail):
    c, parts, admins = ts
    _legacy(admins, **setup)
    r = _import(c, **imp)
    assert r.status_code == status and detail in r.json()["detail"], r.text
    assert PATH not in parts["vault"].data and "e" * 32 not in r.text


def test_import_app_id_en_uso_409_y_cluster_sin_admin_422(ts):
    c, _parts, admins = ts
    _legacy(admins)
    assert _import(c, serviceId="100").status_code == 201
    r = _import(c, ns="sigla-otro-qa", serviceId="100")  # la misma application para otro namespace
    assert r.status_code == 409 and "ya está en uso" in r.json()["detail"]
    body = {"namespace": "sigla-x-qa", "sigla": "sigla", "tier": "nonprd", "environment": "QA", "tenant": TENANT}
    r = c.post(
        "/v1/consumers", json={**body, "importFrom3scale": {"cluster": "paas-prdpg", "serviceId": "1"}}, headers=idem()
    )
    assert r.status_code == 422 and "sin admin de 3scale" in r.json()["detail"]


def test_import_sin_admins_501_y_sin_selector_400(client):
    body = {"namespace": NS, "sigla": "sigla", "tier": "nonprd", "environment": "QA", "tenant": TENANT}
    r = client.post(
        "/v1/consumers", json={**body, "importFrom3scale": {"cluster": "paas-qas", "serviceId": "1"}}, headers=idem()
    )
    assert r.status_code == 501
    r = client.post("/v1/consumers", json={**body, "importFrom3scale": {"cluster": "paas-qas"}}, headers=idem())
    assert r.status_code == 400 and "applicationId, serviceId o systemName" in r.text
    r = client.post("/v1/consumers", json={**body, "threescale": {"syncCredentials": True}}, headers=idem())
    assert r.status_code == 400, "syncCredentials ya no existe (0.5.0)"
