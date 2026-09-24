import { useEffect, useState } from "react";
import { Button, Card, CopyBox, ErrorBox, Input, Select, StatusBadge, Table, useAction } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtBytes, fmtDate } from "../lib/format";
import { useLive } from "../lib/live";
import type { Device } from "../lib/types";
import { useFetch } from "../lib/useFetch";

export interface RemoteSession {
  id: string; device_id: string; device: string | null; user_email: string; protocol: string; proxy_host: string; listen_port: number;
  allowed_cidr: string; reason: string | null; username: string | null; status: string; expires_at: string; created_at: string;
  closed_at: string | null; closed_by: string | null; connections: number; bytes_in: number; bytes_out: number; connect: string; password?: string | null;
}

export function Countdown({ until }: { until: string }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(t); }, []);
  const s = Math.max(0, Math.round((new Date(until).getTime() - now) / 1000));
  return <span className="font-mono">{Math.floor(s / 60)}:{String(s % 60).padStart(2, "0")}</span>;
}

export default function RemoteTab({ device }: { device: Device }) {
  const { can, me } = useAuth();
  const list = useFetch<RemoteSession[]>(`/devices/${device.id}/remote-sessions`);
  const [f, setF] = useState({ protocol: "winbox", duration_minutes: 60, reason: "", allowed_cidr: "" });
  const [created, setCreated] = useState<RemoteSession | null>(null);
  const { busy, error, run } = useAction();
  useLive(() => void list.reload(), ["remote.session"]);
  const active = list.data?.filter((s) => s.status === "active") ?? [];
  return (
    <div className="space-y-6">
      {can("technician") && (
        <Card title="Zeitlich begrenzten Zugang öffnen">
          <ErrorBox error={error} />
          <div className="grid gap-3 md:grid-cols-4">
            <Select label="Protokoll" value={f.protocol} onChange={(e) => setF({ ...f, protocol: e.target.value })}>
              <option value="winbox">Winbox</option>
              <option value="ssh">SSH</option>
              <option value="webfig">WebFig (HTTP)</option>
            </Select>
            <Select label="Dauer" value={f.duration_minutes} onChange={(e) => setF({ ...f, duration_minutes: Number(e.target.value) })}>
              {[15, 30, 60, 120, 240].map((m) => <option key={m} value={m}>{m} Minuten</option>)}
            </Select>
            <Input label="Erlaubte Quell-IP/CIDR (leer = meine IP)" value={f.allowed_cidr} onChange={(e) => setF({ ...f, allowed_cidr: e.target.value })} />
            <Input label="Grund / Ticket" value={f.reason} onChange={(e) => setF({ ...f, reason: e.target.value })} />
          </div>
          <div className="mt-3 flex justify-end">
            <Button disabled={busy} onClick={() => void run(async () => {
              setCreated(await api.post<RemoteSession>(`/devices/${device.id}/remote-sessions`, { ...f, allowed_cidr: f.allowed_cidr || null, reason: f.reason || null }));
              await list.reload();
            })}>Zugang öffnen</Button>
          </div>
          {created && (
            <div className="mt-4 space-y-2 rounded-lg border border-emerald-200 bg-emerald-50 p-4 text-sm">
              <div className="font-medium text-emerald-900">Zugang aktiv – läuft ab in <Countdown until={created.expires_at} /></div>
              <CopyBox text={created.connect} />
              <div className="grid gap-2 md:grid-cols-2">
                <div>Benutzer: <code className="font-mono">{created.username}</code></div>
                <div>Passwort (nur jetzt sichtbar): <code className="font-mono">{created.password}</code></div>
              </div>
              <p className="text-xs text-emerald-800">Temporärer RouterOS-Benutzer, wird bei Ablauf automatisch entfernt. Zugriff nur von {created.allowed_cidr}. Jede Verbindung wird protokolliert.</p>
            </div>
          )}
        </Card>
      )}
      <Card title={`Sessions (${active.length} aktiv)`}>
        <Table head={["Start", "Benutzer", "Protokoll", "Verbindung", "Status", "Restzeit", "Verbindungen", "Traffic", ""]} empty={list.data?.length === 0}>
          {list.data?.map((s) => (
            <tr key={s.id}>
              <td className="px-3 py-2 text-slate-500">{fmtDate(s.created_at)}</td>
              <td className="px-3 py-2">{s.user_email}<div className="text-xs text-slate-500">{s.reason}</div></td>
              <td className="px-3 py-2">{s.protocol}</td>
              <td className="px-3 py-2 font-mono text-xs">{s.connect}<div className="text-slate-400">von {s.allowed_cidr}</div></td>
              <td className="px-3 py-2"><StatusBadge status={s.status === "active" ? "running" : s.status === "failed" ? "failed" : "queued"} /> <span className="text-xs">{s.status}</span></td>
              <td className="px-3 py-2">{s.status === "active" ? <Countdown until={s.expires_at} /> : "–"}</td>
              <td className="px-3 py-2">{s.connections}</td>
              <td className="px-3 py-2 text-xs">{fmtBytes(s.bytes_in)} ↑ {fmtBytes(s.bytes_out)} ↓</td>
              <td className="px-3 py-2 text-right">
                {s.status === "active" && (s.user_email === me?.user.email || can("admin")) && (
                  <Button variant="ghost" onClick={() => void run(async () => { await api.post(`/remote-sessions/${s.id}/close`); await list.reload(); })}>Beenden</Button>
                )}
              </td>
            </tr>
          ))}
        </Table>
      </Card>
    </div>
  );
}
