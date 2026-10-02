"""La API Consumers nunca devuelve, loguea ni lee una App Key + contrato con el OpenAPI."""

from pathlib import Path

import yaml
from conftest import NS, PATH, idem, onboard
from openapi_spec_validator import validate

from api_consumers.logging_setup import redact
from api_consumers.vault import HvacVault, MemoryVault

ROOT = Path(__file__).resolve().parents[1]


def test_el_cliente_de_vault_no_puede_leer_datos():
    """D3: escribir sin leer. No existe método de lectura de datos (solo metadata)."""
    for cls in (HvacVault, MemoryVault):
        assert not [m for m in dir(cls) if m.startswith("read")], cls
    src = (ROOT / "src/api_consumers/vault.py").read_text()
    assert "read_secret_version" not in src and "read_secret(" not in src


def test_la_key_no_aparece_en_respuestas_metadata_ni_logs(client, parts, capfd):
    texts = [str(onboard(client))]
    parts["subscriber"].subs[NS] = 1
    rot = client.post(f"/v1/consumers/{NS}/rotations", headers=idem()).json()
    texts.append(str(rot))
    texts.append(
        client.patch(f"/v1/consumers/{NS}/rotations/{rot['id']}", json={"action": "complete"}, headers=idem()).text
    )
    texts.append(client.get(f"/v1/consumers/{NS}").text)
    texts.append(client.get(f"/v1/consumers/{NS}/secret-apim").text)
    keys = {v["app_key"] for v in parts["vault"].data[PATH].values()}
    assert len(keys) == 2  # original + rotada
    out, err = capfd.readouterr()
    haystack = "\n".join(texts) + str(parts["vault"].meta) + str(parts["subscriber"].calls) + out + err
    for k in keys:
        assert k not in haystack


def test_openapi_valido_y_rutas_alineadas(client):
    spec = yaml.safe_load((ROOT / "openapi/consumers.yaml").read_text())
    validate(spec)
    spec_routes = {
        (m.upper(), p)
        for p, item in spec["paths"].items()
        for m in item
        if m in {"get", "post", "put", "patch", "delete"}
    }
    app_routes = set()
    for r in client.app.routes:
        path = getattr(r, "path", "")
        if path.startswith("/v1/"):
            for m in r.methods - {"HEAD", "OPTIONS"}:
                app_routes.add((m, path.replace("{rotation_id}", "{rotationId}")))
    assert spec_routes == app_routes


def test_redaccion_de_logs():
    k = "0123456789abcdef0123456789abcdef"
    assert k not in redact(f"key={k}")
    assert "***" in redact('{"app_key": "algo"}')
    assert "app_id" in redact("app_id=abcd1234")


def test_auth(client):
    client.headers.pop("Authorization")
    assert client.get(f"/v1/consumers/{NS}").status_code == 401
