import re

import yaml
from conftest import ID, INDEX, KEY, NS, PATH, QS, TENANT, Q, idem, onboard


def test_alta_escribe_la_credencial_y_devuelve_secret_apim(client, parts):
    c = onboard(client)
    assert re.fullmatch(r"[a-f0-9]{8}", c["appId"]) and c["state"] == "Active" and c["vaultVersion"] == 1
    assert c["vaultPath"] == PATH and c["vaultProperty"] == KEY and c["origin"] == "generated"
    assert (c["environment"], c["tenant"]) == ("QA", TENANT)
    v1 = parts["vault"].data[PATH][1]
    assert v1[ID] == c["appId"] and re.fullmatch(r"[a-f0-9]{32}", v1[KEY])
    meta = parts["vault"].meta[PATH]
    assert meta["app_id"] == c["appId"] and meta["state"] == "active" and meta["created_by"] == "api-consumers"
    assert "servicenow_ref" not in meta  # Vault rechaza valores vacíos
    es = yaml.safe_load(c["secretApimManifest"].split("\n", 1)[1])
    assert es["kind"] == "ExternalSecret" and es["metadata"]["namespace"] == NS
    assert es["metadata"]["name"] == "secret-apim-b2c"
    assert es["spec"]["dataFrom"][0]["extract"]["key"] == PATH  # la store del consumidor no fija mount
    assert es["spec"]["target"]["template"]["data"] == {"APP_ID": "{{ .app_id_b2c }}", "APP_KEY": "{{ .app_key_b2c }}"}
    assert es["spec"]["secretStoreRef"]["name"] == "vault-apim-consumer-nonprd"
    # índices de unicidad (cas=0), en el mount propio de APIM
    assert parts["vault"].current[f"{INDEX}/app-ids/{c['appId']}"] == 1


def test_alta_idempotente_409_y_422(client):
    a = onboard(client, key="RITM0000100")
    body = {"namespace": NS, "sigla": "sigla", "tier": "nonprd", **Q}
    r = client.post("/v1/consumers", json=body, headers={"Idempotency-Key": "RITM0000100"})
    assert r.status_code == 200 and r.json()["appId"] == a["appId"]
    r = client.post("/v1/consumers", json={**body, "owner": "otro"}, headers={"Idempotency-Key": "RITM0000100"})
    assert r.status_code == 422
    assert client.post("/v1/consumers", json=body, headers=idem()).status_code == 409


def test_app_id_tomado_se_regenera(client, parts, monkeypatch):
    from api_consumers import service as svc_mod

    ids = iter(["aaaaaaaa", "aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(svc_mod, "generate_app_id", lambda: next(ids))
    assert onboard(client)["appId"] == "aaaaaaaa"
    assert onboard(client, ns="sigla-otro-qa")["appId"] == "bbbbbbbb"  # aaaaaaaa ya estaba en el índice


def test_import_desde_3scale_y_colision(client, parts):
    parts["threescale"].apps[("galicia", "app-1")] = ("abcd1234", "f" * 32)
    c = onboard(client, ns="sigla-migrada-qa", importFrom3scale={"tenant": "galicia", "applicationId": "app-1"})
    assert c["appId"] == "abcd1234" and c["origin"] == "3scale-import"
    parts["threescale"].apps[("galicia", "app-2")] = ("99999999", "f" * 32)  # misma key → H-03
    body = {"namespace": "sigla-otra-qa", "sigla": "o", "tier": "nonprd", **Q}
    imp = {"importFrom3scale": {"tenant": "galicia", "applicationId": "app-2"}}
    r = client.post("/v1/consumers", json={**body, **imp}, headers=idem())
    assert r.status_code == 409 and "colisiona" in r.json()["detail"]


def test_sync_con_3scale(client, parts):
    c = onboard(client, threescale={"syncCredentials": True, "tenant": "galicia", "applicationId": "app-9"})
    assert c["credentialSync"]["threescale"] == "InSync"
    assert parts["threescale"].apps[("galicia", "app-9")][0] == c["appId"]


def test_get_y_secret_apim(client):
    assert client.get(f"/v1/consumers/{NS}?{QS}").status_code == 404
    c = onboard(client)
    got = client.get(f"/v1/consumers/{NS}?{QS}").json()
    assert got["appId"] == c["appId"] and "secretApimManifest" not in got
    r = client.get(f"/v1/consumers/{NS}/secret-apim?{QS}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/yaml")
    assert yaml.safe_load(r.text.split("\n", 1)[1])["metadata"]["name"] == "secret-apim-b2c"


def test_baja_con_suscripciones_vivas_409(client, parts):
    onboard(client)
    parts["subscriber"].subs[NS] = 2
    r = client.delete(f"/v1/consumers/{NS}?{QS}", headers=idem())
    assert r.status_code == 409 and "suscripciones vivas" in r.json()["detail"]


def test_baja_escribe_tombstone_sin_leer_la_key_y_se_puede_volver_a_dar_de_alta(client, parts):
    c = onboard(client)
    r = client.delete(f"/v1/consumers/{NS}?{QS}", headers=idem())
    assert r.status_code == 200 and r.json()["state"] == "Revoked" and r.json()["vaultVersion"] == 2
    tomb = parts["vault"].data[PATH][2]
    assert tomb[ID] == c["appId"] and KEY not in tomb  # el app_id salió de la metadata
    assert parts["vault"].data[PATH][1][KEY]  # la versión anterior queda (auditoría, sin delete)
    assert client.delete(f"/v1/consumers/{NS}?{QS}", headers=idem()).status_code == 200  # idempotente
    assert client.get(f"/v1/consumers/{NS}/secret-apim?{QS}").status_code == 404
    again = onboard(client)
    assert again["state"] == "Active" and again["vaultVersion"] == 3 and again["appId"] != c["appId"]


def test_una_credencial_por_tenant(client, parts):
    """Un namespace puede tener credencial b2b y b2c: paths, app IDs y estado independientes."""
    b2c = onboard(client)
    b2b = onboard(client, tenant="b2b")
    assert b2b["vaultPath"] == f"openshift-qas/{NS}/secret-apim-b2b-v2" and b2b["appId"] != b2c["appId"]
    assert set(parts["vault"].data[b2b["vaultPath"]][1]) == {"app_id_b2b", "app_key_b2b"}
    assert client.get(f"/v1/consumers/{NS}", params={**Q, "tenant": "b2b"}).json()["appId"] == b2b["appId"]
    # las suscripciones vivas de un tenant no bloquean la baja del otro
    parts["subscriber"].subs[NS] = 1  # FakeSubscriber: tenant b2c
    r = client.delete(f"/v1/consumers/{NS}", params={**Q, "tenant": "b2b"}, headers=idem())
    assert r.status_code == 200 and r.json()["state"] == "Revoked"
    assert client.delete(f"/v1/consumers/{NS}?{QS}", headers=idem()).status_code == 409


def test_ambiente_y_tenant_validos(client):
    body = {"namespace": NS, "sigla": "s", "tier": "nonprd"}
    r = client.post("/v1/consumers", json={**body, "environment": "UAT", "tenant": "b2c"}, headers=idem())
    assert r.status_code == 422 and "ambiente UAT" in r.json()["detail"]
    assert client.post("/v1/consumers", json={**body, "environment": "QA"}, headers=idem()).status_code == 400
    assert client.get(f"/v1/consumers/{NS}").status_code == 400  # sin ambiente ni tenant
    assert client.get(f"/v1/consumers/{NS}", params={**Q, "tenant": "b2x"}).status_code == 400
