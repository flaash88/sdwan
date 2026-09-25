"""„Rechte einschränken“: API-Benutzer von ``full`` (Altgeräte) auf die eigene Gruppe umstellen.

Ablauf mit Totmannschaltung – die Plattform darf sich dabei nicht selbst aussperren:

1. Vorherige Gruppe des API-Benutzers auslesen (nicht fest ``full`` annehmen).
2. Scheduler ``sdwan-revert-api-group`` anlegen: läuft einmal nach ca. 3 Minuten, stellt den Benutzer auf die
   vorherige Gruppe zurück und entfernt sich selbst.
3. Gruppe ``sdwan-api`` anlegen/aktualisieren und zurücklesen. Stimmen die Policies nicht exakt, wird nicht
   umgestellt (Scheduler wird entfernt).
4. Benutzer umstellen, alte Verbindung schließen.
5. NEUE API-Verbindung aufbauen und den Selbsttest laufen lassen. Nur wenn beides klappt (Selbsttest nicht rot),
   wird der Scheduler gelöscht. Sonst bleibt er stehen und stellt die Rechte automatisch zurück.
"""

from __future__ import annotations

import logging
from typing import Any

from app.models import Device
from app.routeros import RouterOSError, connect_device
from app.routeros.client import DeviceAPI
from app.routeros.schema import API_GROUP, API_POLICIES, policy_set

log = logging.getLogger(__name__)

REVERT_SCHEDULER = "sdwan-revert-api-group"
# Annahme, im Labor zu verifizieren: ein Scheduler mit interval=3m und ohne start-time läuft zum ersten Mal
# ca. 3 Minuten nach dem Anlegen (next-run in /system scheduler print prüfen). Das vermeidet die
# versionsabhängigen Datumsformate von start-date. Er entfernt sich beim ersten Lauf selbst.
REVERT_AFTER = "3m"
# Rechte, mit denen das on-event-Skript läuft (Benutzer ändern, Scheduler entfernen)
REVERT_POLICY = "read,write,policy,test"


def revert_script(user: str, group: str) -> str:
    return f'/user set [find name="{user}"] group="{group}"; /system scheduler remove [find name="{REVERT_SCHEDULER}"]'


async def ensure_group(api: DeviceAPI, name: str, policies: tuple[str, ...], comment: str) -> bool:
    """Gruppe anlegen bzw. Policies setzen und zurücklesen. True, wenn die Policies exakt stimmen."""
    want = ",".join(policies)
    rows = await api.print("/user/group", name=name)
    if rows:
        if policy_set(rows[0].get("policy")) != set(policies):
            await api.set("/user/group", rows[0][".id"], policy=want)
    else:
        await api.add("/user/group", name=name, policy=want, comment=comment)
    back = await api.print("/user/group", name=name)
    return bool(back) and policy_set(back[0].get("policy")) == set(policies)


async def _remove_scheduler(api: DeviceAPI) -> None:
    for r in await api.print("/system/scheduler", name=REVERT_SCHEDULER):
        await api.remove("/system/scheduler", r[".id"])


async def restrict_api_user(device: Device) -> dict[str, Any]:
    """Führt die Umstellung aus. Rückgabe ``status``: ok | unchanged | readback_mismatch | reverting."""
    from app.config import get_settings
    from app.services.selftest import run_selftest

    user = get_settings().routeros_api_user
    async with connect_device(device) as api:
        rows = await api.print("/user", name=user)
        if not rows:
            raise RouterOSError(f"API-Benutzer '{user}' nicht gefunden")
        previous = str(rows[0].get("group", ""))
        if previous == API_GROUP:
            ok = await ensure_group(api, API_GROUP, API_POLICIES, "sdwan:mgmt")
            return {"status": "unchanged" if ok else "readback_mismatch", "previous_group": previous}
        await _remove_scheduler(api)
        await api.add("/system/scheduler", name=REVERT_SCHEDULER, interval=REVERT_AFTER, policy=REVERT_POLICY,
                      **{"on-event": revert_script(user, previous)}, comment="sdwan:mgmt Totmannschaltung")
        if not await ensure_group(api, API_GROUP, API_POLICIES, "sdwan:mgmt"):
            await _remove_scheduler(api)  # nichts umgestellt -> Totmannschaltung überflüssig
            return {"status": "readback_mismatch", "previous_group": previous,
                    "message": f"Gruppe '{API_GROUP}' hat nach dem Anlegen nicht exakt die Policies {', '.join(API_POLICIES)} – "
                               "Benutzer wurde nicht umgestellt"}
        await api.set("/user", rows[0][".id"], group=API_GROUP)
    # alte Sitzung ist geschlossen; ab hier nur neue Verbindungen
    try:
        async with connect_device(device) as api:
            await api.call("/system/identity/print")
    except RouterOSError as exc:
        return {"status": "reverting", "previous_group": previous, "selftest": None,
                "message": f"Neue Verbindung nach der Umstellung fehlgeschlagen: {exc}"}
    selftest = await run_selftest(device)
    if selftest["status"] == "error":
        return {"status": "reverting", "previous_group": previous, "selftest": selftest,
                "message": "Selbsttest nach der Umstellung mit Fehlern"}
    try:
        async with connect_device(device) as api:
            await _remove_scheduler(api)
    except RouterOSError as exc:
        return {"status": "reverting", "previous_group": previous, "selftest": selftest,
                "message": f"Totmannschaltung konnte nicht entfernt werden: {exc}"}
    return {"status": "ok", "previous_group": previous, "selftest": selftest}
