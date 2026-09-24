import type { ComponentType } from "react";
import { useRef, useState } from "react";
import { Sparkline } from "./components/Chart";
import { Stat } from "./components/ui";
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

/** Zusätzliche Dashboard-Kacheln späterer Phasen. */
export const dashboardWidgets: ComponentType[] = [FleetLive];
