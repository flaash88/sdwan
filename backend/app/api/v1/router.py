from fastapi import APIRouter

from app.api.v1 import audit, auth, content_filter, dashboard, devices, internal, mesh, metrics, pairing, policies, remote, sites, tenants, users, wan, ws, ztp

api_router = APIRouter(prefix="/api/v1")
for mod in (auth, users, tenants, sites, devices, pairing, internal, audit, dashboard, ws, mesh, wan, metrics, policies, ztp, content_filter, remote):
    api_router.include_router(mod.router)
