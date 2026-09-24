from fastapi import APIRouter

from app.api.v1 import audit, auth, dashboard, devices, internal, mesh, pairing, sites, tenants, users, wan, ws

api_router = APIRouter(prefix="/api/v1")
for mod in (auth, users, tenants, sites, devices, pairing, internal, audit, dashboard, ws, mesh, wan):
    api_router.include_router(mod.router)
