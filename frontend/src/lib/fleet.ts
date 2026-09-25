import { useEffect } from "react";
import { useLive } from "./live";
import type { Device, Site } from "./types";
import { useFetch } from "./useFetch";

export interface AlertItem {
  id: string; rule_id: string | null; device_id: string | null; device: string | null; subject: string;
  status: "pending" | "firing" | "resolved"; severity: string; message: string; value: number | null;
  started_at: string; fired_at: string | null; resolved_at: string | null; fires_at: string | null;
  notified: boolean; acknowledged_by: string | null; acknowledged_at: string | null;
}

/**
 * Einheitliche Alarm-Zustände (Glocke, Filter, Dashboard):
 * aktiv = ausgelöst, nicht behoben, nicht quittiert · quittiert = ausgelöst, nicht behoben, quittiert.
 */
export type AlarmState = "active" | "ack" | "resolved" | "pending";
export function alarmState(a: AlertItem): AlarmState {
  if (a.status === "resolved") return "resolved";
  if (a.status === "pending") return "pending";
  return a.acknowledged_by ? "ack" : "active";
}

/** Offene Alarme (firing + pending), live aktualisiert. */
export function useOpenAlerts() {
  const f = useFetch<AlertItem[]>("/alerts?state=open");
  useLive(() => void f.reload(), ["alert.firing", "alert.resolved"]);
  useEffect(() => {
    const t = setInterval(() => void f.reload(), 60000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return f;
}

export const activeAlarmCount = (list: AlertItem[] | null) => list?.filter((a) => alarmState(a) === "active").length ?? 0;

export interface FirmwareUpdate { installed: string; latest: string; channel: string; update_available: boolean; checked_at: string }
export const firmwareUpdate = (d: Device) => (d.facts?.update ?? null) as FirmwareUpdate | null;

/** Geräteliste + Standorte, live aktualisiert (Status, Poll-Werte). */
export function useDevices() {
  const devices = useFetch<Device[]>("/devices");
  useLive((e) => {
    if (e.type === "device.poll") {
      const d = e.data as { id: string; status: Device["status"]; uptime: string; last_seen_at: string; cpu_load: number };
      devices.setData((prev) => prev?.map((x) => (x.id === d.id ? { ...x, status: d.status, uptime: d.uptime, last_seen_at: d.last_seen_at, facts: { ...x.facts, cpu_load: d.cpu_load } } : x)) ?? prev);
    } else void devices.reload();
  }, ["device.status", "device.paired", "device.poll"]);
  return devices;
}

export function useSites() {
  return useFetch<Site[]>("/sites");
}

export interface DeviceState {
  wan_mode: string;
  wan_links: number;
  active_wan: { slot: number; name: string; interface: string; backup: boolean; latency_ms: number | null } | null;
  vrrp_role: string | null;
  vrrp: { id: string; name: string; vrid: number; state: string; enabled: boolean }[];
  on_backup: boolean;
  backup_since: string | null;
}
export interface FleetState { devices: Record<string, DeviceState>; sites_on_backup: number; sites_total: number }

/** Aktiver WAN / VRRP-Rolle / Backup-Betrieb je Gerät (live bei WAN- und VRRP-Wechseln). */
export function useFleetState() {
  const f = useFetch<FleetState>("/dashboard/fleet-state");
  useLive(() => void f.reload(), ["wan.link", "vrrp.state", "device.status"]);
  return f;
}

/** Meldung ohne vorangestellten Gerätenamen ("rtr1: WAN … ausgefallen" -> "WAN … ausgefallen"). */
export function alertTitle(a: AlertItem): string {
  const m = a.message;
  if (a.device && m.startsWith(`${a.device}: `)) return m.slice(a.device.length + 2);
  if (a.device && m.startsWith(`${a.device} `)) return m.slice(a.device.length + 1).replace(/^./, (c) => c.toUpperCase());
  return m;
}
