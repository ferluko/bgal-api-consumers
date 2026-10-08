"""FastAPI: rutas de openapi/consumers.yaml (v0.2.0-draft)."""

from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse

from . import __version__
from .config import Settings, get_settings
from .errors import Problem
from .integrations import build_threescale
from .logging_setup import setup_logging
from .models import ConsumerRequest, RotationAction, RotationRequest
from .service import Cred, Runner, Service
from .subscriber import build_subscriber
from .vault import build_vault

PROBLEM = "application/problem+json"


def build_service(s: Settings) -> Service:
    return Service(
        settings=s,
        vault=build_vault(s),
        subscriber=build_subscriber(s),
        threescale=build_threescale(s),
        runner=Runner(s.worker_mode, s.worker_threads),
    )


def create_app(settings: Settings | None = None, service: Service | None = None) -> FastAPI:
    s = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging(s.log_level)
        svc: Service = app.state.svc
        try:
            svc.recover()
        except Exception as e:  # Vault puede no estar disponible al arrancar; readyz lo refleja
            logging.getLogger(__name__).warning("recover al arrancar: %s", e)
        yield
        svc.runner.shutdown()

    app = FastAPI(
        title="API Consumers", version=__version__, lifespan=lifespan, docs_url="/docs", openapi_url="/openapi.json"
    )
    app.state.svc = service or build_service(s)

    @app.exception_handler(Problem)
    async def _problem(request: Request, exc: Problem):
        return JSONResponse(exc.to_dict(instance=request.url.path), status_code=exc.status, media_type=PROBLEM)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        detail = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        p = Problem(400, "Bad Request", detail)
        return JSONResponse(p.to_dict(instance=request.url.path), status_code=400, media_type=PROBLEM)

    def auth(authorization: str | None = Header(default=None)) -> None:
        tokens = s.tokens
        if not tokens:
            return
        presented = (authorization or "").removeprefix("Bearer ").strip()
        if not any(hmac.compare_digest(presented, t) for t in tokens):
            raise Problem(401, "Unauthorized", "token inválido o ausente")

    def svc() -> Service:
        return app.state.svc

    deps = [Depends(auth)]
    IdemKey = Header(alias="Idempotency-Key", max_length=200)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readyz(sv: Service = Depends(svc)):
        ok = sv.vault.healthy()
        return JSONResponse({"vault": ok}, status_code=200 if ok else 503)

    def cred(
        namespace: str,
        environment: str = Query(min_length=1, max_length=16),
        tenant: str = Query(pattern="^(b2b|b2c)$"),
        sv: Service = Depends(svc),
    ) -> Cred:
        """La credencial que identifican el namespace, el ambiente y el tenant (query obligatorios)."""
        return sv.cred(namespace, environment, tenant)

    # ----------------------------------------------------------- consumers
    @app.post("/v1/consumers", status_code=201, dependencies=deps, tags=["consumers"])
    def create_consumer(body: ConsumerRequest, idempotency_key: str = IdemKey, sv: Service = Depends(svc)):
        status, view = sv.create_consumer(body.model_dump(exclude_none=True), idempotency_key)
        return JSONResponse(view, status_code=status)

    @app.get("/v1/consumers/{namespace}", dependencies=deps, tags=["consumers"])
    def get_consumer(c: Cred = Depends(cred), sv: Service = Depends(svc)):
        return sv.get_consumer(c)

    @app.delete("/v1/consumers/{namespace}", dependencies=deps, tags=["consumers"])
    def delete_consumer(c: Cred = Depends(cred), idempotency_key: str = IdemKey, sv: Service = Depends(svc)):
        return sv.delete_consumer(c, idempotency_key)

    @app.get("/v1/consumers/{namespace}/secret-apim", dependencies=deps, tags=["consumers"])
    def get_secret_apim(c: Cred = Depends(cred), sv: Service = Depends(svc)):
        return PlainTextResponse(sv.secret_apim(c), media_type="application/yaml")

    # ----------------------------------------------------------- rotations
    @app.post("/v1/consumers/{namespace}/rotations", status_code=202, dependencies=deps, tags=["rotations"])
    def start_rotation(
        body: RotationRequest | None = None,
        c: Cred = Depends(cred),
        idempotency_key: str = IdemKey,
        sv: Service = Depends(svc),
    ):
        view = sv.start_rotation(c, body.model_dump(exclude_none=True) if body else {}, idempotency_key)
        query = urlencode({"environment": c.environment, "tenant": c.tenant})
        loc = f"/v1/consumers/{c.namespace}/rotations/{view['id']}?{query}"
        return JSONResponse(view, status_code=202, headers={"Location": loc})

    @app.get("/v1/consumers/{namespace}/rotations/{rotation_id}", dependencies=deps, tags=["rotations"])
    def get_rotation(rotation_id: str, c: Cred = Depends(cred), sv: Service = Depends(svc)):
        return sv.get_rotation(c, rotation_id)

    @app.patch(
        "/v1/consumers/{namespace}/rotations/{rotation_id}", status_code=202, dependencies=deps, tags=["rotations"]
    )
    def update_rotation(
        rotation_id: str,
        body: RotationAction,
        c: Cred = Depends(cred),
        idempotency_key: str = IdemKey,  # noqa: ARG001 (la acción es idempotente por estado)
        sv: Service = Depends(svc),
    ):
        return JSONResponse(sv.update_rotation(c, rotation_id, body.action), status_code=202)

    return app


def run() -> None:
    import uvicorn

    s = get_settings()
    uvicorn.run(
        "api_consumers.main:create_app",
        factory=True,
        host=s.listen_host,
        port=s.listen_port,
        proxy_headers=True,
        log_config=None,
    )
