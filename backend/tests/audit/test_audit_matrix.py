"""Audit B.1/B.2 – Mandantentrennung (IDOR) und Rollen-Matrix über ALLE Endpunkte der App.

* IDOR: Benutzer von Mandant A rufen jeden Endpunkt mit den IDs von Mandant B auf – im Pfad UND im Body
  (Felder ``*_id``/``*_ids``). Erwartet: nie 2xx; B-Objekte bleiben unverändert.
* Datenabfluss: Listen-/Detail-Endpunkte von A dürfen den Marker aus B-Objektnamen nicht enthalten.
* RBAC: Ist-Status je Rolle × Endpunkt (eigene IDs) gegen die im Code deklarierte Mindestrolle
  (ReadCtx/TechCtx/AdminCtx/SuperCtx). Ergebnis-Matrix: ``AUDIT_MATRIX_OUT=<datei.csv>`` setzen.

Gefundene Abweichungen stehen als eigene xfail-Tests (AUDIT-xxx) in ``test_audit_findings.py``; dieser Test prüft die
Grundregeln und bleibt grün, solange keine NEUE Verletzung auftaucht (bekannte sind in KNOWN_* erfasst).
"""

from __future__ import annotations

import csv
import os
import re
import time
from typing import Any

from app.main import app
from tests.audit._world import BMARK, build_tenant, param_value

SKIP_PREFIX = ("/api/v1/internal/", "/api/v1/onboard/", "/api/v1/pair", "/api/v1/auth/login", "/api/v1/hotspot/public", "/healthz",
               "/api/v1/ws", "/api/v1/meta")
# Endpunkte, die für jeden angemeldeten Benutzer schreibend erlaubt sind (eigenes Konto)
SELF_SERVICE = {"/api/v1/auth/2fa/setup", "/api/v1/auth/2fa/enable", "/api/v1/auth/2fa/disable", "/api/v1/auth/2fa/recovery-codes",
                "/api/v1/auth/api-tokens", "/api/v1/auth/api-tokens/{token_id}", "/api/v1/auth/logout", "/api/v1/auth/password"}
RANK = {"readonly": 0, "technician": 1, "admin": 2, "super": 3}


def _routes() -> list[tuple[str, str, Any]]:
    out = []
    for r in app.routes:
        if not hasattr(r, "methods") or r.path.startswith(SKIP_PREFIX) or not r.path.startswith("/api/"):
            continue
        for m in sorted(r.methods - {"HEAD", "OPTIONS"}):
            out.append((m, r.path, r))
    order = {"GET": 0, "POST": 1, "PUT": 1, "PATCH": 1, "DELETE": 2}
    return sorted(out, key=lambda x: (order[x[0]], x[1]))


def declared_role(route: Any) -> str:
    """Mindestrolle aus den Dependencies (require_role-Closure bzw. require_superuser); sonst 'auth'/'none'."""
    found = "none"

    def walk(dep: Any) -> None:
        nonlocal found
        call = dep.call
        name = getattr(call, "__name__", "")
        if name == "require_superuser":
            found = "super"
        elif name == "_dep" and call.__closure__:
            for c in call.__closure__:
                v = c.cell_contents
                if hasattr(v, "value") and v.value in RANK:
                    if found not in ("super",) and (found not in RANK or RANK[v.value] > RANK.get(found, -1)):
                        found = v.value
        elif name == "get_ctx" and found == "none":
            found = "auth"
        for d in dep.dependencies:
            walk(d)

    for d in route.dependant.dependencies:
        walk(d)
    return found


_SPEC: dict[str, Any] | None = None


def _schema(ref: dict[str, Any]) -> dict[str, Any]:
    global _SPEC
    _SPEC = _SPEC or app.openapi()
    while "$ref" in ref:
        name = ref["$ref"].split("/")[-1]
        ref = _SPEC["components"]["schemas"][name]
    return ref


