import re

import yaml
from conftest import EKS, EKS_PATH, ID, INDEX, KEY, NS, PATH, QS, QSE, TENANT, Q, idem, onboard


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


def test_split_mount_con_y_sin_data():
    """Vault corporativo (26/10): con `/data/`, el mount es todo lo anterior (namespace de Vault + KV por namespace)."""
    from api_consumers.vault import split_mount

    assert split_mount(f"openshift-qas/{NS}/data/secret-apim-b2c-v2") == (f"openshift-qas/{NS}", "secret-apim-b2c-v2")
    assert split_mount(f"openshift-qas/{NS}/secret-apim-b2c-v2") == ("openshift-qas", f"{NS}/secret-apim-b2c-v2")
    assert split_mount("apim-nonprod/consumers/_index/app-ids/x") == ("apim-nonprod", "consumers/_index/app-ids/x")
    assert split_mount("apim-nonprod", root_ok=True) == ("apim-nonprod", "")
    assert split_mount(f"openshift-qas/{NS}/data/", root_ok=True) == (f"openshift-qas/{NS}", "")


def test_alta_con_el_path_del_vault_corporativo(client, parts, settings):
    """El path con `/data/` llega igual al ExternalSecret del consumidor: ESO lo usa tal cual."""
    settings.credential_path_template = "openshift-{env}/{namespace}/data/secret-apim-{tenant}-v2"
    c = onboard(client)
    path = f"openshift-qas/{NS}/data/secret-apim-{TENANT}-v2"
    assert c["vaultPath"] == path and parts["vault"].data[path][1][KEY]
    es = yaml.safe_load(c["secretApimManifest"].split("\n", 1)[1])
    assert es["spec"]["dataFrom"][0]["extract"]["key"] == path


def _preexistente(parts, ns="agfi-agrofinan-qas"):
    """Credencial que dejó otro proceso (DevSecOps, 3scale): datos en Vault, versión 3, sin la metadata de la API
    Consumers (ni created_by ni app_id)."""
    path = f"openshift-qas/{ns}/secret-apim-{TENANT}-v2"
    for v in range(3):
        parts["vault"].write(path, {ID: "a1b2c3d4", KEY: f"k{v}"}, cas=v)
    return ns, path


def test_preexistente_no_administrada_unmanaged_y_sin_operaciones_destructivas(client, parts):
    """paas-dev (27/10): agfi-agrofinan-qas aparecía Active sin appId; rotarla o darla de baja escribía app_id vacío."""
    ns, path = _preexistente(parts)
    c = client.get(f"/v1/consumers/{ns}?{QS}").json()
    assert c["state"] == "Unmanaged" and "appId" not in c and "origin" not in c and c["vaultVersion"] == 3
    before = dict(parts["vault"].data[path])
    r = client.post(f"/v1/consumers/{ns}/rotations?{QS}", json={}, headers=idem())
    assert r.status_code == 409 and "no administrada" in r.json()["detail"]
    r = client.delete(f"/v1/consumers/{ns}?{QS}", headers=idem())
    assert r.status_code == 409 and "no administrada" in r.json()["detail"]
    assert parts["vault"].data[path] == before and parts["vault"].current[path] == 3, "no escribió nada"
    body = {"namespace": ns, "sigla": "agfi", "tier": "nonprd", **Q}
    r = client.post("/v1/consumers", json=body, headers=idem())
    assert r.status_code == 409 and "adoptarla" in r.json()["detail"]
    es = yaml.safe_load(client.get(f"/v1/consumers/{ns}/secret-apim?{QS}").text.split("\n", 1)[1])
    assert "app-id" not in es["spec"]["target"]["template"]["metadata"]["labels"], "sin label app-id vacío"
    assert es["spec"]["dataFrom"][0]["extract"]["key"] == path


def test_administrada_sigue_igual(client, parts):
    c = onboard(client)
    assert client.get(f"/v1/consumers/{NS}?{QS}").json()["state"] == "Active"
    es = yaml.safe_load(client.get(f"/v1/consumers/{NS}/secret-apim?{QS}").text.split("\n", 1)[1])
    assert es["spec"]["target"]["template"]["metadata"]["labels"]["app-id"] == c["appId"]


