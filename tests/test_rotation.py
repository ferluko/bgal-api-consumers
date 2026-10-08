"""Rotación coordinada con el API Subscriber (ADR API Consumers D4): sin leer la key vieja."""

from conftest import ID, KEY, NS, PATH, QS, TENANT, cred, idem, make_service, onboard

from api_consumers import service as service_mod


def _start(client, ns=NS, key=None, **body):
    r = client.post(
        f"/v1/consumers/{ns}/rotations?{QS}", json=body or None, headers={"Idempotency-Key": key} if key else idem()
    )
    return r


def test_rotacion_completa(client, parts):
    c = onboard(client)
    parts["subscriber"].subs[NS] = 1
    v1_key = parts["vault"].data[PATH][1][KEY]
    r = _start(client)
    rot = r.json()
    assert r.status_code == 202 and r.headers["location"] == f"/v1/consumers/{NS}/rotations/{rot['id']}?{QS}"
    assert rot["state"] == "AwaitingConsumer" and rot["pinnedVersion"] == 1 and rot["newVersion"] == 2
    # orden seguro: primero la ventana fijada a v1, después la key nueva, después el refresh
    calls = parts["subscriber"].calls
    assert calls[0] == ("open", NS, TENANT, rot["id"], 1) and calls[1] == ("refresh", NS, TENANT)
    v2 = parts["vault"].data[PATH][2]
    assert v2[ID] == c["appId"] and v2[KEY] != v1_key
    assert parts["vault"].data[PATH][1][KEY] == v1_key  # la versión fijada sigue (la leen los -prev)
    assert client.get(f"/v1/consumers/{NS}?{QS}").json()["state"] == "Rotating"

    r = client.patch(f"/v1/consumers/{NS}/rotations/{rot['id']}?{QS}", json={"action": "complete"}, headers=idem())
    assert r.status_code == 202 and r.json()["state"] == "Completed"
    assert parts["subscriber"].calls[-1] == ("close", NS, TENANT) and (NS, TENANT) not in parts["subscriber"].overlaps
    consumer = client.get(f"/v1/consumers/{NS}?{QS}").json()
    assert consumer["state"] == "Active" and consumer["rotatedAt"] and consumer["vaultVersion"] == 2


def test_si_la_ventana_no_se_confirma_no_se_escribe_la_key_nueva(client, parts):
    onboard(client)
    parts["subscriber"].fail_open = True
    rot = _start(client).json()
    assert rot["state"] == "Failed" and "no se escribió la key nueva" in rot["error"]["detail"]
    assert parts["vault"].current[PATH] == 1  # nadie queda cortado
    assert client.get(f"/v1/consumers/{NS}?{QS}").json()["state"] == "Active"


def test_idempotente_y_409_con_rotacion_en_curso(client, parts):
    onboard(client)
    a = _start(client, key="ROT-1").json()
    assert _start(client, key="ROT-1").json()["id"] == a["id"]
    r = _start(client, key="ROT-2")
    assert r.status_code == 409
    assert client.delete(f"/v1/consumers/{NS}?{QS}", headers=idem()).status_code == 409


def test_abort_antes_de_la_key_nueva(client, parts, monkeypatch):
    onboard(client)
    monkeypatch.setattr(parts["svc"], "_drive", lambda ns: None)  # queda en Requested
    rot = _start(client).json()
    assert rot["state"] == "Requested"
    monkeypatch.undo()
    r = client.patch(f"/v1/consumers/{NS}/rotations/{rot['id']}?{QS}", json={"action": "abort"}, headers=idem())
    assert r.status_code == 202 and r.json()["state"] == "Aborted"
    assert parts["vault"].current[PATH] == 1


def test_sin_abort_despues_de_escribir_la_key(client):
    onboard(client)
    rot = _start(client).json()
    r = client.patch(f"/v1/consumers/{NS}/rotations/{rot['id']}?{QS}", json={"action": "abort"}, headers=idem())
    assert r.status_code == 409 and "roll-forward" in r.json()["detail"]
    assert (
        client.patch(f"/v1/consumers/{NS}/rotations/x?{QS}", json={"action": "complete"}, headers=idem()).status_code
        == 404
    )


def test_version_fijada_purgada_falla_sin_escribir(client, parts, monkeypatch):
    onboard(client)
    monkeypatch.setattr(parts["svc"], "_drive", lambda ns: None)
    _start(client)
    monkeypatch.undo()
    del parts["vault"].data[PATH][1]  # max_versions la purgó
    parts["svc"]._drive(cred(parts))
    rot = client.get(f"/v1/consumers/{NS}?{QS}").json()
    assert rot["state"] == "Active"
    meta = parts["vault"].meta[PATH]
    assert meta["rotation_state"] == "Failed" and "ya no existe" in meta["rotation_error"]
    assert parts["vault"].current[PATH] == 1


def test_otro_escritor_entre_medio_cas_falla(client, parts, monkeypatch):
    onboard(client)
    monkeypatch.setattr(parts["svc"], "_drive", lambda ns: None)
    _start(client)
    monkeypatch.undo()
    parts["vault"].write(PATH, {ID: "x", KEY: "y"}, cas=1)  # alguien escribió v2
    parts["svc"]._drive(cred(parts))
    assert parts["vault"].meta[PATH]["rotation_state"] == "Failed"
    assert parts["vault"].current[PATH] == 2  # no se pisó


def test_callback_en_awaiting_y_completed(client, monkeypatch):
    sent = []
    monkeypatch.setattr(service_mod, "deliver_callback", lambda url, body, *a: sent.append((url, body["state"])))
    onboard(client)
    rot = _start(client, callbackUrl="https://null.example/cb").json()
    client.patch(f"/v1/consumers/{NS}/rotations/{rot['id']}?{QS}", json={"action": "complete"}, headers=idem())
    assert sent == [("https://null.example/cb", "AwaitingConsumer"), ("https://null.example/cb", "Completed")]


def test_recover_retoma_rotaciones_pendientes(client, parts, settings, monkeypatch):
    onboard(client)
    monkeypatch.setattr(parts["svc"], "_drive", lambda ns: None)
    _start(client)
    monkeypatch.undo()
    other = make_service(settings, vault=parts["vault"], sub=parts["subscriber"])["svc"]  # proceso nuevo
    other.recover()
    assert parts["vault"].meta[PATH]["rotation_state"] == "AwaitingConsumer"