def dummy(schema: dict[str, Any], target: Any, path: str, name: str = "") -> Any:
    """Minimaler Body aus dem JSON-Schema; ID-Felder zeigen auf die Objekte von ``target`` (Body-IDOR)."""
    s = _schema(schema)
    for key in ("anyOf", "oneOf", "allOf"):
        if key in s:
            opts = [o for o in s[key] if _schema(o).get("type") != "null"]
            return dummy(opts[0], target, path, name) if opts else None
    typ = s.get("type")
    if name.endswith("_ids") or name in ("device_ids", "site_ids"):
        v = param_value(target, path, name[:-1])
        return [v] if v else []
    if name.endswith("_id"):
        return param_value(target, path, name) or "00000000-0000-0000-0000-000000000000"
    if "enum" in s:
        return s["enum"][0]
    if "default" in s and s["default"] is not None:
        return s["default"]
    if typ == "object" or "properties" in s:
        req = s.get("required", [])
        return {k: dummy(v, target, path, k) for k, v in s.get("properties", {}).items() if k in req or k.endswith(("_id", "_ids"))}
    if typ == "array":
        return []
    if typ == "integer":
        return max(1, int(s.get("minimum", 1)))
    if typ == "number":
        return 1
    if typ == "boolean":
        return False
    if s.get("format") == "email":
        return "x@example.com"
    if s.get("format") == "uuid":
        return "00000000-0000-0000-0000-000000000000"
    if s.get("format") in ("date-time",):
        return "2030-01-01T00:00:00Z"
    return "x" * max(1, int(s.get("minLength", 1)))


def body_for(method: str, path: str, target: Any) -> Any:
    if method in ("GET", "DELETE"):
        return None
    op = (app.openapi()["paths"].get(path) or {}).get(method.lower()) or {}
    rb = ((op.get("requestBody") or {}).get("content") or {}).get("application/json")
    return dummy(rb["schema"], target, path) if rb else None


def fill(path: str, t: Any) -> str | None:
    missing = []

    def sub(m: re.Match[str]) -> str:
        v = param_value(t, path, m.group(1))
        if v is None:
            missing.append(m.group(1))
            return "x"
        return str(v)

    p = re.sub(r"{(\w+)}", sub, path)
    return None if missing else p


EXCEPTIONS: list[str] = []
FILTERED: list[str] = []
# Lesende Operationen per POST (Vorschau/Live-Anforderung) – für readonly beabsichtigt (ReadCtx)
READ_POSTS = {"POST /api/v1/devices/{device_id}/metrics/live", "POST /api/v1/fw/blocks/{block_id}/expand",
              "POST /api/v1/hotspot/preview", "POST /api/v1/policies/{policy_id}/preview"}


async def call(client, method: str, path: str, headers: dict[str, str], body: Any) -> tuple[int, str]:
    try:
        r = await client.request(method, path, headers=headers, json=body if body is not None else None)
    except Exception as exc:  # noqa: BLE001 - unbehandelte Ausnahme der App = in Produktion HTTP 500
        EXCEPTIONS.append(f"{method} {path}: {type(exc).__name__}: {str(exc)[:100]}")
        return 599, ""
    return r.status_code, r.text


# Bekannte, im Bericht dokumentierte Abweichungen (Endpunkt-Schlüssel "METHOD path") – siehe test_audit_findings.py
KNOWN_IDOR: set[str] = set()
KNOWN_RBAC: set[str] = set()


