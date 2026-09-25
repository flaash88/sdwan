"""Policy-Listen der Benutzergruppen (routeros/schema.py) dürfen nicht auseinanderlaufen."""

from __future__ import annotations

from app.routeros.schema import API_CORE_POLICIES, API_POLICIES, API_RECOMMENDED_POLICIES, REMOTE_POLICIES


def test_remote_policies_subset_of_api_policies():
    # RouterOS lässt (Annahme) keine Gruppe mit Rechten anlegen, die der API-Benutzer selbst nicht hat –
    # sonst scheitert jeder Fernzugriff.
    extra = set(REMOTE_POLICIES) - set(API_POLICIES)
    assert not extra, f"REMOTE_POLICIES enthält Policies, die API_POLICIES fehlen: {sorted(extra)}"


def test_api_policies_split_into_core_and_recommended():
    assert set(API_POLICIES) == set(API_CORE_POLICIES) | set(API_RECOMMENDED_POLICIES)
    assert not set(API_CORE_POLICIES) & set(API_RECOMMENDED_POLICIES)
    assert len(API_POLICIES) == len(set(API_POLICIES)) and len(REMOTE_POLICIES) == len(set(REMOTE_POLICIES))


def test_policy_lists_target_state():
    assert set(API_POLICIES) == {"read", "write", "api", "policy", "reboot", "test", "ssh", "sensitive", "winbox", "web"}
    assert set(REMOTE_POLICIES) == {"ssh", "read", "write", "test", "winbox", "web", "reboot", "sensitive"}
    # Temporäre Benutzer: keine Benutzerverwaltung, kein API-Zugriff, kein Konsolen-Login
    assert not {"policy", "api", "local"} & set(REMOTE_POLICIES)
