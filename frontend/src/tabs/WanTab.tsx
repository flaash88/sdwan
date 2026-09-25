import { useEffect, useState } from "react";
import { Badge, Button, Card, Checkbox, ErrorBox, Input, Select, StatusBadge, Table, useAction } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtAgo } from "../lib/format";
import { useLive } from "../lib/live";
import { useMeta } from "../lib/meta";
import type { Device } from "../lib/types";
import { useFetch } from "../lib/useFetch";
import { ifaceLabel, useInterfaces } from "../lib/interfaces";

interface WanLink {
  id?: string;
  slot?: number;
  name: string;
  interface: string;
  gateway: string;
  resolved_gateway?: string | null;
  priority: number;
  weight: number;
  check_type: "ping" | "http";
  check_target: string;
  check_interval_s: number;
  check_timeout_ms: number;
  loss_threshold_pct: number;
  latency_threshold_ms: number | null;
  enabled: boolean;
  status?: string;
  active?: boolean;
  last_latency_ms?: number | null;
  last_loss_pct?: number | null;
  last_change_at?: string | null;
}
interface WanConfig {
  mode: string;
  recovery_delay_s: number;
  flush_connections: boolean;
  last_apply: string | null;
  last_error: string | null;
  links: WanLink[];
}

const TARGETS = ["1.1.1.1", "9.9.9.9", "8.8.4.4", "208.67.222.222"];
const blank = (i: number): WanLink => ({ name: `WAN${i + 1}`, interface: "", gateway: "dhcp", priority: i + 1, weight: 1, check_type: "ping", check_target: TARGETS[i] ?? "", check_interval_s: 10, check_timeout_ms: 1000, loss_threshold_pct: 50, latency_threshold_ms: null, enabled: true });

