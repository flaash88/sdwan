import type { IconName } from "./components/Icon";
import type { Role } from "./lib/types";

export interface NavItem {
  to: string;
  label: string;
  icon: IconName;
  role?: Role;
  superuser?: boolean;
  /** Zähler-Badge: "alarms" (rot) oder "firmware" */
  badge?: "alarms" | "firmware";
}

export const NAV: NavItem[] = [
  { to: "/", label: "Dashboard", icon: "grid" },
  { to: "/devices", label: "Geräte", icon: "router" },
  { to: "/sites", label: "Standorte", icon: "pin" },
  { to: "/mesh", label: "VPN-Mesh", icon: "network" },
  { to: "/policies", label: "Firewall-Policies", icon: "shield" },
  { to: "/feeds", label: "Threat-Feeds", icon: "octagon" },
  { to: "/compliance", label: "Compliance", icon: "checkCircle" },
  { to: "/config-search", label: "Config-Suche", icon: "search" },
  { to: "/scripts", label: "Scripts", icon: "terminal" },
  { to: "/wlan", label: "WLAN", icon: "signal" },
  { to: "/hotspot", label: "Gäste-Portal", icon: "users" },
  { to: "/ztp", label: "Zero-Touch", icon: "package", role: "technician" },
  { to: "/content-filter", label: "Content-Filter", icon: "filter" },
  { to: "/firmware", label: "Firmware", icon: "cpu", badge: "firmware" },
  { to: "/advisories", label: "Sicherheitsmeldungen", icon: "alert" },
  { to: "/maintenance", label: "Wartungsfenster", icon: "clock" },
  { to: "/alerts", label: "Alarme", icon: "bell", badge: "alarms" },
  { to: "/reports", label: "Berichte", icon: "chart" },
  { to: "/remote", label: "Fernzugriff", icon: "terminal" },
  { to: "/audit", label: "Audit-Log", icon: "list", role: "admin" },
];

/** Bereich "Verwaltung" */
export const NAV_ADMIN: NavItem[] = [
  { to: "/users", label: "Benutzer", icon: "users", role: "admin" },
  { to: "/offboarding", label: "Offboarding-Archiv", icon: "archive", role: "admin" },
  { to: "/tenants", label: "Mandanten", icon: "building", superuser: true },
  { to: "/platform/backup", label: "Plattform-Sicherung", icon: "archive", superuser: true },
];
