import type { Tone } from "../components/ui";
import { useFetch } from "./useFetch";

export interface LocalAccess {
  id: string; device_id: string; enabled: boolean; status: "pending" | "active" | "not_created" | "error" | "disabled"; reason: string | null;
  username: string; has_password: boolean; password_set_at: string | null; viewed_at: string | null; rotate_due_at: string | null;
  networks: string[]; interfaces: string[]; manual_networks: string[]; service_port: { enabled?: boolean; interface?: string; network?: string }; applied_at: string | null;
}
export interface LocalAccessRow { device_id: string; device: string; device_status: string; access: LocalAccess | null }

export const LA_STATUS: Record<string, [string, Tone]> = {
  active: ["aktiv", "green"], pending: ["ausstehend", "blue"], not_created: ["nicht angelegt", "orange"], error: ["Fehler", "red"],
  disabled: ["deaktiviert", "gray"], none: ["nicht angelegt", "gray"],
};

/** Vor-Ort-Zugang aller Geräte des Mandanten (Phase 24), je Geräte-ID. */
export function useLocalAccessMap() {
  const q = useFetch<LocalAccessRow[]>("/local-access");
  const map: Record<string, LocalAccess | null> = {};
  for (const r of q.data ?? []) map[r.device_id] = r.access;
  return { ...q, map };
}
