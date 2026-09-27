import { useNavigate } from "react-router-dom";
import { useFetch } from "../lib/useFetch";
import type { Neighbor } from "../tabs/NeighborsTab";
import { Button, EmptyState, ErrorBox, Loading, Modal } from "./ui";

interface Topo { site: { id: string; name: string }; devices: { id: string; name: string; status: string; model: string | null; neighbors: Neighbor[] }[] }

const ROW = 46, BOX_H = 38, LEFT_W = 180, RIGHT_X = 340, RIGHT_W = 240, W = RIGHT_X + RIGHT_W + 10;

/** Standort-Topologie: Router → Nachbarn je Interface (aus /ip/neighbor). Plattform-Geräte sind anklickbar. */
export default function SiteTopology({ siteId, onClose }: { siteId: string; onClose: () => void }) {
  const q = useFetch<Topo>(`/sites/${siteId}/topology`);
  const nav = useNavigate();
  let y = 10;
  const blocks = (q.data?.devices ?? []).map((d) => {
    const n = Math.max(1, d.neighbors.length);
    const top = y;
    y += n * ROW + 24;
    return { d, top, h: n * ROW };
  });
  return (
    <Modal open onClose={onClose} title={`Topologie – ${q.data?.site.name ?? ""}`} subtitle="Router des Standorts und ihre Nachbarn (Discovery, alle 10 min)" size="lg"
      footer={<Button onClick={onClose}>Schließen</Button>}>
      <ErrorBox error={q.error} />
      {!q.data ? <Loading rows={3} /> : q.data.devices.length === 0 ? <EmptyState compact icon="router" title="Keine Router an diesem Standort" /> : (
        <div className="overflow-x-auto">
          <svg width={W} height={y} role="img" aria-label="Topologie" className="text-fg">
            {blocks.map(({ d, top, h }) => {
              const cy = top + h / 2;
              return (
                <g key={d.id}>
                  <g className="cursor-pointer" onClick={() => nav(`/devices/${d.id}`)}>
                    <rect x={10} y={cy - BOX_H / 2} width={LEFT_W} height={BOX_H} rx={7} className="fill-blue-bg stroke-blue" />
                    <circle cx={24} cy={cy} r={4} className={d.status === "online" ? "fill-green" : d.status === "offline" ? "fill-red" : "fill-gray"} />
                    <text x={34} y={cy - 2} className="fill-current text-[12.5px] font-medium">{d.name}</text>
                    <text x={34} y={cy + 12} className="fill-[var(--text3)] text-[11px]">{d.model ?? ""}</text>
                  </g>
                  {d.neighbors.length === 0 && <text x={RIGHT_X} y={cy + 4} className="fill-[var(--text3)] text-[12px]">keine Nachbarn sichtbar</text>}
                  {d.neighbors.map((n, i) => {
                    const ny = top + i * ROW + ROW / 2;
                    const linked = !!n.device;
                    return (
                      <g key={`${n.mac_address}-${i}`} className={linked ? "cursor-pointer" : undefined} onClick={() => n.device && nav(`/devices/${n.device.id}`)}>
                        <path d={`M ${10 + LEFT_W} ${cy} C ${RIGHT_X - 80} ${cy}, ${RIGHT_X - 80} ${ny}, ${RIGHT_X} ${ny}`} fill="none" className="stroke-[var(--border-strong)]" strokeWidth={1.3} />
                        <text x={RIGHT_X - 8} y={ny - 5} textAnchor="end" className="fill-[var(--text3)] font-mono text-[10.5px]">{n.interface ?? ""}</text>
                        <rect x={RIGHT_X} y={ny - 17} width={RIGHT_W} height={34} rx={6} className={linked ? "fill-blue-bg stroke-blue" : "fill-[var(--panel2)] stroke-[var(--border)]"} />
                        <text x={RIGHT_X + 10} y={ny - 2} className="fill-current text-[12px] font-medium">{(n.identity ?? n.mac_address ?? "?").slice(0, 30)}{linked ? ` → ${n.device!.name}` : ""}</text>
                        <text x={RIGHT_X + 10} y={ny + 11} className="fill-[var(--text3)] text-[10.5px]">{[n.board, n.address].filter(Boolean).join(" · ").slice(0, 38)}</text>
                      </g>
                    );
                  })}
                </g>
              );
            })}
          </svg>
        </div>
      )}
    </Modal>
  );
}
