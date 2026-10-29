"""Applications de 3scale para las suscripciones (0.3.0, decisiones del 29/10).

El API Subscriber decide en qué clusters de 3scale se suscribe un consumidor y le pide a la API Consumers la
application: una por servicio (API) en la cuenta de la sigla, con el app_id y la app_key de la credencial, en el admin
de 3scale de ese cluster (cada cluster tiene el suyo por tenant, también el par pg/mz). La lógica es la del script del
equipo de 3scale (`test_3scale_subscribe_custom_keys.py`): servicio por id o system_name, cuenta por sigla (username),
plan, application con application_id y application_key propios.

**Excepción a D3 (29/10):** es la única lectura de la key (`Vault.read_field`), con el mismo AppRole, y vive solo en
este módulo. La key nunca se devuelve ni se registra (el access_token va en la query y la key en el path de keys: el
logger de httpx queda en WARNING, ver logging_setup). Los admins permitidos llegan por configuración
(APICON_THREESCALE_ADMINS): un cluster sin admin configurado da 422, así que la key nunca va a una URL del pedido.

En una rotación, la key nueva se agrega a las applications del app_id (3scale admite varias keys: es el solapamiento) y
al completar se borran las demás.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from dataclasses import dataclass
from typing import Protocol

import httpx

from .config import Settings
from .errors import Problem, conflict, not_implemented, unprocessable

log = logging.getLogger(__name__)

_DESC_PLAN = re.compile(r"\bplan=(\S+)")


def _unavailable(detail: str) -> Problem:
    return Problem(503, "3scale Unavailable", detail)


class Admin(Protocol):
    """Admin API de 3scale (Account Management) de un cluster y tenant. Los ids son los internos de 3scale."""

    def service(self, service_id: str | None, system_name: str | None) -> dict | None: ...
    def account(self, username: str) -> dict | None: ...
    def create_account(self, username: str, email: str) -> dict: ...
    def plans(self, service_id: str) -> list[dict]: ...
    def applications(self, account_id: str) -> list[dict]: ...
    def create_application(
        self, account_id: str, plan_id: str, name: str, description: str, app_id: str, app_key: str
    ) -> dict: ...
    def update_application(self, account_id: str, app: str, plan_id: str, description: str) -> dict: ...
    def delete_application(self, account_id: str, app: str) -> None: ...
    def keys(self, account_id: str, app: str) -> list[str]: ...
    def add_key(self, account_id: str, app: str, key: str) -> None: ...
    def delete_key(self, account_id: str, app: str, key: str) -> None: ...


class HttpAdmin:
    """Llamados del script de 3scale, con httpx. El access_token va como parámetro (como en el script)."""

    def __init__(self, url: str, token: str, verify: bool = True, timeout_s: float = 30, transport=None):
        self._http = httpx.Client(base_url=url.rstrip("/"), verify=verify, timeout=timeout_s, transport=transport)
        self._token = token

    def _req(self, method: str, path: str, params: dict | None = None, data: dict | None = None) -> dict | None:
        """JSON de la respuesta; None si 404. 422 de 3scale → 409 (p. ej. application_id ya tomado)."""
        q = {"access_token": self._token, **(params or {})}
        try:
            if data is None:
                r = self._http.request(method, path, params=q)
            else:
                r = self._http.request(method, path, data={**q, **data})
        except httpx.HTTPError as e:
            raise _unavailable(f"3scale no responde ({type(e).__name__})") from e
        if r.status_code == 404:
            return None
        if r.status_code == 422:  # nunca se incluye el cuerpo del pedido (lleva la key)
            raise conflict(f"3scale rechazó {method} {path.split('?')[0]}: {r.text[:200]}")
        if r.status_code >= 300:
            raise _unavailable(f"3scale respondió HTTP {r.status_code} en {method} {path.split('/keys/')[0]}")
        return r.json() if r.content.strip() else {}

    def service(self, service_id: str | None, system_name: str | None) -> dict | None:
        if service_id:
            svc = ((self._req("GET", f"/admin/api/services/{service_id}.json") or {}).get("service")) or None
            if svc and (not system_name or svc.get("system_name") == system_name):
                return svc
        if not system_name:
            return None
        items = (self._req("GET", "/admin/api/services.json", {"per_page": 500}) or {}).get("services") or []
        found = [s["service"] for s in items if s["service"].get("system_name") == system_name]
        return found[0] if len(found) == 1 else None

    def account(self, username: str) -> dict | None:
        r = self._req("GET", "/admin/api/accounts/find.json", {"username": username})
        return (r or {}).get("account")

    def create_account(self, username: str, email: str) -> dict:
        data = {"org_name": username, "username": username, "email": email, "password": secrets.token_urlsafe(24)}
        return (self._req("POST", "/admin/api/signup.json", data=data) or {})["account"]

    def plans(self, service_id: str) -> list[dict]:
        r = self._req("GET", f"/admin/api/services/{service_id}/application_plans.json") or {}
        return [p["application_plan"] for p in r.get("plans") or []]

    def applications(self, account_id: str) -> list[dict]:
        r = self._req("GET", f"/admin/api/accounts/{account_id}/applications.json", {"per_page": 500}) or {}
        return [a["application"] for a in r.get("applications") or []]

    def create_application(
        self, account_id: str, plan_id: str, name: str, description: str, app_id: str, app_key: str
    ) -> dict:
        data = {
            "plan_id": plan_id,
            "name": name,
            "description": description,
            "application_id": app_id,
            "application_key": app_key,
        }
        return (self._req("POST", f"/admin/api/accounts/{account_id}/applications.json", data=data) or {})[
            "application"
        ]

    def update_application(self, account_id: str, app: str, plan_id: str, description: str) -> dict:
        base = f"/admin/api/accounts/{account_id}/applications/{app}"
        self._req("PUT", f"{base}/change_plan.json", data={"plan_id": plan_id})
        return (self._req("PUT", f"{base}.json", data={"description": description}) or {}).get("application") or {}

    def delete_application(self, account_id: str, app: str) -> None:
        self._req("DELETE", f"/admin/api/accounts/{account_id}/applications/{app}.json")

    def keys(self, account_id: str, app: str) -> list[str]:
        r = self._req("GET", f"/admin/api/accounts/{account_id}/applications/{app}/keys.json") or {}
        return [k["key"]["value"] for k in r.get("keys") or []]

    def add_key(self, account_id: str, app: str, key: str) -> None:
        self._req("POST", f"/admin/api/accounts/{account_id}/applications/{app}/keys.json", data={"key": key})

    def delete_key(self, account_id: str, app: str, key: str) -> None:
        self._req("DELETE", f"/admin/api/accounts/{account_id}/applications/{app}/keys/{key}.json")


class MemoryAdmin:
    """Tests: un admin de 3scale en memoria (servicios, cuentas, applications y keys)."""

    def __init__(self, services: dict[str, str] | None = None, plans: list[str] | None = None):
        self.services = {sid: {"id": sid, "system_name": name} for sid, name in (services or {}).items()}
        self.plan_names = plans or ["default"]
        self.accounts: dict[str, dict] = {}
        self.apps: dict[str, dict] = {}  # id interno → application (con "keys")

    def service(self, service_id: str | None, system_name: str | None) -> dict | None:
        if service_id in self.services:
            return self.services[service_id]
        return next((s for s in self.services.values() if s["system_name"] == system_name), None)

    def account(self, username: str) -> dict | None:
        return self.accounts.get(username)

    def create_account(self, username: str, email: str) -> dict:
        return self.accounts.setdefault(username, {"id": f"acc-{username}", "username": username, "email": email})

    def plans(self, service_id: str) -> list[dict]:
        return [{"id": f"{service_id}-{n}", "system_name": n, "default": i == 0} for i, n in enumerate(self.plan_names)]

    def applications(self, account_id: str) -> list[dict]:
        return [a for a in self.apps.values() if a["account_id"] == account_id]

    def create_application(
        self, account_id: str, plan_id: str, name: str, description: str, app_id: str, app_key: str
    ) -> dict:
        if any(a["application_id"] == app_id and a["service_id"] == plan_id.split("-")[0] for a in self.apps.values()):
            raise conflict("3scale rechazó la application: application_id has already been taken")
        app = {
            "id": str(len(self.apps) + 1),
            "account_id": account_id,
            "service_id": plan_id.split("-")[0],
            "plan_id": plan_id,
            "name": name,
            "description": description,
            "application_id": app_id,
            "keys": [app_key],
            "state": "live",
        }
        self.apps[app["id"]] = app
        return app

    def update_application(self, account_id: str, app: str, plan_id: str, description: str) -> dict:
        self.apps[app].update(plan_id=plan_id, description=description)
        return self.apps[app]

    def delete_application(self, account_id: str, app: str) -> None:
        self.apps.pop(app, None)

    def keys(self, account_id: str, app: str) -> list[str]:
        return list(self.apps[app]["keys"])

    def add_key(self, account_id: str, app: str, key: str) -> None:
        if key not in self.apps[app]["keys"]:
            self.apps[app]["keys"].append(key)

    def delete_key(self, account_id: str, app: str, key: str) -> None:
        self.apps[app]["keys"].remove(key)


@dataclass(frozen=True)
class _Rule:
    match: re.Pattern[str]
    admin_url: str


def _sigla(custom: dict) -> str:
    """Cuenta de 3scale (username) del consumidor: la de su primera application (`threescale_sigla`, la del ticket) o la
    sigla de la credencial."""
    return (custom.get("threescale_sigla") or custom.get("sigla") or "").upper()


def _description(subscription_id: str | None, plan_id: str) -> str:
    """El plan del API Subscriber va en la descripción: el de 3scale puede ser otro (el default del servicio)."""
    return f"API Subscriber: {subscription_id or '-'} plan={plan_id}"


def _view(app: dict, service: dict, plans: dict[str, str]) -> dict:
    m = _DESC_PLAN.search(app.get("description") or "")
    return {
        "applicationId": str(app["id"]),
        "appId": app.get("application_id"),
        "serviceId": str(service["id"]),
        "systemName": service.get("system_name"),
        "planId": m[1] if m else None,
        "threescalePlan": plans.get(str(app.get("plan_id"))),
        "state": app.get("state"),
    }


class Applications:
    """Las applications de un consumidor (namespace × tenant) en los admins de 3scale."""

    def __init__(self, s: Settings, vault, admin_factory=None):
        self.s = s
        self.vault = vault
        raw = json.loads(s.threescale_admins) if s.threescale_admins.strip() else []
        self.rules = [_Rule(re.compile(r["match"]), r["adminUrl"]) for r in raw if r.get("adminUrl")]
        self.tokens: dict[str, str] = json.loads(s.threescale_tokens) if s.threescale_tokens.strip() else {}
        self._factory = admin_factory or self._http
        self._admins: dict[tuple[str, str], Admin] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.rules)

    def _http(self, url: str, token: str) -> Admin:
        return HttpAdmin(url, token, verify=self.s.threescale_verify, timeout_s=self.s.threescale_timeout_s)

    def admin(self, cluster: str, tenant: str) -> Admin:
        if not self.enabled:
            raise not_implemented("applications de 3scale no habilitadas (APICON_THREESCALE_ADMINS)")
        if (cluster, tenant) not in self._admins:
            rule = next((r for r in self.rules if r.match.fullmatch(cluster)), None)
            if rule is None:
                raise unprocessable(f"cluster {cluster} sin admin de 3scale configurado (APICON_THREESCALE_ADMINS)")
            token = self.tokens.get(f"{cluster}/{tenant}") or self.tokens.get(cluster)
            if not token:
                raise unprocessable(f"sin token del admin de 3scale de {cluster} ({tenant}) (APICON_THREESCALE_TOKENS)")
            self._admins[(cluster, tenant)] = self._factory(rule.admin_url.format(t=tenant, cluster=cluster), token)
        return self._admins[(cluster, tenant)]

    def _find(self, admin: Admin, sigla: str, service: dict, app_id: str) -> tuple[dict | None, dict | None]:
        acc = admin.account(sigla)
        if not acc:
            return None, None
        apps = admin.applications(str(acc["id"]))
        same = [a for a in apps if str(a.get("service_id")) == str(service["id"]) and a.get("application_id") == app_id]
        return acc, (same[0] if same else None)

    def _service(self, admin: Admin, cluster: str, service_id: str | None, system_name: str | None) -> dict:
        svc = admin.service(service_id, system_name)
        if not svc:
            raise unprocessable(f"servicio {service_id or system_name} inexistente en el 3scale de {cluster}")
        return svc

    def get(self, custom: dict, cluster: str, tenant: str, service_id: str | None, system_name: str | None) -> dict:
        admin = self.admin(cluster, tenant)
        svc = self._service(admin, cluster, service_id, system_name)
        acc, app = self._find(admin, _sigla(custom), svc, custom.get("app_id") or "")
        if not app:
            return {}
        plans = {str(p["id"]): p.get("system_name") or p.get("name") for p in admin.plans(str(svc["id"]))}
        return _view(app, svc, plans)

    def put(
        self,
        cred_path: str,
        key_field: str,
        custom: dict,
        cluster: str,
        tenant: str,
        service_id: str | None,
        system_name: str | None,
        sigla: str,
        plan_id: str,
        subscription_id: str | None,
    ) -> dict:
        """Crea la application (con la key de Vault) o, si ya está con este app_id, le pone el plan. Idempotente."""
        admin = self.admin(cluster, tenant)
        svc = self._service(admin, cluster, service_id, system_name)
        app_id = custom.get("app_id") or ""
        acc, app = self._find(admin, sigla, svc, app_id)
        plans = admin.plans(str(svc["id"]))
        if not plans:
            raise unprocessable(f"el servicio {svc.get('system_name')} no tiene application plans en {cluster}")
        plan = (
            next((p for p in plans if (p.get("system_name") or p.get("name")) == plan_id), None)
            or next((p for p in plans if p.get("default")), None)
            or plans[0]
        )
        desc = _description(subscription_id, plan_id)
        if app:
            app = admin.update_application(str(acc["id"]), str(app["id"]), str(plan["id"]), desc)
        else:
            if not acc:
                if not self.s.threescale_account_email:
                    raise unprocessable(
                        f"la sigla {sigla} no tiene cuenta en el 3scale de {cluster} y no se crean cuentas "
                        "(APICON_THREESCALE_ACCOUNT_EMAIL)"
                    )
                acc = admin.create_account(sigla, self.s.threescale_account_email.format(sigla=sigla.lower()))
            app_key = self.vault.read_field(cred_path, key_field)  # excepción a D3 (29/10): solo acá
            if not app_key:
                raise unprocessable(f"{cred_path}: sin {key_field} en Vault")
            try:
                app = admin.create_application(str(acc["id"]), str(plan["id"]), f"{sigla}App", desc, app_id, app_key)
            finally:
                del app_key
        names = {str(p["id"]): p.get("system_name") or p.get("name") for p in plans}
        return _view(app, svc, names)

    def delete(self, custom: dict, cluster: str, tenant: str, service_id: str | None, system_name: str | None) -> bool:
        admin = self.admin(cluster, tenant)
        svc = self._service(admin, cluster, service_id, system_name)
        acc, app = self._find(admin, _sigla(custom), svc, custom.get("app_id") or "")
        if not app:
            return False
        admin.delete_application(str(acc["id"]), str(app["id"]))
        return True

    def _all(self, custom: dict, clusters: list[str], tenant: str):
        """Todas las applications del app_id en la cuenta de la sigla, en esos clusters: (admin, account_id, app)."""
        for cluster in sorted(set(clusters)):
            admin = self.admin(cluster, tenant)
            acc = admin.account(_sigla(custom))
            if not acc:
                continue
            for app in admin.applications(str(acc["id"])):
                if app.get("application_id") == custom.get("app_id"):
                    yield admin, str(acc["id"]), str(app["id"])

    def add_key(self, custom: dict, clusters: list[str], tenant: str, app_key: str) -> int:
        """Rotación (KeyWritten): la key nueva en todas las applications del app_id. Devuelve cuántas."""
        n = 0
        for admin, acc, app in self._all(custom, clusters, tenant):
            admin.add_key(acc, app, app_key)
            n += 1
        return n

    def prune_keys(self, cred_path: str, key_field: str, custom: dict, clusters: list[str], tenant: str) -> int:
        """Rotación (Completed): saca de las applications toda key que no sea la vigente en Vault. Devuelve cuántas."""
        current = self.vault.read_field(cred_path, key_field)  # excepción a D3 (29/10): solo acá
        if not current:
            return 0
        n = 0
        try:
            for admin, acc, app in self._all(custom, clusters, tenant):
                for k in admin.keys(acc, app):
                    if k != current:
                        admin.delete_key(acc, app, k)
                        n += 1
        finally:
            del current
        return n
