from fastapi import APIRouter

from app.api.v1 import alerts, audit, auth, content_filter, dashboard, device_ops, devices, internal, mesh, metrics, ops, pairing, policies, remote, sites, tenants, users, vrrp, wan, ws, ztp

api_router = APIRouter(prefix="/api/v1")
for mod in (auth, users, tenants, sites, devices, pairing, internal, audit, dashboard, ws, mesh, wan, metrics, policies, ztp, content_filter, remote, ops, alerts, vrrp, device_ops):
    api_router.include_router(mod.router)