# ======================================================= EKS (0.4.0, 25/11)
def test_alta_en_eks_arma_el_path_del_cluster(client, parts):
    c = onboard(client, platform="eks", eksCluster=EKS)
    assert c["vaultPath"] == EKS_PATH and (c["platform"], c["eksCluster"]) == ("eks", EKS)
    assert parts["vault"].data[EKS_PATH][1][ID] == c["appId"]
    meta = parts["vault"].meta[EKS_PATH]
    assert (meta["platform"], meta["eks_cluster"], meta["environment"]) == ("eks", EKS, "QA")
    es = yaml.safe_load(c["secretApimManifest"].split("\n", 1)[1])
    assert es["spec"]["dataFrom"][0]["extract"]["key"] == EKS_PATH
    assert es["spec"]["target"]["template"]["metadata"]["annotations"]["apim.bgal/vault-path"] == EKS_PATH
    # la reserva del app_id guarda quién la hizo (la adopción de la copia de un namespace migrado la compara)
    assert parts["vault"].meta[f"{INDEX}/app-ids/{c['appId']}"] == {"namespace": NS, "tenant": TENANT}
    # GET, secret-apim y baja con la query; sin ella es la de openshift, que no existe
    assert client.get(f"/v1/consumers/{NS}?{QSE}").json()["appId"] == c["appId"]
    assert client.get(f"/v1/consumers/{NS}?{QS}").status_code == 404
    assert EKS_PATH in client.get(f"/v1/consumers/{NS}/secret-apim?{QSE}").text
    r = client.delete(f"/v1/consumers/{NS}?{QSE}", headers=idem())
    assert r.status_code == 200 and r.json()["state"] == "Revoked"


def test_openshift_y_eks_del_mismo_namespace_conviven(client):
    """Un namespace migrado de OpenShift a EKS tiene una credencial en cada path (24/11)."""
    a = onboard(client)
    b = onboard(client, platform="eks", eksCluster=EKS)
    assert (a["vaultPath"], b["vaultPath"]) == (PATH, EKS_PATH) and a["appId"] != b["appId"]
    assert a["platform"] == "openshift" and "eksCluster" not in a
    assert client.get(f"/v1/consumers/{NS}?{QS}").json()["appId"] == a["appId"]


def test_platform_y_eks_cluster_invalidos(client):
    body = {"namespace": NS, "sigla": "sigla", "tier": "nonprd", **Q}
    for extra, status, match in (
        ({"platform": "eks"}, 422, "falta eksCluster"),
        ({"eksCluster": EKS}, 422, "solo va con platform eks"),
        ({"platform": "openshift", "eksCluster": EKS}, 422, "solo va con platform eks"),
        ({"platform": "gke"}, 400, "platform"),
        ({"platform": "eks", "eksCluster": "Arqc_Delta"}, 400, "eksCluster"),
    ):
        r = client.post("/v1/consumers", json={**body, **extra}, headers=idem())
        assert r.status_code == status and match in r.json()["detail"], r.text
    assert client.get(f"/v1/consumers/{NS}?{QS}&platform=eks").status_code == 422
    assert client.get(f"/v1/consumers/{NS}?{QS}&platform=gke").status_code == 400


def test_reintento_con_platform_omitido_o_explicito(client):
    a = onboard(client, key="RITM0000200")
    body = {"namespace": NS, "sigla": "sigla", "tier": "nonprd", **Q, "platform": "openshift"}
    r = client.post("/v1/consumers", json=body, headers={"Idempotency-Key": "RITM0000200"})
    assert r.status_code == 200 and r.json()["appId"] == a["appId"]


def test_stg_es_int(client):
    c = onboard(client, ns="sigla-app-int", environment="STG")
    assert c["vaultPath"] == f"openshift-int/sigla-app-int/secret-apim-{TENANT}-v2"
