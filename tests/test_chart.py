"""Chart Helm + jerarquía de values (se salta si no hay helm)."""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "deploy/charts/api-consumers"
V = ROOT / "deploy/values"
pytestmark = pytest.mark.skipif(not shutil.which("helm"), reason="helm no instalado")
LAB = [
    "groups/all",
    "groups/tier-lower",
    "groups/domain-lab",
    "groups/env-lab",
    "profiles/apim-toolkit",
    "clusters/paas-arqlab",
]


def render(layers, *sets):
    args = ["helm", "template", "api-consumers", str(CHART)]
    for layer in layers:
        if (V / f"{layer}.yaml").exists():
            args += ["-f", str(V / f"{layer}.yaml")]
    for s in sets:
        args += ["--set", s]
    p = subprocess.run(args, capture_output=True, text=True)
    return p, [d for d in yaml.safe_load_all(p.stdout) if d] if p.returncode == 0 else []


def by_kind(docs, kind, name=None):
    return [d for d in docs if d["kind"] == kind and (name is None or d["metadata"]["name"] == name)]


def test_lab_render():
    p, docs = render(LAB)
    assert p.returncode == 0, p.stderr
    assert not by_kind(docs, "Secret") and not by_kind(docs, "ExternalSecret")
    assert not by_kind(docs, "Role") and not by_kind(docs, "ClusterRoleBinding"), "no toca el cluster; approle en lab"
    env = by_kind(docs, "ConfigMap", "api-consumers-env")[0]["data"]
    assert env["APICON_VAULT_AUTH"] == "approle" and env["APICON_CONSUMER_STORE_NAME"] == "vault-apim-consumer-nonprd"
    c = by_kind(docs, "Deployment")[0]["spec"]["template"]["spec"]["containers"][0]
    assert not [e for e in c.get("env", []) if "valueFrom" in e], "ningún secreto como variable de entorno"
    vol = next(
        v for v in by_kind(docs, "Deployment")[0]["spec"]["template"]["spec"]["volumes"] if v["name"] == "secrets"
    )
    paths = {i["key"]: i["path"] for i in vol["secret"]["items"]}
    assert paths["vault_secret_id"] == "APICON_VAULT_SECRET_ID" and paths["subscriber_url"] == "APICON_SUBSCRIBER_URL"


def test_prd_render_usa_eso_y_auth_kubernetes():
    layers = ["groups/all", "groups/tier-high", "groups/domain-apim", "groups/env-prd", "profiles/apim-toolkit"]
    p, docs = render(layers, "cluster.name=paas-apim-prdpg")
    assert p.returncode == 0, p.stderr
    assert by_kind(docs, "ConfigMap", "api-consumers-env")[0]["data"]["APICON_TIER"] == "prd"
    assert (
        by_kind(docs, "ExternalSecret")[0]["spec"]["dataFrom"][0]["extract"]["key"] == "ocp/apim-toolkit/api-consumers"
    )
    assert by_kind(docs, "ClusterRoleBinding")  # TokenReview para auth de Kubernetes


@pytest.mark.parametrize(
    "sets,msg",
    [
        (["apiConsumers.vault.addr="], "vault.addr es obligatorio"),
        (["apiConsumers.vault.auth=ldap"], "vault.auth"),
        (["apiConsumers.tier=qa"], "nonprd o prd"),
    ],
)
def test_validaciones_del_chart(sets, msg):
    p, _ = render(LAB, *sets)
    assert p.returncode != 0 and msg in p.stderr
