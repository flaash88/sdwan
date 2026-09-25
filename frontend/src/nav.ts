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
  { to: "/ztp", label: "Zero-Touch", icon: "package", role: "technician" },
  { to: "/content-filter", label: "Content-Filter", icon: "filter" },
  { to: "/firmware", label: "Firmware", icon: "cpu", badge: "firmware" },
  { to: "/alerts", label: "Alarme", icon: "bell", badge: "alarms" },
  { to: "/reports", label: "Berichte", icon: "chart" },
  { to: "/remote", label: "Fernzugriff", icon: "terminal" },
  { to: "/audit", label: "Audit-Log", icon: "list", role: "admin" },
];

/** Bereich "Verwaltung" */
export const NAV_ADMIN: NavItem[] = [
  { to: "/users", label: "Benutzer", icon: "users", role: "admin" },
  { to: "/tenants", label: "Mandanten", icon: "building", superuser: true },
];
