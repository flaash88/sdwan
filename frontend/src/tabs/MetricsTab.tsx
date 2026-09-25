import { useEffect, useMemo, useRef, useState } from "react";
import { color, LineChart, Sparkline, type Series } from "../components/Chart";
import { Button, Card, ErrorBox, Select, StatusDot, Table } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtAgo, fmtBps, fmtBytes, fmtDate } from "../lib/format";
import { useLive } from "../lib/live";
import type { Device } from "../lib/types";
import { useFetch } from "../lib/useFetch";
import { ifaceLabel, METRIC_LABELS } from "../lib/interfaces";

interface Live {
  id: string;
  cpu_load: number | null;
  mem_used: number | null;
  mem_total: number | null;
  mgmt_rtt_ms: number | null;
  uptime: string | null;
  rx_bps: number;
  tx_bps: number;
  interfaces: Record<string, { rx_bps: number | null; tx_bps: number | null; running: boolean; comment?: string | null; default_name?: string | null }>;
  wan: Record<string, { status: string; rtt_ms: number | null; loss_pct: number | null; active?: boolean }>;
}
type Row = { time: number; [k: string]: number | string };

function Tile({ label, value, history, c, online = true }: { label: string; value: string; history: number[]; c: string; online?: boolean }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-panel p-4 shadow-sm">
      <div className="flex items-center justify-between text-xs font-medium uppercase tracking-wide text-slate-500">
        {label}
        {online ? <span className="flex items-center gap-1 normal-case text-emerald-600"><StatusDot status="online" /> live</span> : <span className="normal-case text-red-600">offline</span>}
      </div>
      <div className={`mt-1 text-2xl font-semibold ${online ? "text-slate-900" : "text-slate-300"}`}>{online ? value : "–"}</div>
      <Sparkline values={history} color={c} />
    </div>
  );
}

function groupSeries(rows: Row[], field: string, by?: string, scale = 1, label: (k: string) => string = (k) => k): Series[] {
  const groups = new Map<string, { t: number; v: number }[]>();
  for (const r of rows) {
    if (typeof r[field] !== "number") continue;
    const k = by ? label(String(r[by])) : METRIC_LABELS[field] ?? field;
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k)!.push({ t: r.time, v: (r[field] as number) * scale });
  }
  return [...groups.entries()].map(([name, points], i) => ({ name, points, color: color(i) }));
}

