"""POST|PATCH /v1/consumers/{ns}/adoption (0.4.0, 25/11): el onboarding de una credencial preexistente por la API, lo
que hacía adopt-credential.sh a mano desde el bastión. Al desafectar 3scale, el pipeline de DevSecOps crea la
credencial en Vault y llama acá; la API Consumers escribe solo la metadata y reserva el app_id (no lee datos, D3)."""

from conftest import EKS, ID, INDEX, KEY, NS, QS, TENANT, idem, onboard

from api_consumers.adoption import reserve

PRE = "sigla-preexistente-qas"
OCP = f"openshift-qas/{PRE}/secret-apim-{TENANT}-v2"
EKS_PRE = f"eks-{EKS}/{PRE}/data/secret-apim-{TENANT}-v2"
QE = f"{QS}&platform=eks&eksCluster={EKS}"
REAL, WRONG = "0a1b2c3d", "0d3c2b1a"
SECRET = "clave-secreta"


def _preexistente(parts, path=OCP, app_id=REAL):
    """Lo que deja DevSecOps: datos en Vault (versión 3) sin la metadata de la API Consumers."""
    for v in range(3):
        parts["vault"].write(path, {ID: app_id, KEY: f"{SECRET}-v{v}"}, cas=v)


def _adopt(client, app_id=REAL, ns=PRE, q=QS, key="ADOPT-1", dry=False, **body):
    url = f"/v1/consumers/{ns}/adoption?{q}" + ("&dryRun=true" if dry else "")
    return client.post(url, json={"appId": app_id, **body}, headers={"Idempotency-Key": key})


def _fix(client, previous, new, ns=PRE, q=QS, dry=False, **body):
    url = f"/v1/consumers/{ns}/adoption?{q}" + ("&dryRun=true" if dry else "")
    return client.patch(url, json={"appId": new, "previousAppId": previous, **body}, headers=idem())


def test_adopta_por_la_api_sin_tocar_los_datos(client, parts):
    _preexistente(parts)
    before = dict(parts["vault"].data[OCP])
    assert client.get(f"/v1/consumers/{PRE}?{QS}").json()["state"] == "Unmanaged"
    r = _adopt(client, owner="DevOps", servicenowRef="UR0000001")
    assert r.status_code == 201, r.text
    c = r.json()
    assert (c["state"], c["appId"], c["origin"], c["vaultVersion"], c["platform"]) == (
        "Active",
        REAL,
        "adopted",
        3,
        "openshift",
    )
    assert c["notes"] == [] and SECRET not in r.text
    assert parts["vault"].data[OCP] == before and parts["vault"].current[OCP] == 3, "sin versión nueva"
    meta = parts["vault"].meta[OCP]
    assert (meta["adopted_by"], meta["adopted_index"], meta["owner"], meta["sigla"]) == (
        "api-consumers",
        "reserved",
        "DevOps",
        "SIGLA",
    )
    assert parts["vault"].meta[f"{INDEX}/app-ids/{REAL}"] == {"namespace": PRE, "tenant": TENANT}
    # ya administrada: se puede rotar y dar de baja
    assert client.get(f"/v1/consumers/{PRE}?{QS}").json()["state"] == "Active"


def test_dry_run_no_escribe(client, parts):
    _preexistente(parts)
    r = _adopt(client, dry=True)
    assert r.status_code == 200, r.text
    p = r.json()
    assert (p["dryRun"], p["mode"], p["vaultPath"], p["vaultVersion"]) == (True, "adopt", OCP, 3)
    assert p["index"] == "reserved"
    assert p["changes"]["app_id"] == REAL and p["reserve"] == f"{INDEX}/app-ids/{REAL}"
    assert any("sin OWNER" in n for n in p["notes"])
    assert parts["vault"].meta[OCP] == {} and f"{INDEX}/app-ids/{REAL}" not in parts["vault"].current


def test_reintento_idempotente(client, parts):
    _preexistente(parts)
    a = _adopt(client, owner="DevOps")
    assert a.status_code == 201
    r = _adopt(client, owner="DevOps")
    assert r.status_code == 200 and r.json()["appId"] == REAL
    r = _adopt(client, owner="otro")
    assert r.status_code == 422 and "Idempotency-Key" in r.json()["detail"]
    r = _adopt(client, key="ADOPT-2")
    assert r.status_code == 409 and "ya está administrada" in r.json()["detail"]


def test_rechazos(client, parts):
    r = _adopt(client)
    assert r.status_code == 404 and "no existe" in r.json()["detail"]
    _preexistente(parts)
    r = _adopt(client, app_id="con espacio")
    assert r.status_code == 422 and "label" in r.json()["detail"]
    onboard(client)  # generada por la API Consumers: ya administrada
    r = _adopt(client, ns=NS)
    assert r.status_code == 409 and "ya está administrada" in r.json()["detail"]


