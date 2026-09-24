"""Pydantic-Schemas für die Kern-API (Phase 1)."""

from __future__ import annotations

import datetime as dt
import ipaddress
import re
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models import DeviceStatus, PairingStatus, Role


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- Auth --------------------------------------------------------------------
class LoginIn(BaseModel):
    email: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class UserBase(BaseModel):
    email: EmailStr
    full_name: str | None = None
    role: Role = Role.readonly


class UserCreate(UserBase):
    password: str = Field(min_length=8)
    tenant_id: uuid.UUID | None = None
    is_superuser: bool = False


class UserUpdate(BaseModel):
    full_name: str | None = None
    role: Role | None = None
    password: str | None = Field(default=None, min_length=8)
    is_active: bool | None = None


class UserOut(ORM):
    id: uuid.UUID
    email: str
    full_name: str | None
    role: Role
    tenant_id: uuid.UUID | None
    is_superuser: bool
    is_active: bool
    last_login_at: dt.datetime | None = None


# --- Tenants -----------------------------------------------------------------
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


class TenantCreate(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    slug: str
    contact_email: EmailStr | None = None
    mesh_topology: str = "hub_spoke"

    @field_validator("slug")
    @classmethod
    def check_slug(cls, v: str) -> str:
        if not SLUG_RE.match(v):
            raise ValueError("slug: nur a-z, 0-9 und '-' (2-63 Zeichen)")
        return v

    @field_validator("mesh_topology")
    @classmethod
    def check_topo(cls, v: str) -> str:
        if v not in ("hub_spoke", "full_mesh", "none"):
            raise ValueError("mesh_topology: hub_spoke | full_mesh | none")
        return v


class TenantUpdate(BaseModel):
    name: str | None = None
    contact_email: EmailStr | None = None
    is_active: bool | None = None
    mesh_topology: str | None = None

    @field_validator("mesh_topology")
    @classmethod
    def check_topo(cls, v: str | None) -> str | None:
        return TenantCreate.check_topo(v) if v else v


class TenantOut(ORM):
    id: uuid.UUID
    name: str
    slug: str
    contact_email: str | None
    is_active: bool
    mesh_topology: str
    mesh_subnet: str | None
    created_at: dt.datetime


# --- Sites -------------------------------------------------------------------
def _validate_subnets(v: list[str]) -> list[str]:
    out = []
    for s in v:
        out.append(str(ipaddress.ip_network(s, strict=False)))
    return out


class SiteCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    address: str | None = None
    description: str | None = None
    lan_subnets: list[str] = []
    is_mesh_hub: bool = False
    latitude: float | None = None
    longitude: float | None = None
    tenant_id: uuid.UUID | None = None  # nur MSP-Admin ohne gewählten Tenant

    @field_validator("lan_subnets")
    @classmethod
    def check_subnets(cls, v: list[str]) -> list[str]:
        return _validate_subnets(v)


class SiteUpdate(BaseModel):
    name: str | None = None
    address: str | None = None
    description: str | None = None
    lan_subnets: list[str] | None = None
    is_mesh_hub: bool | None = None
    latitude: float | None = None
    longitude: float | None = None

    @field_validator("lan_subnets")
    @classmethod
    def check_subnets(cls, v: list[str] | None) -> list[str] | None:
        return _validate_subnets(v) if v is not None else v


class SiteOut(ORM):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    address: str | None
    description: str | None
    lan_subnets: list[str]
    is_mesh_hub: bool
    latitude: float | None
    longitude: float | None
    created_at: dt.datetime


# --- Devices -----------------------------------------------------------------
class DeviceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    site_id: uuid.UUID | None = None
    serial: str | None = None
    tags: list[str] = []
    notes: str | None = None


class DeviceUpdate(BaseModel):
    name: str | None = None
    site_id: uuid.UUID | None = None
    mesh_endpoint: str | None = None
    tags: list[str] | None = None
    notes: str | None = None


class DeviceOut(ORM):
    id: uuid.UUID
    tenant_id: uuid.UUID
    site_id: uuid.UUID | None
    name: str
    serial: str | None
    model: str | None
    identity: str | None
    routeros_version: str | None
    architecture: str | None
    tunnel_ip: str
    wg_public_key: str | None
    mesh_ip: str | None = None
    mesh_endpoint: str | None = None
    pairing_status: PairingStatus
    pairing_expires_at: dt.datetime | None
    paired_at: dt.datetime | None
    status: DeviceStatus
    last_seen_at: dt.datetime | None
    last_handshake_at: dt.datetime | None
    uptime: str | None
    tags: list[str]
    notes: str | None
    facts: dict[str, Any]
    created_at: dt.datetime


class PairingInfo(BaseModel):
    token: str
    expires_at: dt.datetime
    command: str
    script_url: str


class DeviceCreated(BaseModel):
    device: DeviceOut
    pairing: PairingInfo


class PairIn(BaseModel):
    token: str
    public_key: str
    serial: str | None = None
    routeros_version: str | None = None
    model: str | None = None
    architecture: str | None = None
    identity: str | None = None


# --- Audit -------------------------------------------------------------------
class AuditOut(ORM):
    id: uuid.UUID
    tenant_id: uuid.UUID | None
    user_email: str | None
    action: str
    target_type: str | None
    target_id: str | None
    details: dict[str, Any]
    ip_address: str | None
    success: bool
    created_at: dt.datetime


# --- Hub (intern) ------------------------------------------------------------
class HubRegisterIn(BaseModel):
    public_key: str
    endpoint: str | None = None


class HubPeer(BaseModel):
    public_key: str
    allowed_ips: str
    device_id: uuid.UUID


class HubPeerStatIn(BaseModel):
    public_key: str
    endpoint: str | None = None
    latest_handshake: int = 0  # Unix-Timestamp, 0 = nie
    rx_bytes: int = 0
    tx_bytes: int = 0


TokenOut.model_rebuild()
