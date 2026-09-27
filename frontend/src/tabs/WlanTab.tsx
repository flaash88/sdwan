import { Link } from "react-router-dom";
import { Button, Card, EmptyState, ErrorBox, Loading, Notice, Pill, useAction } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import type { Device } from "../lib/types";
import { useFetch } from "../lib/useFetch";
import { WLAN_STATUS } from "../pages/Wlan";

interface Radio { name: string; bands: string; ssid: string | null; disabled: boolean; managed: boolean; master: string | null }
interface Live { driver: "wifi" | "wireless" | null; radios: Radio[]; capsman: boolean; cap: boolean; clients: { interface: string; mac: string; signal: string | null; tx_rate: string | null; rx_rate: string | null; uptime: string | null; ssid: string | null }[]; channels: Record<string, string | null>; caps: { name: string; address: string; state: string }[] }
interface Data {
  facts: Live | null; live: Live | null; live_error: string | null;
  profiles: { id: string; name: string; ssid: string; mode: string; version: number; status: string; error: string | null; applied_version: number | null; detail: { interfaces?: string[] } }[];
}

/** WLAN des Geräts: Treiber, Radios, Kanäle, Clients, zugewiesene Profile. Nur sichtbar, wenn das Gerät WLAN hat. */
export default function WlanTab({ device }: { device: Device }) {
  const { can } = useAuth();
  const data = useFetch<Data>(`/devices/${device.id}/wlan`);
  const { busy, error, run } = useAction();
  if (!data.data) return data.error ? <ErrorBox error={data.error} deviceId={device.id} /> : <Loading rows={4} />;
  const d = data.data;
  const w = d.live ?? d.facts;
  return (
    <div className="flex flex-col gap-4">
      <ErrorBox error={error ?? d.live_error} deviceId={device.id} />
      {w?.driver === "wireless" && <Notice tone="orange" icon="alert" title="Treiber „wireless“ – nur Anzeige">Dieses Gerät nutzt das ältere WLAN-Paket. Die Plattform zeigt Status und Clients, konfiguriert aber nur den wifi-Treiber.</Notice>}
      <Card title="Radios" subtitle={w ? `Treiber ${w.driver}${w.capsman ? " · CAPsMAN-Controller" : ""}${w.cap ? " · CAP (von CAPsMAN verwaltet)" : ""}${d.live ? "" : " · Stand der letzten Abfrage"}` : undefined}
        actions={can("technician") && w?.driver === "wifi" && <Button size="sm" variant="secondary" icon="upload" disabled={busy} onClick={() => void run(async () => { await api.post(`/devices/${device.id}/wlan/apply`); await data.reload(); })}>Profile abgleichen</Button>}>
        {!w || !w.radios.length ? <EmptyState compact title="Keine Radios" text={w?.capsman ? "Controller ohne eigene Radios – CAPs siehe unten." : undefined} /> : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {w.radios.map((r) => (
              <div key={r.name} className="rounded-md border border-line p-3">
                <div className="flex items-center justify-between"><span className="font-mono font-medium">{r.name}</span>
                  {r.disabled ? <Pill tone="gray">deaktiviert</Pill> : r.managed ? <Pill tone="blue">Plattform</Pill> : r.master ? <Pill tone="gray">virtuell</Pill> : <Pill tone="green">Radio</Pill>}</div>
                <div className="mt-1 text-sm text-fg2">{r.ssid ?? "–"}{r.master && <span className="text-fg3"> · an {r.master}</span>}</div>
                {r.bands && <div className="text-xs text-fg3">{r.bands}</div>}
                {d.live?.channels[r.name] && <div className="text-xs text-fg3">Kanal {d.live.channels[r.name]}</div>}
              </div>
            ))}
          </div>
        )}
        <p className="mt-3 text-xs text-fg3">Nachbarnetze werden nicht angezeigt: ein Scan würde verbundene Clients kurz trennen.</p>
      </Card>
      <Card flush title="Profile" actions={<Link to="/wlan" className="text-sm text-blue-text hover:underline">WLAN-Profile verwalten</Link>}>
        {d.profiles.length === 0 ? <EmptyState compact title="Keine Profile zugewiesen" /> : (
          <table className="w-full text-sm"><tbody>{d.profiles.map((p) => { const [l, t] = WLAN_STATUS[p.status] ?? [p.status, "gray"]; return (
            <tr key={p.id} className="border-b border-line last:border-b-0">
              <td className="px-4 py-2 font-medium">{p.name}</td><td className="px-2 font-mono text-xs">{p.ssid}</td><td className="px-2">{p.mode === "capsman" ? "CAPsMAN" : "lokal"}</td>
              <td className="px-2"><Pill tone={t} title={p.error ?? ""}>{l}</Pill>{p.status === "ok" && p.applied_version !== p.version && <Pill tone="orange">nicht ausgerollt</Pill>}</td>
              <td className="px-2 text-xs text-fg2">{p.error ?? (p.detail.interfaces ?? []).join(", ")}</td>
            </tr>); })}</tbody></table>
        )}
      </Card>
      <Card flush title="Clients" subtitle={d.live ? `${d.live.clients.length} verbunden` : "nur bei erreichbarem Gerät"}>
        {!d.live?.clients.length ? <EmptyState compact title="Keine Clients" /> : (
          <div className="overflow-x-auto"><table className="w-full text-sm">
            <thead><tr className="border-b border-line bg-panel2 text-left text-xs text-fg3"><th className="px-4 py-2 font-medium">MAC</th><th className="px-2 font-medium">Interface</th><th className="px-2 font-medium">SSID</th><th className="px-2 font-medium">Signal</th><th className="px-2 font-medium">TX / RX</th><th className="px-2 font-medium">Verbunden</th></tr></thead>
            <tbody>{d.live.clients.map((c) => (
              <tr key={`${c.interface}-${c.mac}`} className="border-b border-line last:border-b-0">
                <td className="px-4 py-2 font-mono text-xs">{c.mac}</td><td className="px-2 font-mono text-xs">{c.interface}</td><td className="px-2">{c.ssid ?? "–"}</td>
                <td className="px-2">{c.signal ?? "–"}</td><td className="px-2 text-xs">{c.tx_rate ?? "–"} / {c.rx_rate ?? "–"}</td><td className="px-2 text-xs">{c.uptime ?? "–"}</td>
              </tr>))}</tbody>
          </table></div>
        )}
      </Card>
      {w?.capsman && (
        <Card flush title="CAPs am Controller">
          {!d.live?.caps.length ? <EmptyState compact title="Keine CAPs verbunden" text="CAPs werden auf dem jeweiligen Gerät manuell als CAP eingerichtet (siehe Doku)." /> : (
            <table className="w-full text-sm"><tbody>{d.live.caps.map((c) => <tr key={c.name} className="border-b border-line last:border-b-0"><td className="px-4 py-2">{c.name}</td><td className="px-2 font-mono text-xs">{c.address}</td><td className="px-2">{c.state}</td></tr>)}</tbody></table>
          )}
        </Card>
      )}
    </div>
  );
}
