import type { DeviceState } from "../lib/fleet";
import type { Device } from "../lib/types";
import { Pill, StatusBadge } from "./ui";

/** Aktiver WAN: Primärleitung neutral, Backup orange, unbekannt grau. */
export function WanPill({ state, offline }: { state?: DeviceState | null; offline?: boolean }) {
  const w = state?.active_wan;
  if (!w || offline) return <Pill tone="gray" icon="minusCircle">{state && state.wan_links === 0 ? "Kein WAN" : "Unbekannt"}</Pill>;
  if (w.backup) return <Pill tone="orange" icon="signal" title={`${w.name} (${w.interface}) – Backup-Leitung aktiv`}>{w.name}</Pill>;
  return <Pill tone="neutral" icon="cable" title={`${w.name} (${w.interface})`}>{w.name}</Pill>;
}

/** VRRP-Rolle: Master = Standort läuft über Backup (orange), Backup neutral. */
export function VrrpPill({ state }: { state?: DeviceState | null }) {
  const r = state?.vrrp_role;
  if (!r) return <span className="text-fg3">–</span>;
  if (r === "master") return <Pill tone="orange" icon="alert">Master</Pill>;
  if (r === "backup") return <Pill tone="neutral" icon="pause">Backup</Pill>;
  if (r === "disabled") return <Pill tone="gray" icon="minusCircle">Deaktiviert</Pill>;
  return <Pill tone="gray" icon="minusCircle">Unbekannt</Pill>;
}

/** Gerätestatus inkl. Pairing (ausstehend/gesperrt). */
export function DeviceStatusBadge({ device }: { device: Device }) {
  if (device.pairing_status !== "paired") return <StatusBadge status={device.pairing_status} label={device.pairing_status === "pending" ? "Nicht verbunden" : undefined} />;
  const rb = rebootInfo(device);
  if (rb) return <Pill tone="blue" icon="loader" title={`Neustart ausgelöst von ${rb.by ?? "System"} – Offline-Alarm bis ${new Date(rb.until).toLocaleTimeString("de-DE")} unterdrückt`}>{rb.reason === "firmware" ? "Neustart läuft (Firmware-Update)" : "Neustart läuft"}</Pill>;
  return <StatusBadge status={device.status} />;
}

export function CpuBar({ value }: { value: number | null | undefined }) {
  if (value == null) return <span className="text-fg3">–</span>;
  const v = Math.max(0, Math.min(100, Number(value)));
  return (
    <span className="flex min-w-[76px] items-center gap-2 whitespace-nowrap" title={`CPU ${v} %`}>
      <span className="h-1 flex-1 overflow-hidden rounded-sm bg-sunken">
        <span className={`block h-full ${v >= 80 ? "bg-orange" : "bg-fg3"}`} style={{ width: `${v}%` }} />
      </span>
      <span className="w-[34px] text-right text-fg2">{v} %</span>
    </span>
  );
}

/** Laufender Neustart (facts.reboot, vom Poller entfernt, sobald das Gerät zurück ist oder 5 min vergangen sind). */
export function rebootInfo(device: Device): { at: string; by?: string | null; reason?: string; until: string } | null {
  const r = (device.facts as { reboot?: { at: string; by?: string | null; reason?: string; until: string } } | undefined)?.reboot;
  return r && r.until ? r : null;
}
