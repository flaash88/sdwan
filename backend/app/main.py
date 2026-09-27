from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.router import api_router
from app.bootstrap import ensure_bootstrap_admin
from app.config import get_settings
from app.db import TenantIsolationError, create_all
from app.proxy import TrustedProxyMiddleware
from app.routeros.schema import TRUST_ANCHORS_MIN_VERSION
from app.secrets_check import InsecureSecretsError
from app.secrets_check import enforce as enforce_secrets

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    try:
        enforce_secrets(s)  # AUDIT-006: Produktion startet nicht mit Standard-Geheimnissen
    except InsecureSecretsError as exc:
        logging.getLogger("app").critical(str(exc))
        raise
    if s.db_auto_create:
        await create_all()
    await ensure_bootstrap_admin()
    from app.seeds import apply_all as apply_seeds

    await apply_seeds()
    yield


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title=s.app_name, version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # AUDIT-003: X-Forwarded-For nur von konfigurierten Proxys (äußerste Schicht, vor CORS)
    app.add_middleware(TrustedProxyMiddleware, trusted=s.trusted_proxies)

    @app.exception_handler(TenantIsolationError)
    async def _iso(_req: Request, exc: TenantIsolationError) -> JSONResponse:
        return JSONResponse({"detail": "Zugriff auf fremden Tenant verweigert"}, status_code=403)

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/api/v1/meta", tags=["health"])
    async def meta() -> dict:
        return {
            "version": app.version,
            "simulator": s.routeros_backend == "simulator",
            "grafana_url": s.grafana_public_url,
            "hub_endpoint": f"{s.wg_hub_endpoint}:{s.wg_hub_port}",
            "management_network": s.wg_network,
            "smtp_configured": bool(s.smtp_host),
            "product_name": s.product_name,
            "product_short": s.product_short,
            "onboarding_min_routeros": TRUST_ANCHORS_MIN_VERSION,
        }

    app.include_router(api_router)
    return app


app = create_app()
