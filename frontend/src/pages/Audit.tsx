import { useState } from "react";
import { Card, Input, PageHeader, Table } from "../components/ui";
import { fmtDate } from "../lib/format";
import type { AuditEntry } from "../lib/types";
import { useFetch } from "../lib/useFetch";

export default function AuditPage() {
  const [action, setAction] = useState("");
  const q = action ? `?action=${encodeURIComponent(action)}&limit=300` : "?limit=300";
  const log = useFetch<AuditEntry[]>(`/audit${q}`);
  return (
    <>
      <PageHeader title="Audit-Log" subtitle="Alle schreibenden Aktionen: Konfiguration, Policy-Pushes, Remote-Zugriffe" />
      <Card>
        <div className="mb-4 max-w-xs">
          <Input placeholder="Aktion filtern (z. B. device., policy., remote.)" value={action} onChange={(e) => setAction(e.target.value)} />
        </div>
        <Table head={["Zeit", "Benutzer", "Aktion", "Ziel", "Details", "IP"]} empty={log.data?.length === 0}>
          {log.data?.map((a) => (
            <tr key={a.id} className={a.success ? "" : "bg-red-50"}>
              <td className="whitespace-nowrap px-3 py-2 text-slate-500">{fmtDate(a.created_at)}</td>
              <td className="px-3 py-2">{a.user_email ?? "System"}</td>
              <td className="px-3 py-2 font-mono text-xs">{a.action}</td>
              <td className="px-3 py-2 font-mono text-xs">{a.target_type ? `${a.target_type}:${a.target_id?.slice(0, 8)}` : "–"}</td>
              <td className="max-w-md truncate px-3 py-2 font-mono text-xs text-slate-500" title={JSON.stringify(a.details)}>{JSON.stringify(a.details)}</td>
              <td className="px-3 py-2 text-xs text-slate-500">{a.ip_address ?? "–"}</td>
            </tr>
          ))}
        </Table>
      </Card>
    </>
  );
}