export default function MetricsTab({ device }: { device: Device }) {
  const { me } = useAuth();
  const [live, setLive] = useState<Live | null>(null);
  const hist = useRef<Live[]>([]);
  const [range, setRange] = useState("1h");
  const sys = useFetch<{ points: Row[] }>(`/devices/${device.id}/metrics?measurement=system&range=${range}`);
  const ifc = useFetch<{ points: Row[] }>(`/devices/${device.id}/metrics?measurement=interface&range=${range}`);
  const wan = useFetch<{ points: Row[] }>(`/devices/${device.id}/metrics?measurement=wan&range=${range}`);

  // Live-Modus anfordern und alle 60 s erneuern
  useEffect(() => {
    let stop = false;
    const req = () => api.post<{ snapshot: Live | null }>(`/devices/${device.id}/metrics/live`).then((r) => !stop && setLive((cur) => (r.snapshot ? cur ?? r.snapshot : null))).catch(() => undefined);
    void req();
    const t = setInterval(req, 60000);
    return () => { stop = true; clearInterval(t); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [device.id]);

  const online = device.status === "online";
  useEffect(() => {
    if (!online) {
      setLive(null);
      hist.current = [];
    }
  }, [online]);

  useLive((e) => {
    const d = e.data as unknown as Live;
    if (d.id !== device.id || !online) return;
    hist.current = [...hist.current.slice(-59), d];
    setLive(d);
  }, ["device.metrics"]);

  const h = hist.current;
  const facts = (device.facts?.interfaces ?? {}) as Record<string, { comment?: string | null; default_name?: string | null }>;
  const ifName = (n: string) => ifaceLabel(n, live?.interfaces?.[n] ?? facts[n]);
  const ifaceRows = useMemo(() => Object.entries(live?.interfaces ?? {}).sort(([a], [b]) => a.localeCompare(b)), [live]);
  const grafana = me?.user.is_superuser;

  return (
    <div className="space-y-6">
      {!online && (
        <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800">
          <b>Gerät nicht erreichbar.</b> Keine Live-Werte – zuletzt gesehen {fmtAgo(device.last_seen_at)}{device.last_seen_at ? ` (${fmtDate(device.last_seen_at)})` : ""}. Der Verlauf unten zeigt die Daten bis dahin.
        </div>
      )}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
        <Tile online={online} label="CPU-Auslastung" value={live?.cpu_load != null ? `${live.cpu_load}%` : "–"} history={h.map((x) => x.cpu_load ?? 0)} c={color(0)} />
        <Tile online={online} label="Arbeitsspeicher" value={live?.mem_total ? `${Math.round(((live.mem_used ?? 0) / live.mem_total) * 100)}%` : "–"} history={h.map((x) => (x.mem_total ? (x.mem_used ?? 0) / x.mem_total : 0))} c={color(1)} />
        <Tile online={online} label="Download" value={fmtBps(live?.rx_bps)} history={h.map((x) => x.rx_bps)} c={color(4)} />
        <Tile online={online} label="Upload" value={fmtBps(live?.tx_bps)} history={h.map((x) => x.tx_bps)} c={color(2)} />
        <Tile online={online} label="Latenz Cloud" value={live?.mgmt_rtt_ms != null ? `${live.mgmt_rtt_ms} ms` : "–"} history={h.map((x) => x.mgmt_rtt_ms ?? 0)} c={color(5)} />
      </div>

      <Card title={online ? "Interfaces (live)" : "Interfaces (Gerät offline)"}>
        <Table head={["", "Interface", "RX", "TX"]} empty={ifaceRows.length === 0}>
          {ifaceRows.map(([n, i]) => (
            <tr key={n}>
              <td className="px-3 py-1.5"><StatusDot status={i.running ? "up" : "down"} /></td>
              <td className="px-3 py-1.5"><span className="font-mono text-xs">{n}</span>{(i.comment || (i.default_name && i.default_name !== n)) && <span className="ml-2 text-xs text-slate-500">{ifaceLabel(n, i).slice(n.length).replace(/^ – /, "")}</span>}</td>
              <td className="px-3 py-1.5">{fmtBps(i.rx_bps)}</td>
              <td className="px-3 py-1.5">{fmtBps(i.tx_bps)}</td>
            </tr>
          ))}
        </Table>
      </Card>

      <Card
        title="Verlauf"
        actions={
          <>
            <Select value={range} onChange={(e) => setRange(e.target.value)}>
              {["15m", "1h", "6h", "24h", "7d", "30d"].map((r) => <option key={r}>{r}</option>)}
            </Select>
            {grafana && <Button variant="secondary" onClick={() => void api.get<{ url: string }>(`/devices/${device.id}/metrics/grafana`).then((r) => window.open(r.url, "_blank"))}>Grafana ↗</Button>}
          </>
        }
      >
        <ErrorBox error={sys.error} />
        <div className="grid gap-6 lg:grid-cols-2">
          <div><h4 className="mb-1 text-sm font-medium text-slate-600">CPU-Auslastung (%)</h4><LineChart series={groupSeries(sys.data?.points ?? [], "cpu_load")} yMin={0} /></div>
          <div><h4 className="mb-1 text-sm font-medium text-slate-600">Speicher belegt</h4><LineChart series={groupSeries(sys.data?.points ?? [], "mem_used")} format={fmtBytes} /></div>
          <div><h4 className="mb-1 text-sm font-medium text-slate-600">Download je Interface</h4><LineChart series={groupSeries((ifc.data?.points ?? []).filter((p) => !String(p.interface).startsWith("sdwan-")), "rx_bps", "interface", 1, ifName)} format={fmtBps} /></div>
          <div><h4 className="mb-1 text-sm font-medium text-slate-600">WAN-Latenz (ms)</h4><LineChart series={groupSeries(wan.data?.points ?? [], "rtt_ms", "wan")} /></div>
        </div>
      </Card>
    </div>
  );
}
