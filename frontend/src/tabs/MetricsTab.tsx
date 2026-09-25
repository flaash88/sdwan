import { useEffect, useMemo, useState } from "react";
import { LineChart, type Marker, type Series } from "../components/Chart";
import { Button, Card, ErrorBox, Notice, Segment, StatusBadge, Table } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtAgo, fmtBps, fmtDate } from "../lib/format";
import { ifaceLabel } from "../lib/interfaces";
import { useLive } from "../lib/live";
import type { Device } from "../lib/types";
import { useFetch } from "../lib/useFetch";
import type { EventsResponse } from "../pages/DeviceDetail";

interface Live {
  id: string;
  cpu_load: number | null;
  mgmt_rtt_ms: number | null;
  rx_bps: number;
  tx_bps: number;
  interfaces: Record<string, { rx_bps: number | null; tx_bps: number | null; running: boolean; comment?: string | null; default_name?: string | null }>;
}
type Row = { time: number; [k: string]: number | string };
type Range = "15m" | "1h" | "6h" | "24h" | "7d" | "30d";
const RANGES: { value: Range; label: string; s: number }[] = [
  { value: "15m", label: "15 min", s: 900 }, { value: "1h", label: "1 h", s: 3600 }, { value: "6h", label: "6 h", s: 21600 },
  { value: "24h", label: "24 h", s: 86400 }, { value: "7d", label: "7 T", s: 604800 }, { value: "30d", label: "30 T", s: 2592000 },
];
interface WanCfg { mode: string; links: { slot: number; name: string; interface: string; priority: number; enabled: boolean }[] }

const mbps = (v: number) => (v / 1e6).toLocaleString("de-DE", { maximumFractionDigits: v < 1e7 ? 1 : 0 });

/** Summe je Zeitpunkt über die gewählten Interfaces. */
function sumBy(rows: Row[], field: string, keep: (iface: string) => boolean) {
  const m = new Map<number, number>();
  for (const r of rows) {
    if (typeof r[field] !== "number" || !keep(String(r.interface))) continue;
    const t = Math.round(r.time);
    m.set(t, (m.get(t) ?? 0) + (r[field] as number));
  }
  return [...m.entries()].sort((a, b) => a[0] - b[0]).map(([t, v]) => ({ t, v }));
}

/** Backup-Phasen (Backup-WAN aktiv oder VRRP-Master) aus dem Statusverlauf. */
function backupMarkers(ev: EventsResponse | null, wan: WanCfg | null, from: number, to: number): Marker[] {
  if (!ev) return [];
  const best = Math.min(...(wan?.links.filter((l) => l.enabled).map((l) => l.priority) ?? [1]));
  const backupSlots = new Set((wan?.mode === "failover" ? wan.links.filter((l) => l.priority > best) : []).map((l) => `WAN${l.slot}`));
  const relevant = (subject: string, label: string) => subject.startsWith("vrrp:") || (subject.startsWith("wanactive:") && [...backupSlots].some((s) => label.startsWith(s + " ")));
  const on = (subject: string, status: string) => (subject.startsWith("vrrp:") ? status === "master" : status === "active");
  const subjects = new Map<string, { label: string; spans: [number, number | null][]; open: number | null }>();
  for (const [s, i] of Object.entries(ev.initial)) if (relevant(s, i.label)) subjects.set(s, { label: i.label, spans: [], open: on(s, i.status) ? from : null });
  for (const e of [...ev.events].reverse()) {
    if (!relevant(e.subject, e.label)) continue;
    const x = subjects.get(e.subject) ?? { label: e.label, spans: [], open: null };
    const t = new Date(e.at).getTime() / 1000;
    if (on(e.subject, e.status) && x.open == null) x.open = t;
    if (!on(e.subject, e.status) && x.open != null) { x.spans.push([x.open, t]); x.open = null; }
    subjects.set(e.subject, x);
  }
  const out: Marker[] = [];
  for (const [s, x] of subjects) {
    if (x.open != null) x.spans.push([x.open, null]);
    for (const [a, b] of x.spans) if ((b ?? to) >= from) out.push({ t: a, until: b ?? to, label: s.startsWith("vrrp:") ? `VRRP ${x.label}: Master` : `Failover auf ${x.label}`, tone: "orange" });
  }
  return out;
}