def test_adopta_en_eks(client, parts):
    _preexistente(parts, EKS_PRE)
    assert _adopt(client).status_code == 404  # sin la query la busca en openshift
    r = _adopt(client, q=QE)
    assert r.status_code == 201, r.text
    c = r.json()
    assert (c["platform"], c["eksCluster"], c["vaultPath"]) == ("eks", EKS, EKS_PRE)
    assert parts["vault"].meta[EKS_PRE]["eks_cluster"] == EKS


def test_copia_de_un_namespace_migrado_comparte_la_reserva(client, parts):
    """24/11: un namespace migrado de OpenShift a EKS tiene el mismo secreto en los dos paths."""
    _preexistente(parts, OCP)
    _preexistente(parts, EKS_PRE)
    assert _adopt(client).status_code == 201
    r = _adopt(client, q=QE, key="ADOPT-EKS")
    assert r.status_code == 201, r.text
    assert any("se comparte" in n for n in r.json()["notes"])
    assert parts["vault"].meta[EKS_PRE]["adopted_index"] == "shared"
    for q in (QS, QE):
        assert client.get(f"/v1/consumers/{PRE}?{q}").json()["state"] == "Active"


def test_app_id_reservado_por_otro_o_reserva_vieja(client, parts):
    _preexistente(parts)
    reserve(parts["vault"], f"{INDEX}/app-ids/{REAL}", "otro-ns-qas", "b2c")
    r = _adopt(client)
    assert r.status_code == 409 and "de otro-ns-qas (b2c)" in r.json()["detail"]
    assert parts["vault"].meta[OCP] == {}
    # una reserva anterior a 0.4.0 no tiene dueño en la metadata: se rechaza y queda skipIndex
    _preexistente(parts, f"openshift-qas/sigla-vieja-qas/secret-apim-{TENANT}-v2", WRONG)
    parts["vault"].write(f"{INDEX}/app-ids/{WRONG}", {"namespace": "sigla-vieja-qas"}, cas=0)
    r = _adopt(client, app_id=WRONG, ns="sigla-vieja-qas")
    assert r.status_code == 409 and "anterior a 0.4.0" in r.json()["detail"]
    r = _adopt(client, app_id=WRONG, ns="sigla-vieja-qas", key="ADOPT-SKIP", skipIndex=True)
    assert r.status_code == 201
    assert parts["vault"].meta[f"openshift-qas/sigla-vieja-qas/secret-apim-{TENANT}-v2"]["adopted_index"] == "skipped"


def test_fix_app_id_por_la_api(client, parts):
    _preexistente(parts)
    assert _adopt(client, app_id=WRONG).status_code == 201
    p = _fix(client, WRONG, REAL, dry=True, releasePrevious=True).json()
    assert (p["mode"], p["changes"]["app_id"], p["release"]) == ("fix-app-id", REAL, f"{INDEX}/app-ids/{WRONG}")
    assert parts["vault"].meta[OCP]["app_id"] == WRONG, "el dryRun no escribe"
    r = _fix(client, WRONG, REAL, releasePrevious=True)
    assert r.status_code == 200, r.text
    assert r.json()["appId"] == REAL and parts["vault"].meta[OCP]["app_id_corrected_from"] == WRONG
    assert f"{INDEX}/app-ids/{REAL}" in parts["vault"].current
    assert f"{INDEX}/app-ids/{WRONG}" not in parts["vault"].current
    r = _fix(client, WRONG, REAL)  # reintento de la corrección hecha
    assert r.status_code == 200 and r.json()["appId"] == REAL


def test_fix_app_id_rechazos(client, parts):
    _preexistente(parts)
    _adopt(client, app_id=WRONG)
    r = _fix(client, REAL, REAL)
    assert r.status_code == 422 and "iguales" in r.json()["detail"]
    r = _fix(client, "aaaaaaaa", REAL)
    assert r.status_code == 409 and f"el app_id actual es {WRONG}" in r.json()["detail"]
    parts["subscriber"].subs[PRE] = 1  # suscripción viva: su label app-id es el viejo
    r = _fix(client, WRONG, REAL)
    assert r.status_code == 409 and "suscripciones vivas" in r.json()["detail"]
    onboard(client)
    r = _fix(client, "aaaaaaaa", REAL, ns=NS)
    assert r.status_code == 409 and "no es una adopción" in r.json()["detail"]


def test_sin_permiso_en_la_metadata_del_indice_la_reserva_vale_igual(client, parts, monkeypatch):
    """Con una policy sin update en la metadata de app-ids (la de antes de 0.4.0), el alta y la adopción siguen: la
    reserva queda sin dueño, como antes."""
    real_set = parts["vault"].set_metadata

    def set_metadata(path, metadata, drop=None):
        if "/app-ids/" in path:
            raise PermissionError(path)
        return real_set(path, metadata, drop)

    monkeypatch.setattr(parts["vault"], "set_metadata", set_metadata)
    c = onboard(client)
    assert f"{INDEX}/app-ids/{c['appId']}" in parts["vault"].current
    assert parts["vault"].meta[f"{INDEX}/app-ids/{c['appId']}"] == {}
    _preexistente(parts)
    assert _adopt(client).status_code == 201
