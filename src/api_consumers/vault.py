"""Vault KV v2 con el acceso mínimo de ADR API Consumers D3: escribir sin leer keys.

- Datos (`data/`): solo **escritura** (create/update), siempre con check-and-set (`cas`). No hay método para leerlos:
  la garantía de que la API Consumers nunca lee una App Key es estructural, no de disciplina.
- Metadata (`metadata/`): lectura y escritura de `custom_metadata`, `current_version` y versiones. Ahí vive el estado
  del consumidor y de la rotación.
- Sin `delete` ni `destroy`: la baja escribe una versión tombstone y las anteriores quedan para auditoría.

Los paths son completos: el primer segmento es el mount KV v2 (`openshift-<ambiente>/<ns>/secret-apim-<tenant>-v2`,
`apim-<tier>/consumers/_index/...`).
"""

from __future__ import annotations

import copy
import hashlib
import secrets
from datetime import datetime, timezone
from typing import Protocol

from .config import Settings


def generate_app_id() -> str:
    return secrets.token_hex(4)  # 8 hex, formato 3scale


def generate_app_key() -> str:
    return secrets.token_hex(16)  # 128 bits, 32 hex


def fingerprint(app_key: str) -> str:
    return hashlib.sha256(app_key.encode()).hexdigest()


def custom_metadata(metadata: dict) -> dict[str, str]:
    """custom_metadata de KV v2: Vault rechaza valores vacíos (0 < len <= 512). Los vacíos se omiten."""
    out = {k: str(v) for k, v in metadata.items() if v is not None and str(v) != ""}
    for k, v in out.items():
        if len(v) > 512:
            raise ValueError(f"custom_metadata {k}: {len(v)} caracteres (máximo 512)")
    return out


class CasMismatch(Exception):
    """Check-and-set falló: el secreto existe (cas=0) o alguien escribió otra versión en el medio."""


class Vault(Protocol):
    def write(self, path: str, data: dict, cas: int) -> int:
        """Nueva versión con check-and-set. Devuelve el número de versión. CasMismatch si no coincide."""

    def metadata(self, path: str) -> dict | None:
        """{"custom": {...}, "current_version": int, "versions": {n: {"destroyed": bool, "deleted": bool}}} o None."""

    def set_metadata(self, path: str, metadata: dict, drop: set[str] | None = None) -> None:
        """Merge sobre custom_metadata (`drop`: claves a quitar)."""

    def list(self, prefix: str) -> list[str]:
        """Claves bajo `prefix` (metadata list)."""

    def healthy(self) -> bool: ...


class MemoryVault:
    """Tests y desarrollo local. Simula versiones, cas, max_versions y metadata de KV v2. Sin lectura de datos."""

    def __init__(self, max_versions: int = 10) -> None:
        self.max_versions = max_versions
        self.data: dict[str, dict[int, dict]] = {}  # introspección de tests: el servicio no tiene cómo leerlo
        self.current: dict[str, int] = {}
        self.meta: dict[str, dict[str, str]] = {}

    def write(self, path: str, data: dict, cas: int) -> int:
        cur = self.current.get(path, 0)
        if cas != cur:
            raise CasMismatch(f"{path}: cas={cas}, versión actual={cur}")
        n = cur + 1
        versions = self.data.setdefault(path, {})
        versions[n] = copy.deepcopy(data)
        for old in sorted(versions)[: max(0, len(versions) - self.max_versions)]:
            del versions[old]  # Vault purga las más viejas al superar max_versions
        self.current[path] = n
        self.meta.setdefault(path, {})
        return n

    def metadata(self, path: str) -> dict | None:
        if path not in self.current:
            return None
        return {
            "custom": dict(self.meta.get(path, {})),
            "current_version": self.current[path],
            "versions": {n: {"destroyed": False, "deleted": False} for n in self.data.get(path, {})},
        }

    def set_metadata(self, path: str, metadata: dict, drop: set[str] | None = None) -> None:
        m = self.meta.setdefault(path, {})
        for k in drop or ():
            m.pop(k, None)
        m.update(custom_metadata(metadata))

    def list(self, prefix: str) -> list[str]:
        prefix = prefix.rstrip("/") + "/"
        out = set()
        for p in self.current:
            if p.startswith(prefix):
                head, sep, _ = p[len(prefix) :].partition("/")
                out.add(head + ("/" if sep else ""))
        return sorted(out)

    def healthy(self) -> bool:
        return True


