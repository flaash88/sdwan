import { useEffect, useRef, useState } from "react";

/** Breite des Containers (für verzerrungsfreie SVG-Beschriftung). */
function useWidth(initial = 600) {
  const ref = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(initial);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(200, Math.round(e.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w] as const;
}

export interface Series {
  name: string;
  color: string;
  points: { t: number; v: number }[];
  /** Fläche unter der Linie leicht einfärben */
  fill?: boolean;
}

/** Markierung (z. B. Failover) als gestrichelte Linie; ``until`` schattiert den Bereich bis dahin. */
export interface Marker { t: number; until?: number | null; label: string; tone?: "orange" | "green" | "red" | "blue" }

const PALETTE = ["var(--blue)", "var(--c2)", "var(--orange)", "var(--red)", "var(--green)", "#a855f7", "#84cc16", "#ec4899"];
export const color = (i: number) => PALETTE[i % PALETTE.length];

const fmtClock = (t: number) => new Date(t * 1000).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
const fmtDay = (t: number) => new Date(t * 1000).toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit" });

/** Schlanker SVG-Linienchart ohne externe Abhängigkeiten (Design: Raster, 5 Zeitmarken, Failover-Markierungen). */
export function LineChart({ series, height = 180, format = (v: number) => v.toFixed(1), yMin, yMax, markers = [], range }: {
  series: Series[]; height?: number; format?: (v: number) => string; yMin?: number; yMax?: number; markers?: Marker[];
  /** fester Zeitbereich [Start, Ende] in Sekunden */ range?: [number, number];
}) {
  const [hover, setHover] = useState<number | null>(null);
  const [box, W] = useWidth();
  const H = height, P = { l: 48, r: 8, t: 8, b: 22 };
  const all = series.flatMap((s) => s.points);
  if (all.length < 2) return <div ref={box} className="flex items-center justify-center text-fg3" style={{ height }}>Noch keine Daten für diesen Zeitraum</div>;
  const tMin = range?.[0] ?? Math.min(...all.map((p) => p.t)), tMax = range?.[1] ?? Math.max(...all.map((p) => p.t));
  const vMin = yMin ?? Math.min(0, ...all.map((p) => p.v));
  const vMax = yMax ?? Math.max(...all.map((p) => p.v), vMin + 1e-9) * 1.08;
  const x = (t: number) => P.l + ((t - tMin) / Math.max(tMax - tMin, 1)) * (W - P.l - P.r);
  const y = (v: number) => P.t + (1 - (Math.min(v, vMax) - vMin) / (vMax - vMin || 1)) * (H - P.t - P.b);
  const grid = [0, 0.25, 0.5, 0.75, 1];
  const span = tMax - tMin;
  const fmtT = span > 86400 * 1.5 ? fmtDay : fmtClock;
  const hoverT = hover != null ? tMin + ((hover - P.l) / (W - P.l - P.r)) * (tMax - tMin) : null;
  const tone = (m: Marker) => `var(--${m.tone ?? "orange"})`;
  const path = (pts: { t: number; v: number }[]) => pts.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)} ${y(p.v).toFixed(1)}`).join(" ");
  return (
    <div ref={box}>
      <svg viewBox={`0 0 ${W} ${H}`} className="block w-full" style={{ height }} role="img" aria-label={series.map((s) => s.name).join(", ")}
        onMouseMove={(e) => { const r = (e.currentTarget as SVGSVGElement).getBoundingClientRect(); setHover(((e.clientX - r.left) / r.width) * W); }} onMouseLeave={() => setHover(null)}>
        {grid.map((f) => {
          const v = vMin + (1 - f) * (vMax - vMin);
          return (
            <g key={f}>
              <line x1={P.l} x2={W - P.r} y1={y(v)} y2={y(v)} style={{ stroke: "var(--border)" }} vectorEffect="non-scaling-stroke" />
              <text x={P.l - 6} y={y(v) + 3} textAnchor="end" fontSize="11" style={{ fill: "var(--text3)" }}>{format(v)}</text>
            </g>
          );
        })}
        {grid.map((f) => {
          const t = tMin + f * span;
          return <text key={"x" + f} x={x(t)} y={H - 5} fontSize="11" textAnchor={f === 0 ? "start" : f === 1 ? "end" : "middle"} style={{ fill: "var(--text3)" }}>{fmtT(t)}</text>;
        })}
        {markers.filter((m) => m.t <= tMax && (m.until ?? tMax) >= tMin).map((m, i) => (
          <g key={"m" + i}>
            <rect x={x(Math.max(m.t, tMin))} y={P.t} width={Math.max(x(Math.min(m.until ?? tMax, tMax)) - x(Math.max(m.t, tMin)), 0)} height={H - P.t - P.b} style={{ fill: tone(m), opacity: 0.07 }} />
            {m.t >= tMin && <line x1={x(m.t)} x2={x(m.t)} y1={P.t} y2={H - P.b} style={{ stroke: tone(m) }} strokeWidth={1.5} strokeDasharray="4 3" vectorEffect="non-scaling-stroke"><title>{m.label}</title></line>}
          </g>
        ))}
        {series.map((s) => s.fill && s.points.length > 1 && (
          <path key={s.name + "f"} d={`${path(s.points)} L${x(s.points[s.points.length - 1].t)} ${y(vMin)} L${x(s.points[0].t)} ${y(vMin)} Z`} style={{ fill: s.color, opacity: 0.08 }} />
        ))}
        {series.map((s) => (
          <path key={s.name} d={path(s.points)} fill="none" style={{ stroke: s.color }} strokeWidth={1.6} strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
        ))}
        {hover != null && hover > P.l && <line x1={hover} x2={hover} y1={P.t} y2={H - P.b} style={{ stroke: "var(--text3)" }} strokeDasharray="3 3" vectorEffect="non-scaling-stroke" />}
      </svg>
      <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-xs text-fg2">
        {series.map((s) => {
          const near = hoverT != null && s.points.length ? s.points.reduce((a, b) => (Math.abs(b.t - hoverT) < Math.abs(a.t - hoverT) ? b : a)) : s.points[s.points.length - 1];
          return (
            <span key={s.name} className="flex items-center gap-1.5">
              <span className="inline-block h-0.5 w-3" style={{ background: s.color }} />
              {s.name} <b className="font-semibold text-fg">{near ? format(near.v) : "–"}</b>
            </span>
          );
        })}
        {hoverT != null && <span className="ml-auto text-fg3">{new Date(hoverT * 1000).toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" })}</span>}
      </div>
    </div>
  );
}

export function Sparkline({ values, color: c = "var(--blue)", height = 32 }: { values: number[]; color?: string; height?: number }) {
  if (values.length < 2) return <div style={{ height }} />;
  const W = 120, max = Math.max(...values, 1e-9), min = Math.min(...values, 0);
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * W},${height - ((v - min) / (max - min || 1)) * (height - 2) - 1}`).join(" ");
  return (
    <svg viewBox={`0 0 ${W} ${height}`} className="w-full" preserveAspectRatio="none" style={{ height }} aria-hidden>
      <polyline fill="none" style={{ stroke: c }} strokeWidth={1.5} points={pts} vectorEffect="non-scaling-stroke" />
    </svg>
  );
}