async def test_tenant_isolation_and_rbac_matrix(client, msp, hub, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "remote_proxy_port_range", "41900-41939")
    t0 = time.time()
    b = await build_tenant(client, msp, "bbb", BMARK)
    a = await build_tenant(client, msp, "aaa")
    build_s = time.time() - t0
    routes = _routes()
    rows: list[dict[str, Any]] = []
    idor: list[str] = []
    leaks: list[str] = []
    rbac: list[str] = []

    # 1) IDOR: A-Admin (und A-Techniker) mit B-IDs – nur Endpunkte, die IDs enthalten (Pfad oder Body)
    for method, path, route in routes:
        if declared_role(route) == "super":
            continue
        p = fill(path, b) if "{" in path else path
        body = body_for(method, path, b)
        bset = set(filter(None, b.ids.values()))
        has_b = "{" in path or (body is not None and any(v in bset for v in _ids_in(body)))
        if p is None or not has_b:
            continue
        if "{tenant_id}" in path:
            continue  # Mandanten-Verwaltung ist SuperCtx
        for role, h in (("tech", a.tech), ("admin", a.admin)):
            st, text = await call(client, method, p, h, body)
            if st < 300 and not path.startswith("/api/v1/auth/"):
                key = f"{method} {path}"
                # Verletzung, wenn die Antwort B-Daten enthält oder ein Pfad-Objekt von B angesprochen wurde;
                # 2xx ohne B-Bezug (z. B. Massenaktion filtert fremde IDs weg) nur protokollieren
                touches_b = BMARK in text.lower() or any(v in text for v in bset) or "{" in path
                if key not in KNOWN_IDOR and touches_b:
                    idor.append(f"{role} {key} -> {st} {text[:120]}")
                elif not touches_b:
                    FILTERED.append(f"{role} {key} -> {st} (fremde IDs verworfen)")

    # 2) Datenabfluss: alle GET von A (eigene IDs) dürfen den B-Marker nicht enthalten
    for method, path, route in routes:
        if method != "GET" or declared_role(route) == "super":
            continue
        p = fill(path, a) if "{" in path else path
        if p is None:
            continue
        for role, h in (("ro", a.ro), ("admin", a.admin)):
            st, text = await call(client, method, p, h, None)
            if st < 300 and BMARK in text.lower():
                leaks.append(f"{role} GET {path}")

    # 3) Rollen-Matrix mit eigenen IDs (ro → Token → tech → admin → MSP); DELETE zuletzt (Reihenfolge von _routes)
    headers = [("readonly", a.ro), ("token_read", a.token_read), ("technician", a.tech), ("token_role", a.token_role),
               ("admin", a.admin), ("msp", {**msp, "X-Tenant-ID": a.id})]
    for method, path, route in routes:
        decl = declared_role(route)
        p = fill(path, a) if "{" in path else path
        row = {"method": method, "path": path, "declared": decl}
        if p is None:
            row.update({k: "n/a" for k, _ in headers})
            rows.append(row)
            continue
        body = body_for(method, path, a)
        for role, h in headers:
            if not h:
                row[role] = "no-token"
                continue
            st, _text = await call(client, method, p, h, body)
            row[role] = st
            ok = st < 300
            key = f"{method} {path}"
            write = method != "GET"
            if role in ("readonly", "token_read") and write and ok and path not in SELF_SERVICE and key not in KNOWN_RBAC and key not in READ_POSTS:
                rbac.append(f"{role} darf schreiben: {key} -> {st}")
            if role == "technician" and ok and decl in ("admin", "super") and key not in KNOWN_RBAC:
                rbac.append(f"technician trotz {decl}: {key} -> {st}")
        rows.append(row)

    out = os.environ.get("AUDIT_MATRIX_OUT")
    if out:
        with open(out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["method", "path", "declared", *[k for k, _ in headers]], delimiter=";")
            w.writeheader()
            w.writerows(rows)
        with open(out + ".meta.txt", "w") as f:
            f.write(f"build_s={build_s:.1f} total_s={time.time() - t0:.1f} routes={len(routes)}\n")
            f.write("B-Aufbau fehlgeschlagen:\n" + "\n".join(b.failures) + "\nA-Aufbau fehlgeschlagen:\n" + "\n".join(a.failures) + "\n")
            f.write("EXCEPTIONS (unbehandelt, 500):\n" + "\n".join(sorted(set(EXCEPTIONS))) + "\n")
            f.write("2xx ohne Wirkung (fremde IDs verworfen):\n" + "\n".join(sorted(set(FILTERED))) + "\n")
            f.write("IDOR:\n" + "\n".join(idor) + "\nLEAKS:\n" + "\n".join(leaks) + "\nRBAC:\n" + "\n".join(rbac) + "\n")
    assert not idor, "Mandantenübergriff:\n" + "\n".join(idor)
    assert not leaks, "Datenabfluss:\n" + "\n".join(leaks)
    assert not rbac, "Rollenverletzung:\n" + "\n".join(rbac)


def _ids_in(obj: Any) -> list[str]:
    if isinstance(obj, dict):
        return [x for v in obj.values() for x in _ids_in(v)]
    if isinstance(obj, list):
        return [x for v in obj for x in _ids_in(v)]
    return [obj] if isinstance(obj, str) else []
