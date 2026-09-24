import { Link } from "react-router-dom";
import { Badge, Card, StatusBadge, Table } from "../components/ui";
import { fmtDate } from "../lib/format";
import type { Device } from "../lib/types";
import { useFetch } from "../lib/useFetch";

interface DP { policy_id: string; name: string; scope: string; version: number; deployed_version: number | null; status: string; last_error: string | null; deployed_at: string | null }

export default function PoliciesTab({ device }: { device: Device }) {
  const list = useFetch<DP[]>(`/devices/${device.id}/policies`);
  return (
    <Card title="Zugewiesene Firewall-Policies (in Push-Reihenfolge)">
      <Table head={["Policy", "Geltung", "Stand", "Status", "Gepusht"]} empty={list.data?.length === 0}>
        {list.data?.map((p) => (
          <tr key={p.policy_id}>
            <td className="px-3 py-2"><Link className="text-brand-700 hover:underline" to={`/policies/${p.policy_id}`}>{p.name}</Link></td>
            <td className="px-3 py-2">{p.scope === "global" ? <Badge color="blue">global</Badge> : <Badge>Mandant</Badge>}</td>
            <td className="px-3 py-2">{p.deployed_version ? `v${p.deployed_version}` : "–"} / v{p.version}</td>
            <td className="px-3 py-2"><StatusBadge status={p.status === "deployed" && p.deployed_version !== p.version ? "pending" : p.status} />{p.last_error && <div className="text-xs text-red-600">{p.last_error}</div>}</td>
            <td className="px-3 py-2 text-slate-500">{fmtDate(p.deployed_at)}</td>
          </tr>
        ))}
      </Table>
    </Card>
  );
}
