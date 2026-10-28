"""deploy/scripts/adopt_credential.py: adopción manual de una credencial Unmanaged y corrección del app_id (28/10, en
paas-dev el API Subscriber daba 422 "adoptarla antes de suscribir" con un namespace de DevOps)."""

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml
from conftest import ENV, ID, INDEX, KEY, NS, QS, TENANT, onboard

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/scripts/adopt_credential.py"
_spec = importlib.util.spec_from_file_location("adopt_credential", SCRIPT)
ad = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ad  # dataclasses busca el módulo por nombre
_spec.loader.exec_module(ad)

PRE = "sigla-preexistente-qas"
PATH = f"openshift-qas/{PRE}/secret-apim-{TENANT}-v2"
REAL, WRONG = "0a1b2c3d", "0d3c2b1a"


def _preexistente(parts):
    """Lo que deja DevSecOps: datos en Vault (versión 3) sin la metadata de la API Consumers."""
    for v in range(3):
        parts["vault"].write(PATH, {ID: REAL, KEY: f"k{v}"}, cas=v)


def run(parts, settings, mode, *ids, ns=PRE, phase="apply", **opts):
    p = ad.build_plan(mode, ns, ENV, TENANT, list(ids), opts, settings, parts["vault"], parts["subscriber"])
    return p, (ad.apply(p, parts["vault"]) if phase == "apply" else None)


def test_adopt_la_deja_active_sin_tocar_los_datos(client, parts, settings):
    _preexistente(parts)
    before = dict(parts["vault"].data[PATH])
    assert client.get(f"/v1/consumers/{PRE}?{QS}").json()["state"] == "Unmanaged"
    run(parts, settings, "adopt", REAL, OWNER="DevOps", REF="UR0000001")
    c = client.get(f"/v1/consumers/{PRE}?{QS}").json()
    assert (c["state"], c["appId"], c["origin"], c["vaultVersion"]) == ("Active", REAL, "adopted", 3)
    assert parts["vault"].data[PATH] == before and parts["vault"].current[PATH] == 3, "sin versión nueva"
    assert f"{INDEX}/app-ids/{REAL}" in parts["vault"].current
    meta = parts["vault"].meta[PATH]
    assert (meta["sigla"], meta["owner"], meta["servicenow_ref"], meta["adopted_index"]) == (
        "SIGLA",
        "DevOps",
        "UR0000001",
        "reserved",
    )
    es = yaml.safe_load(client.get(f"/v1/consumers/{PRE}/secret-apim?{QS}").text.split("\n", 1)[1])
    assert es["spec"]["target"]["template"]["metadata"]["labels"]["app-id"] == REAL


def test_plan_no_escribe(parts, settings):
    _preexistente(parts)
    p, _ = run(parts, settings, "adopt", REAL, phase="plan")
    assert parts["vault"].meta[PATH] == {} and f"{INDEX}/app-ids/{REAL}" not in parts["vault"].current
    text = "\n".join(ad.describe(p))
    assert f"app_id: — → {REAL}" in text and "sin OWNER" in text


def test_adopt_se_niega(client, parts, settings):
    with pytest.raises(ad.Refused, match="no existe"):
        run(parts, settings, "adopt", REAL)
    _preexistente(parts)
    for ids, match in (([REAL, REAL], "un app_id"), (["con espacio"], "label"), (["-x"], "label")):
        with pytest.raises(ad.Refused, match=match):
            run(parts, settings, "adopt", *ids)
    with pytest.raises(ad.Refused, match="tenant"):
        ad.build_plan("adopt", PRE, ENV, "m2m", [REAL], {}, settings, parts["vault"], parts["subscriber"])
    with pytest.raises(ad.Refused, match="ambiente"):
        ad.build_plan("adopt", PRE, "UAT", TENANT, [REAL], {}, settings, parts["vault"], parts["subscriber"])
    onboard(client)  # generada por la API Consumers: ya administrada
    with pytest.raises(ad.Refused, match="ya está administrada"):
        run(parts, settings, "adopt", REAL, ns=NS)


