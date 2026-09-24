export type Role = "admin" | "technician" | "readonly";

export interface User {
  id: string;
  email: string;
  full_name: string | null;
  role: Role;
  tenant_id: string | null;
  is_superuser: boolean;
  is_active: boolean;
  last_login_at: string | null;
}

export interface Tenant {
  id: string;
  name: string;
  slug: string;
  contact_email: string | null;
  is_active: boolean;
  mesh_topology: "hub_spoke" | "full_mesh" | "none";
  mesh_subnet: string | null;
  created_at: string;
}

export interface Site {
  id: string;
  tenant_id: string;
  name: string;
  address: string | null;
  description: string | null;
  lan_subnets: string[];
  is_mesh_hub: boolean;
  latitude: number | null;
  longitude: number | null;
}

export type DeviceStatus = "online" | "offline" | "unknown";

export interface Device {
  id: string;
  tenant_id: string;
  site_id: string | null;
  name: string;
  serial: string | null;
  model: string | null;
  identity: string | null;
  routeros_version: string | null;
  architecture: string | null;
  tunnel_ip: string;
  wg_public_key: string | null;
  pairing_status: "pending" | "paired" | "revoked";
  pairing_expires_at: string | null;
  paired_at: string | null;
  status: DeviceStatus;
  last_seen_at: string | null;
  last_handshake_at: string | null;
  uptime: string | null;
  tags: string[];
  notes: string | null;
  facts: Record<string, unknown>;
  created_at: string;
}

export interface PairingInfo {
  token: string;
  expires_at: string;
  command: string;
  script_url: string;
}

export interface AuditEntry {
  id: string;
  tenant_id: string | null;
  user_email: string | null;
  action: string;
  target_type: string | null;
  target_id: string | null;
  details: Record<string, unknown>;
  ip_address: string | null;
  success: boolean;
  created_at: string;
}

export interface LiveEvent {
  type: string;
  tenant_id: string | null;
  data: Record<string, unknown>;
}