export default function WanTab({ device }: { device: Device }) {
  const { can } = useAuth();
  const meta = useMeta();
  const wan = useFetch<WanConfig>(`/devices/${device.id}/wan`);
  const ifaces = useInterfaces(device.id);
  const ifLabel = (n: string) => ifaceLabel(n, ifaces.data?.find((x) => x.name === n));
  const [draft, setDraft] = useState<WanConfig | null>(null);
  const [testResult, setTestResult] = useState<Record<number, string>>({});
  const { busy, error, run } = useAction();
  useEffect(() => { if (wan.data && !draft) setDraft(structuredClone(wan.data)); }, [wan.data, draft]);
  useLive((e) => { if ((e.data as { device_id?: string }).device_id === device.id) void wan.reload(); }, ["wan.link"]);
  if (!draft || !wan.data) return <ErrorBox error={wan.error} />;
  const setLink = (i: number, patch: Partial<WanLink>) => setDraft({ ...draft, links: draft.links.map((l, j) => (j === i ? { ...l, ...patch } : l)) });
  const editable = can("technician");

  return (
    <div className="space-y-6">
      <Card title="Status der WAN-Verbindungen" actions={editable && <Button variant="secondary" disabled={busy} onClick={() => void run(async () => { await api.post(`/devices/${device.id}/wan/apply`); await wan.reload(); })}>Erneut anwenden</Button>}>
        <ErrorBox error={wan.data.last_error} />
        <Table head={["WAN", "Interface", "Gateway", "Status", "Aktiv", "Latenz", "Verlust", "Letzter Wechsel", ""]} empty={wan.data.links.length === 0}>
          {wan.data.links.map((l) => (
            <tr key={l.id}>
              <td className="px-3 py-2 font-medium">{l.slot}. {l.name}</td>
              <td className="px-3 py-2 text-xs"><span className="font-mono">{l.interface}</span>{ifLabel(l.interface) !== l.interface && <div className="text-slate-500">{ifLabel(l.interface).slice(l.interface.length).replace(/^ – /, "")}</div>}</td>
              <td className="px-3 py-2 font-mono text-xs">{l.gateway}{l.resolved_gateway ? ` → ${l.resolved_gateway}` : ""}</td>
              <td className="px-3 py-2"><StatusBadge status={l.status ?? "unknown"} /></td>
              <td className="px-3 py-2">{l.active ? <Badge color="green">trägt Traffic</Badge> : wan.data!.mode !== "failover" && l.status === "up" ? <Badge color="blue">LB</Badge> : "–"}</td>
              <td className="px-3 py-2">{l.last_latency_ms != null ? `${l.last_latency_ms.toFixed(1)} ms` : "–"}</td>
              <td className="px-3 py-2">{l.last_loss_pct != null ? `${l.last_loss_pct}%` : "–"}</td>
              <td className="px-3 py-2 text-slate-500">{fmtAgo(l.last_change_at)}</td>
              <td className="whitespace-nowrap px-3 py-2 text-right">
                {editable && <Button variant="ghost" onClick={() => void run(async () => { const r = await api.post<{ avg_ms: number; loss_pct: number }>(`/devices/${device.id}/wan/${l.slot}/test`); setTestResult({ ...testResult, [l.slot!]: `${r.avg_ms ?? "–"} ms · ${r.loss_pct}% Verlust` }); })}>Testen</Button>}
                {editable && meta?.simulator && <Button variant="ghost" onClick={() => void run(async () => { await api.post(`/devices/${device.id}/wan/${l.slot}/simulate-outage?down=${l.status !== "down"}`); })}>{l.status === "down" ? "Wiederherstellen" : "Ausfall simulieren"}</Button>}
                {testResult[l.slot!] && <div className="text-xs text-slate-500">{testResult[l.slot!]}</div>}
              </td>
            </tr>
          ))}
        </Table>
        <p className="mt-3 text-xs text-slate-500">Health-Checks laufen direkt auf dem Router (Netwatch) – das Umschalten funktioniert auch ohne Verbindung zur Cloud. Status-Aktualisierung mit jedem Polling-Zyklus.</p>
      </Card>

      {editable && (
        <Card title="Konfiguration">
          <ErrorBox error={error} />
          <div className="mb-4 grid gap-4 md:grid-cols-3">
            <Select label="Modus" value={draft.mode} onChange={(e) => setDraft({ ...draft, mode: e.target.value })}>
              <option value="failover">Failover (nach Priorität)</option>
              <option value="loadbalance_pcc">Lastverteilung PCC (gewichtet)</option>
              <option value="loadbalance_ecmp">Lastverteilung ECMP (gleichmäßig)</option>
            </Select>
            <Input label="Recovery-Verzögerung (s) – Link muss so lange stabil sein" type="number" min={0} value={draft.recovery_delay_s} onChange={(e) => setDraft({ ...draft, recovery_delay_s: Number(e.target.value) })} />
            <div className="pt-6"><Checkbox label="Verbindungen bei Ausfall neu verteilen (PCC)" checked={draft.flush_connections} onChange={(v) => setDraft({ ...draft, flush_connections: v })} /></div>
          </div>
          <div className="space-y-4">
            {draft.links.map((l, i) => (
              <div key={i} className="rounded-lg border border-slate-200 p-4">
                <div className="mb-3 flex items-center justify-between">
                  <span className="font-medium">WAN-Link {i + 1}</span>
                  <div className="flex items-center gap-3">
                    <Checkbox label="aktiv" checked={l.enabled} onChange={(v) => setLink(i, { enabled: v })} />
                    <Button variant="ghost" onClick={() => setDraft({ ...draft, links: draft.links.filter((_, j) => j !== i) })}>Entfernen</Button>
                  </div>
                </div>
                <div className="grid gap-3 md:grid-cols-4">
                  <Input label="Name" value={l.name} onChange={(e) => setLink(i, { name: e.target.value })} />
                  <Select label="Interface" value={l.interface} onChange={(e) => setLink(i, { interface: e.target.value })}>
                    {!ifaces.data?.some((x) => x.name === l.interface) && <option value={l.interface}>{l.interface || "– wählen –"}</option>}
                    {ifaces.data?.map((x) => (
                      <option key={x.name} value={x.name}>{ifaceLabel(x.name, x)}{x.running ? "" : " (kein Link)"}{x.type && x.type !== "ether" ? ` [${x.type}]` : ""}</option>
                    ))}
                  </Select>
                  <Input label="Gateway (IP, Interface oder dhcp)" value={l.gateway} onChange={(e) => setLink(i, { gateway: e.target.value })} />
                  <Input label="Check-Ziel (IP, pro WAN eindeutig)" value={l.check_target} onChange={(e) => setLink(i, { check_target: e.target.value })} />
                  <Select label="Check-Typ" value={l.check_type} onChange={(e) => setLink(i, { check_type: e.target.value as "ping" | "http" })}>
                    <option value="ping">Ping (ICMP)</option>
                    <option value="http">HTTP-GET</option>
                  </Select>
                  <Input label={draft.mode === "failover" ? "Priorität (1 = bevorzugt)" : "Priorität (Fallback)"} type="number" min={1} max={4} value={l.priority} onChange={(e) => setLink(i, { priority: Number(e.target.value) })} />
                  <Input label="Gewichtung (PCC)" type="number" min={1} max={10} value={l.weight} disabled={draft.mode !== "loadbalance_pcc"} onChange={(e) => setLink(i, { weight: Number(e.target.value) })} />
                  <Input label="Intervall (s)" type="number" min={1} value={l.check_interval_s} onChange={(e) => setLink(i, { check_interval_s: Number(e.target.value) })} />
                  <Input label="Verlust-Schwelle (%)" type="number" min={1} max={100} value={l.loss_threshold_pct} onChange={(e) => setLink(i, { loss_threshold_pct: Number(e.target.value) })} />
                  <Input label="Latenz-Schwelle (ms, optional)" type="number" value={l.latency_threshold_ms ?? ""} onChange={(e) => setLink(i, { latency_threshold_ms: e.target.value ? Number(e.target.value) : null })} />
                </div>
              </div>
            ))}
          </div>
          <div className="mt-4 flex justify-between">
            <Button variant="secondary" disabled={draft.links.length >= 4} onClick={() => setDraft({ ...draft, links: [...draft.links, blank(draft.links.length)] })}>+ WAN-Link ({draft.links.length}/4)</Button>
            <Button disabled={busy} onClick={() => void run(async () => {
              const r = await api.put<WanConfig & { push: { ok: boolean; error?: string } | null }>(`/devices/${device.id}/wan`, { ...draft, push: true });
              setDraft(structuredClone(r));
              await wan.reload();
              if (r.push && !r.push.ok) throw new Error(`Gespeichert, aber Push fehlgeschlagen: ${r.push.error}`);
            })}>Speichern & auf Router anwenden</Button>
          </div>
        </Card>
      )}
    </div>
  );
}
