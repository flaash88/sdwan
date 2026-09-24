import { useState } from "react";

export interface Series {
  name: string;
  color: string;
  points: { t: number; v: number }[];
}

const PALETTE = ["#0d9488", "#6366f1", "#f59e0b", "#ef4444", "#0ea5e9", "#a855f7", "#84cc16", "#ec4899"];
export const color = (i: number) => PALETTE[i % PALETTE.length];

/** Schlanker SVG-Linienchart ohne externe Abhängigkeiten. */
export function LineChart({ series, height = 180, format = (v: number) => v.toFixed(1), yMin }: { series: Series[]; height?: number; format?: (v: number) => string; yMin?: number }) {
  const [hover, setHover] = useState<number | null>(null);
  const W = 600, H = height, P = { l: 56, r: 8, t: 8, b: 20 };
  const all = series.flatMap((s) => s.points);
  if (all.length < 2) return <div className="flex items-center justify-center text-sm text-slate-400" style={{ height }}>Noch keine Daten</div>;
  const tMin = Math.min(...all.map((p) => p.t)), tMax = Math.max(...all.map((p) => p.t));
  const vMin = yMin ?? Math.min(0, ...all.map((p) => p.v)), vMax = Math.max(...all.map((p) => p.v), vMin + 1e-9);
  const x = (t: number) => P.l + ((t - tMin) / Math.max(tMax - tMin, 1)) * (W - P.l - P.r);
  const y = (v: number) => P.t + (1 - (v - vMin) / (vMax - vMin)) * (H - P.t - P.b);
  const ticks = [0, 0.5, 1].map((f) => vMin + f * (vMax - vMin));
  const fmtT = (t: number) => new Date(t * 1000).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
  const hoverT = hover != null ? tMin + ((hover - P.l) / (W - P.l - P.r)) * (tMax - tMin) : null;
  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" onMouseMove={(e) => { const r = (e.currentTarget as SVGSVGElement).getBoundingClientRect(); setHover(((e.clientX - r.left) / r.width) * W); }} onMouseLeave={() => setHover(null)}>
        {ticks.map((v, i) => (
          <g key={i}>
            <line x1={P.l} x2={W - P.r} y1={y(v)} y2={y(v)} stroke="#e2e8f0" />
            <text x={P.l - 4} y={y(v) + 3} textAnchor="end" fontSize="9" fill="#94a3b8">{format(v)}</text>
          </g>
        ))}
        <text x={P.l} y={H - 4} fontSize="9" fill="#94a3b8">{fmtT(tMin)}</text>
        <text x={W - P.r} y={H - 4} fontSize="9" fill="#94a3b8" textAnchor="end">{fmtT(tMax)}</text>
        {series.map((s) => (
          <polyline key={s.name} fill="none" stroke={s.color} strokeWidth={1.8} points={s.points.map((p) => `${x(p.t)},${y(p.v)}`).join(" ")} />
        ))}
        {hover != null && hover > P.l && <line x1={hover} x2={hover} y1={P.t} y2={H - P.b} stroke="#94a3b8" strokeDasharray="3 3" />}
      </svg>
      <div className="mt-1 flex flex-wrap gap-3 text-xs text-slate-600">
        {series.map((s) => {
          const near = hoverT != null ? s.points.reduce((a, b) => (Math.abs(b.t - hoverT) < Math.abs(a.t - hoverT) ? b : a)) : s.points[s.points.length - 1];
          return (
            <span key={s.name} className="flex items-center gap-1">
              <span className="inline-block h-2 w-2 rounded-full" style={{ background: s.color }} />
              {s.name}: <b>{near ? format(near.v) : "–"}</b>
            </span>
          );
        })}
      </div>
    </div>
  );
}

export function Sparkline({ values, color: c = "#0d9488", height = 32 }: { values: number[]; color?: string; height?: number }) {
  if (values.length < 2) return <div style={{ height }} />;
  const W = 120, max = Math.max(...values, 1e-9), min = Math.min(...values, 0);
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * W},${height - ((v - min) / (max - min || 1)) * (height - 2) - 1}`).join(" ");
  return (
    <svg viewBox={`0 0 ${W} ${height}`} className="w-full" preserveAspectRatio="none" style={{ height }}>
      <polyline fill="none" stroke={c} strokeWidth={1.5} points={pts} />
    </svg>
  );
}
