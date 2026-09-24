import type { ComponentType } from "react";
import { useRef, useState } from "react";
import { Sparkline } from "./components/Chart";
import { Link } from "react-router-dom";
import { Badge, Card, Stat } from "./components/ui";
import type { AlertItem } from "./pages/Alerts";
import { sevColor } from "./pages/Alerts";
import { useFetch } from "./lib/useFetch";
import { fmtBps } from "./lib/format";
import { useLive } from "./lib/live";

/** Live-Kacheln: Gesamtdurchsatz und Ø-CPU der Flotte (aus device.metrics-Events). */
function FleetLive() {
  const ref = useRef<Record<string, { rx: number; tx: number; cpu: number }>>({});
  const [devs, setDevs] = useState<Record<string, { rx: number; tx: number; cpu: number }>>({});
  const [hist, setHist] = useState<number[]>([]);
  useLive((e) => {
    const d = e.data as { id: string; rx_bps: number; tx_bps: number; cpu_load: number | null };
    ref.current = { ...ref.current, [d.id]: { rx: d.rx_bps ?? 0, tx: d.tx_bps ?? 0, cpu: d.cpu_load ?? 0 } };
    const total = Object.values(ref.current).reduce((a, b) => a + b.rx + b.tx, 0);
    setDevs(ref.current);
    setHist((h) => [...h.slice(-59), total]);
  }, ["device.metrics"]);
  const v = Object.values(devs);
  const rx = v.reduce((a, b) => a + b.rx, 0), tx = v.reduce((a, b) => a + b.tx, 0);
  const cpu = v.length ? v.reduce((a, b) => a + b.cpu, 0) / v.length : null;
  return (
    <div className="mb-6 grid grid-cols-2 gap-4 md:grid-cols-4">
      <Stat label="Download gesamt (live)" value={v.length ? fmtBps(rx) : "–"} />
      <Stat label="Upload gesamt (live)" value={v.length ? fmtBps(tx) : "–"} />
      <Stat label="Ø CPU (live)" value={cpu != null ? `${cpu.toFixed(0)}%` : "–"} />
      <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
        <div className="text-xs font-medium uppercase tracking-wide text-slate-500">Durchsatz-Verlauf</div>
        <Sparkline values={hist} height={40} />
      </div>
    </div>
  );
}

/** Aktive Alarme (live aktualisiert). */
function ActiveAlerts() {
  const alerts = useFetch<AlertItem[]>("/alerts?state=open");
  useLive(() => void alerts.reload(), ["alert.firing", "alert.resolved"]);
  if (!alerts.data?.length) return null;
  return (
    <Card title={<Link to="/alerts" className="hover:underline">Aktive Alarme ({alerts.data.length})</Link>} className="mb-6 border-red-200">
      <ul className="space-y-1 text-sm">
        {alerts.data.slice(0, 8).map((a) => (
          <li key={a.id} className="flex items-center gap-2"><Badge color={sevColor(a.severity)}>{a.severity}</Badge>{a.message}</li>
        ))}
      </ul>
    </Card>
  );
}

/** Zusätzliche Dashboard-Kacheln späterer Phasen. */
export const dashboardWidgets: ComponentType[] = [ActiveAlerts, FleetLive];
