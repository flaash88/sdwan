import { useFetch } from "./useFetch";

export interface Iface {
  name: string;
  type: string | null;
  comment: string | null;
  default_name: string | null;
  running: boolean;
  disabled?: boolean;
}

/** Anzeigename: "WAN-Glasfaser (ether1) – Kommentar" – Alias/Kommentar aus RouterOS. */
export function ifaceLabel(name: string, i?: Partial<Iface> | null): string {
  if (!i) return name;
  let l = name;
  if (i.default_name && i.default_name !== name) l += ` (${i.default_name})`;
  if (i.comment) l += ` – ${i.comment}`;
  return l;
}

export function useInterfaces(deviceId: string | null) {
  return useFetch<Iface[]>(deviceId ? `/devices/${deviceId}/interfaces` : null);
}

/** Lesbare Namen für Metrik-Felder. */
export const METRIC_LABELS: Record<string, string> = {
  cpu_load: "CPU-Auslastung",
  mem_used: "Speicher belegt",
  mem_total: "Speicher gesamt",
  uptime_s: "Laufzeit",
  mgmt_rtt_ms: "Latenz zur Cloud",
  rx_bps: "Download",
  tx_bps: "Upload",
  rtt_ms: "Latenz",
  loss_pct: "Paketverlust",
};
