"""Minimaler NextDNS-API-Client (https://nextdns.github.io/api/)."""

from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings

CATEGORIES = {
    "porn": "Pornografie", "gambling": "Glücksspiel", "dating": "Dating", "piracy": "Piraterie & Warez",
    "social-networks": "Soziale Netzwerke", "gaming": "Online-Gaming", "video-streaming": "Video-Streaming",
}
SERVICES = [
    "tiktok", "instagram", "facebook", "snapchat", "twitter", "youtube", "netflix", "twitch", "discord", "reddit",
    "whatsapp", "telegram", "fortnite", "roblox", "minecraft", "steam", "spotify", "tinder", "9gag", "pinterest",
]
SECURITY = {
    "threatIntelligenceFeeds": "Threat-Intelligence-Feeds", "aiThreatDetection": "KI-Bedrohungserkennung",
    "googleSafeBrowsing": "Google Safe Browsing", "cryptojacking": "Cryptojacking", "dnsRebinding": "DNS-Rebinding",
    "idnHomographs": "IDN-Homographen", "typosquatting": "Typosquatting", "dga": "DGA-Domains",
    "nrd": "Neu registrierte Domains", "ddns": "Dynamic-DNS-Domains", "parking": "Geparkte Domains", "csam": "CSAM",
}
BLOCKLISTS = ["nextdns-recommended", "oisd", "adguard-dns-filter", "easylist", "1hosts-lite", "hagezi-multi-normal", "steven-black"]
DEFAULT_SECURITY = {k: k not in ("nrd", "ddns", "parking") for k in SECURITY}

# Tests können hier einen httpx.MockTransport einhängen
transport_override: httpx.AsyncBaseTransport | None = None


class NextDNSError(Exception):
    pass


class NextDNSClient:
    def __init__(self, api_key: str | None = None) -> None:
        s = get_settings()
        self.api_key = api_key or s.nextdns_api_key
        if not self.api_key:
            raise NextDNSError("Kein NextDNS-API-Key konfiguriert (NEXTDNS_API_KEY oder Mandanten-Einstellung)")
        self.base = s.nextdns_api_url.rstrip("/")

    async def _req(self, method: str, path: str, json: Any = None) -> Any:
        async with httpx.AsyncClient(base_url=self.base, headers={"X-Api-Key": self.api_key}, timeout=15, transport=transport_override) as c:
            r = await c.request(method, path, json=json)
        if r.status_code >= 400:
            raise NextDNSError(f"NextDNS {method} {path}: HTTP {r.status_code} {r.text[:200]}")
        if not r.content:
            return None
        data = r.json()
        if isinstance(data, dict) and data.get("errors"):
            raise NextDNSError(f"NextDNS {method} {path}: {data['errors']}")
        return data.get("data") if isinstance(data, dict) else data

    async def create_profile(self, name: str) -> str:
        data = await self._req("POST", "/profiles", {"name": name})
        return str(data["id"])

    async def delete_profile(self, profile_id: str) -> None:
        await self._req("DELETE", f"/profiles/{profile_id}")

    async def apply(self, profile_id: str, cfg: dict[str, Any]) -> None:
        p = f"/profiles/{profile_id}"
        await self._req("PATCH", p, {"name": cfg["name"]})
        await self._req("PATCH", f"{p}/security", cfg["security"])
        await self._req("PATCH", f"{p}/parentalControl", {
            "safeSearch": cfg["safe_search"], "youtubeRestrictedMode": cfg["youtube_restricted"], "blockBypass": cfg["block_bypass"],
        })
        await self._req("PUT", f"{p}/parentalControl/categories", [{"id": c, "active": True} for c in cfg["categories"]])
        await self._req("PUT", f"{p}/parentalControl/services", [{"id": s, "active": True} for s in cfg["services"]])
        await self._req("PUT", f"{p}/privacy/blocklists", [{"id": b} for b in cfg["blocklists"]])
        await self._req("PUT", f"{p}/denylist", [{"id": d, "active": True} for d in cfg["denylist"]])
        await self._req("PUT", f"{p}/allowlist", [{"id": d, "active": True} for d in cfg["allowlist"]])

    async def analytics_status(self, profile_id: str) -> Any:
        return await self._req("GET", f"/profiles/{profile_id}/analytics/status?from=-24h")