def split_mount(path: str, root_ok: bool = False) -> tuple[str, str]:
    """openshift-qas/ns/secret → ("openshift-qas", "ns/secret"). Con `root_ok`, "openshift-qas" → (mount, "")."""
    mount, _, rest = path.strip("/").partition("/")
    if not mount or not (rest or root_ok):
        raise ValueError(f"path de Vault sin mount: {path!r}")
    return mount, rest


class HvacVault:
    """KV v2 vía hvac. Re-autentica sola cuando el token vence (AppRole/Kubernetes tienen TTL)."""

    def __init__(self, s: Settings):
        import hvac

        self._s = s
        self._hvac = hvac
        # Con approle/kubernetes, token="" evita que hvac tome VAULT_TOKEN o ~/.vault-token del entorno y lo mande
        # en el login (Vault 2.x responde 500 "failed to look up namespace from the token").
        token = None if s.vault_auth == "token" else ""
        self.client = hvac.Client(
            url=s.vault_addr, token=token, namespace=s.vault_namespace or None, verify=s.vault_verify
        )
        self._login()

    def _login(self) -> None:
        s = self._s
        if s.vault_auth == "token":
            self.client.token = s.vault_token
        elif s.vault_auth == "approle":
            self.client.auth.approle.login(role_id=s.vault_role_id, secret_id=s.vault_secret_id)
        else:
            with open("/var/run/secrets/kubernetes.io/serviceaccount/token") as f:
                jwt = f.read()
            self.client.auth.kubernetes.login(role=s.vault_k8s_role, jwt=jwt, mount_point=s.vault_k8s_mount)

    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (self._hvac.exceptions.Forbidden, self._hvac.exceptions.Unauthorized):
            if self._s.vault_auth == "token":
                raise
            self._login()
            return fn(*args, **kwargs)

    def write(self, path: str, data: dict, cas: int) -> int:
        kv = self.client.secrets.kv.v2
        try:
            mount, rel = split_mount(path)
            r = self._call(kv.create_or_update_secret, path=rel, secret=data, cas=cas, mount_point=mount)
        except self._hvac.exceptions.InvalidRequest as e:
            if "check-and-set" in str(e):
                raise CasMismatch(f"{path}: cas={cas}") from None
            raise
        return int(((r or {}).get("data") or {}).get("version") or 0)

    def metadata(self, path: str) -> dict | None:
        kv = self.client.secrets.kv.v2
        try:
            mount, rel = split_mount(path)
            r = self._call(kv.read_secret_metadata, path=rel, mount_point=mount)
        except self._hvac.exceptions.InvalidPath:
            return None
        d = (r or {}).get("data") or {}
        return {
            "custom": d.get("custom_metadata") or {},
            "current_version": int(d.get("current_version") or 0),
            "versions": {
                int(n): {"destroyed": bool(v.get("destroyed")), "deleted": bool(v.get("deletion_time"))}
                for n, v in (d.get("versions") or {}).items()
            },
        }

    def set_metadata(self, path: str, metadata: dict, drop: set[str] | None = None) -> None:
        """Merge sobre custom_metadata (el endpoint de KV v2 reemplaza el mapa completo)."""
        kv = self.client.secrets.kv.v2
        cur = self.metadata(path)
        existing = {k: v for k, v in ((cur or {}).get("custom") or {}).items() if k not in (drop or set())}
        merged = {**existing, **custom_metadata(metadata)}
        mount, rel = split_mount(path)
        self._call(kv.update_metadata, path=rel, custom_metadata=merged, mount_point=mount)

    def list(self, prefix: str) -> list[str]:
        kv = self.client.secrets.kv.v2
        try:
            mount, rel = split_mount(prefix, root_ok=True)
            r = self._call(kv.list_secrets, path=rel, mount_point=mount)
        except self._hvac.exceptions.InvalidPath:
            return []
        return list(((r or {}).get("data") or {}).get("keys") or [])

    def healthy(self) -> bool:
        try:
            return bool(self.client.is_authenticated()) or (self._login() is None and self.client.is_authenticated())
        except Exception:
            return False


def build_vault(s: Settings) -> Vault:
    return MemoryVault() if s.vault_mode == "memory" else HvacVault(s)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
