import { Link } from "react-router-dom";
import { Card, ErrorBox, Loading, Table } from "../components/ui";
import { fmtDate } from "../lib/format";
import type { Device } from "../lib/types";
import { useFetch } from "../lib/useFetch";

export interface Neighbor { interface: string | null; identity: string | null; platform: string | null; board: string | null; version: string | null; mac_address: string | null; address: string | null; seen_at: string; device: { id: string; name: string } | null }

/** Nachbarn aus /ip/neighbor (alle 10 min); Plattform-Geräte werden verlinkt. */
export default function NeighborsTab({ device }: { device: Device }) {
  const q = useFetch<Neighbor[]>(`/devices/${device.id}/neighbors`);
  return (
    <Card title="Nachbarn" subtitle={`per Discovery (MNDP/CDP/LLDP) · Stand ${fmtDate(q.data?.[0]?.seen_at)}`} flush>
      <ErrorBox error={q.error} />
      {!q.data ? <Loading rows={3} /> : (
        <Table head={["Interface", "Identity", "Gerät/Plattform", "Version", "MAC", "Adresse"]} empty={q.data.length === 0} emptyText="Keine Nachbarn sichtbar">
          {q.data.map((n, i) => (
            <tr key={`${n.mac_address}-${i}`}>
              <td className="px-3 py-2 font-mono text-xs">{n.interface ?? "–"}</td>
              <td className="px-3 py-2 font-medium">{n.device ? <Link to={`/devices/${n.device.id}`} className="text-blue-text hover:underline">{n.identity} → {n.device.name}</Link> : n.identity ?? "–"}</td>
              <td className="px-3 py-2">{[n.board, n.platform].filter(Boolean).join(" · ") || "–"}</td>
              <td className="px-3 py-2 font-mono text-xs">{n.version ?? "–"}</td>
              <td className="px-3 py-2 font-mono text-xs">{n.mac_address ?? "–"}</td>
              <td className="px-3 py-2 font-mono text-xs">{n.address ?? "–"}</td>
            </tr>
          ))}
        </Table>
      )}
    </Card>
  );
}