def test_app_id_reservado_por_otro_se_niega_sin_tocar_la_metadata(parts, settings):
    _preexistente(parts)
    parts["vault"].write(f"{INDEX}/app-ids/{REAL}", {"namespace": "otro"}, cas=0)
    with pytest.raises(ad.Refused, match="ya está reservado"):
        run(parts, settings, "adopt", REAL)
    assert parts["vault"].meta[PATH] == {}
    run(parts, settings, "adopt", REAL, SKIP_INDEX="1")  # reintento de una corrida propia
    assert parts["vault"].meta[PATH]["adopted_index"] == "skipped"


def test_fix_app_id_corrige_y_libera_la_reserva_vieja(client, parts, settings):
    _preexistente(parts)
    run(parts, settings, "adopt", WRONG)
    with pytest.raises(ad.Refused, match="ya está administrada"):
        run(parts, settings, "adopt", REAL)
    _, out = run(parts, settings, "fix-app-id", WRONG, REAL, RELEASE_OLD="1")
    c = client.get(f"/v1/consumers/{PRE}?{QS}").json()
    assert (c["state"], c["appId"]) == ("Active", REAL)
    assert parts["vault"].meta[PATH]["app_id_corrected_from"] == WRONG
    assert f"{INDEX}/app-ids/{REAL}" in parts["vault"].current
    assert f"{INDEX}/app-ids/{WRONG}" not in parts["vault"].current
    assert any(line.startswith("OK    borrado") for line in out)


def test_fix_app_id_se_niega(client, parts, settings):
    _preexistente(parts)
    run(parts, settings, "adopt", WRONG)
    for ids, match in (([REAL, REAL], "iguales"), (["aaaaaaaa", REAL], "el app_id actual es 0d3c2b1a")):
        with pytest.raises(ad.Refused, match=match):
            run(parts, settings, "fix-app-id", *ids)
    parts["subscriber"].subs[PRE] = 1  # suscripción viva: su label app-id es el viejo
    with pytest.raises(ad.Refused, match="suscripciones vivas"):
        run(parts, settings, "fix-app-id", WRONG, REAL)
    onboard(client)
    with pytest.raises(ad.Refused, match="no es una adopción"):
        run(parts, settings, "fix-app-id", "aaaaaaaa", REAL, ns=NS)


def test_release_old_sin_constancia_pide_force(parts, settings):
    """La adopción a mano del 28/10 no dejó adopted_index: la reserva vieja solo se borra con force."""
    _preexistente(parts)
    parts["vault"].write(f"{INDEX}/app-ids/{WRONG}", {"namespace": PRE}, cas=0)
    parts["vault"].set_metadata(PATH, {"app_id": WRONG, "origin": "adopted", "created_by": "adopcion-manual"})
    with pytest.raises(ad.Refused, match="no consta"):
        run(parts, settings, "fix-app-id", WRONG, REAL, RELEASE_OLD="1")
    run(parts, settings, "fix-app-id", WRONG, REAL, RELEASE_OLD="force")
    assert f"{INDEX}/app-ids/{WRONG}" not in parts["vault"].current


def test_sin_permiso_para_borrar_la_reserva_avisa_y_la_correccion_queda(parts, settings, monkeypatch):
    _preexistente(parts)
    run(parts, settings, "adopt", WRONG)

    def forbidden(vault, path):
        raise PermissionError(path)

    monkeypatch.setattr(ad, "_release", forbidden)
    _, out = run(parts, settings, "fix-app-id", WRONG, REAL, RELEASE_OLD="1")
    assert parts["vault"].meta[PATH]["app_id"] == REAL
    assert any(line.startswith("WARN") and "DevSecOps" in line for line in out)


def test_no_lee_datos_y_uso():
    src = SCRIPT.read_text()
    assert "read_secret" not in src, "D3: el script no lee datos de Vault"
    assert ad.main([]) == 2
