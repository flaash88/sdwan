import type { Role } from "./lib/types";

export interface NavItem {
  to: string;
  label: string;
  icon: string;
  role?: Role;
  superuser?: boolean;
  needsTenant?: boolean;
}

export const NAV: NavItem[] = [
  { to: "/", label: "Dashboard", icon: "◧" },
  { to: "/devices", label: "Geräte", icon: "⌁" },
  { to: "/sites", label: "Standorte", icon: "⌂" },
  { to: "/mesh", label: "VPN-Mesh", icon: "⬡" },
  { to: "/users", label: "Benutzer", icon: "☺", role: "admin" },
  { to: "/tenants", label: "Mandanten", icon: "▣", superuser: true },
  { to: "/audit", label: "Audit-Log", icon: "☰", role: "admin" },
];