export default function MetricsTab({ device }: { device: Device }) {
  const { me } = useAuth();
  const [range, setRange] = useState<Range>("24h");
  const [live, setLive] = useState<Live | null>(null);
  const span = RANGES.find((r) => r.value === range)!.s;
  const days = Math.max(1, Math.ceil(span / 86400));
  const sys = useFetch<{ points: Row[] }>(`/devices/${device.id}/metrics?measurement=system&range=${range}`);
  const ifc = useFetch<{ points: Row[] }>(`/devices/${device.id}/metrics?measurement=interface&range=${range}`);
  const wanM = useFetch<{ points: Row[] }>(`/devices/${device.id}/metrics?measurement=wan&range=${range}`);
  const wan = useFetch<WanCfg>(`/devices/${device.id}/wan`);
  const events = useFetch<EventsResponse>(`/devices/${device.id}/events?days=${days + 1}&limit=2000`);
  const online = device.status === "online";

  // Live-Modus (schnelles Polling) anfordern und alle 60 s erneuern
  useEffect(() => {
    let stop = false;
    const req = () => api.post<{ snapshot: Live | null }>(`/devices/${device.id}/metrics/live`).then((r) => !stop && setLive((cur) => (r.snapshot ? cur ?? r.snapshot : null))).catch(() => undefined);
    void req();
    const t = setInterval(req, 60000);
    return () => { stop = true; clearInterval(t); };
  }, [device.id]);
  useEffect(() => { if (!online) setLive(null); }, [online]);
  useLive((e) => { const d = e.data as unknown as Live; if (d.id === device.id && online) setLive(d); }, ["device.metrics"]);
  useLive((e) => { if ((e.data as { device_id?: string }).device_id === device.id) void events.reload(); }, ["wan.link", "vrrp.state"]);

  const now = Date.now() / 1000;
  const xr: [number, number] = [now - span, now];
  const wanIfaces = new Set(wan.data?.links.map((l) => l.interface) ?? []);
  const keep = (n: string) => (wanIfaces.size ? wanIfaces.has(n) : !n.startsWith("sdwan-") && !n.startsWith("bridge") && n !== "lo");
  const ifcRows = ifc.data?.points ?? [];
  const thr: Series[] = [
    { name: "Empfangen", color: "var(--blue)", fill: true, points: sumBy(ifcRows, "rx_bps", keep) },
    { name: "Senden", color: "var(--c2)", points: sumBy(ifcRows, "tx_bps", keep) },
  ];
  const slotName = (tag: string) => wan.data?.links.find((l) => `WAN${l.slot}` === tag)?.name ?? tag;
  const latency: Series[] = useMemo(() => {
    const g = new Map<string, { t: number; v: number }[]>();
    for (const r of wanM.data?.points ?? []) if (typeof r.rtt_ms === "number") { const k = String(r.wan); g.set(k, [...(g.get(k) ?? []), { t: r.time, v: r.rtt_ms }]); }
    return [...g.entries()].sort().map(([k, pts], i) => ({ name: slotName(k), color: ["var(--blue)", "var(--c2)", "var(--orange)", "var(--red)"][i % 4], points: pts }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wanM.data, wan.data]);
  const cpu: Series[] = [{ name: "Auslastung", color: "var(--blue)", fill: true, points: (sys.data?.points ?? []).filter((r) => typeof r.cpu_load === "number").map((r) => ({ t: r.time, v: r.cpu_load as number })) }];
  const markers = backupMarkers(events.data, wan.data, xr[0], xr[1]);
  const lastMarker = markers.reduce<Marker | null>((a, m) => (!a || m.t > a.t ? m : a), null);
  const cores = Number(device.facts?.cpu_count ?? 0);

  return (
    <>
      {!online && device.pairing_status === "paired" && (
        <Notice tone="red" title="Gerät nicht erreichbar">Keine Live-Werte – zuletzt gesehen {fmtAgo(device.last_seen_at)}{device.last_seen_at ? ` (${fmtDate(device.last_seen_at)})` : ""}. Der Verlauf zeigt die Daten bis dahin.</Notice>
      )}
      <div className="flex flex-wrap items-center gap-3">
        <Segment label="Zeitraum" value={range} onChange={setRange} options={RANGES.map((r) => ({ value: r.value, label: r.label }))} />
        <span className="text-xs text-fg3">{lastMarker ? `Letzter Backup-Betrieb ab ${new Date(lastMarker.t * 1000).toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" })} markiert` : "Kein Backup-Betrieb im Zeitraum"}</span>
        <div className="flex-1" />
        {markers.length > 0 && <span className="flex items-center gap-1.5 text-xs text-orange-text"><span className="w-3.5 border-t-[1.5px] border-dashed border-orange" />Backup-Betrieb (Failover/VRRP-Master)</span>}
        {me?.user.is_superuser && <Button size="sm" variant="secondary" icon="external" onClick={() => void api.get<{ url: string }>(`/devices/${device.id}/metrics/grafana`).then((r) => window.open(r.url, "_blank", "noopener"))}>Grafana</Button>}
      </div>
      <ErrorBox error={sys.error ?? ifc.error} />
      <Card title="Durchsatz" subtitle={`Mbit/s · ${wanIfaces.size ? "alle WAN-Leitungen" : "alle Interfaces"}`}>
        <LineChart series={thr} height={190} yMin={0} format={mbps} markers={markers} range={xr} />
      </Card>
      <div className="grid gap-4 xl:grid-cols-2">
        <Card title="Latenz" subtitle={`ms · Netwatch-Prüfziel je Leitung`}>
          {latency.length || wan.data?.links.length ? <LineChart series={latency} yMin={0} format={(v) => v.toFixed(0)} markers={markers} range={xr} /> : <p className="py-10 text-center text-fg3">Keine WAN-Leitungen mit Prüfung</p>}
        </Card>
        <Card title="CPU" subtitle={`%${cores ? ` · ${cores} ${cores === 1 ? "Kern" : "Kerne"}` : ""}`}>
          <LineChart series={cpu} yMin={0} yMax={100} format={(v) => v.toFixed(0)} markers={markers} range={xr} />
        </Card>
      </div>
      {online && live && (
        <Card title="Interfaces" subtitle="live" flush>
          <Table head={["Status", "Interface", "Empfangen", "Senden"]}>
            {Object.entries(live.interfaces).sort(([a], [b]) => a.localeCompare(b)).map(([n, i]) => (
              <tr key={n}>
                <td className="px-3 py-2"><StatusBadge status={i.running ? "up" : "down"} label={i.running ? "Up" : "Kein Link"} /></td>
                <td className="px-3 py-2"><span className="font-mono text-xs">{n}</span><span className="ml-2 text-xs text-fg3">{ifaceLabel(n, i).slice(n.length).replace(/^ – /, "")}</span></td>
                <td className="px-3 py-2">{fmtBps(i.rx_bps)}</td>
                <td className="px-3 py-2">{fmtBps(i.tx_bps)}</td>
              </tr>
            ))}
          </Table>
        </Card>
      )}
    </>
  );
}
