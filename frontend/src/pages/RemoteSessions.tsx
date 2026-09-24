import { Link } from "react-router-dom";
import { Button, Card, PageHeader, StatusBadge, Table, useAction } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtBytes, fmtDate } from "../lib/format";
import { useLive } from "../lib/live";
import { useFetch } from "../lib/useFetch";
import { Countdown, type RemoteSession } from "../tabs/RemoteTab";

export default function RemoteSessions() {
  const { can, me } = useAuth();
  const list = useFetch<RemoteSession[]>("/remote-sessions");
  const { run } = useAction();
  useLive(() => void list.reload(), ["remote.session"]);
  return (
    <>
      <PageHeader title="Fernzugriff" subtitle="Alle SSH-/Winbox-/WebFig-Sessions über den WireGuard-Tunnel. Neue Sessions im Geräte-Detail öffnen." />
      <Card>
        <Table head={["Start", "Gerät", "Benutzer", "Protokoll", "Status", "Restzeit", "Verbindungen", "Traffic", ""]} empty={list.data?.length === 0}>
          {list.data?.map((s) => (
            <tr key={s.id}>
              <td className="px-3 py-2 text-slate-500">{fmtDate(s.created_at)}</td>
              <td className="px-3 py-2"><Link className="text-brand-700 hover:underline" to={`/devices/${s.device_id}`}>{s.device}</Link></td>
              <td className="px-3 py-2">{s.user_email}<div className="text-xs text-slate-500">{s.reason}</div></td>
              <td className="px-3 py-2">{s.protocol}</td>
              <td className="px-3 py-2"><StatusBadge status={s.status === "active" ? "running" : s.status === "failed" ? "failed" : "queued"} /> <span className="text-xs">{s.status}</span></td>
              <td className="px-3 py-2">{s.status === "active" ? <Countdown until={s.expires_at} /> : s.closed_by ? `beendet von ${s.closed_by}` : "–"}</td>
              <td className="px-3 py-2">{s.connections}</td>
              <td className="px-3 py-2 text-xs">{fmtBytes(s.bytes_in + s.bytes_out)}</td>
              <td className="px-3 py-2 text-right">{s.status === "active" && (s.user_email === me?.user.email || can("admin")) && <Button variant="ghost" onClick={() => void run(async () => { await api.post(`/remote-sessions/${s.id}/close`); await list.reload(); })}>Beenden</Button>}</td>
            </tr>
          ))}
        </Table>
      </Card>
    </>
  );
}
