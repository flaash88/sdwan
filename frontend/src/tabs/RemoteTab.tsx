import { useEffect, useState } from "react";
import { Icon, type IconName } from "../components/Icon";
import { Button, Card, CodeBlock, EmptyState, ErrorBox, Input, Loading, Modal, Notice, Select, StatusBadge, useAction } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtBytes, fmtFull, fmtSince } from "../lib/format";
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

const PROTO: Record<string, { name: string; icon: IconName; desc: string; port: number }> = {
  winbox: { name: "WinBox", icon: "monitor", desc: "Zeitlich begrenzter Port am Fleet-Proxy, weitergeleitet durch den Management-Tunnel.", port: 8291 },
  webfig: { name: "WebFig", icon: "globe", desc: "Weboberfläche des Routers über den Fleet-Proxy im Browser öffnen.", port: 80 },
  ssh: { name: "SSH", icon: "terminal", desc: "SSH-Zugang über den Fleet-Proxy mit temporärem Benutzer.", port: 22 },
};

function OpenDialog({ device, protocol, onClose, onOpened }: { device: Device; protocol: string; onClose: () => void; onOpened: () => void }) {
  const [f, setF] = useState({ duration_minutes: 60, reason: "", allowed_cidr: "" });
  const [created, setCreated] = useState<RemoteSession | null>(null);
  const { busy, error, run } = useAction();
  const p = PROTO[protocol];
  return (
    <Modal open onClose={onClose} title={`${p.name}-Sitzung starten`} subtitle={`${device.name} · Zugang wird bei Ablauf automatisch entfernt und im Audit-Log protokolliert.`} size="lg"
      footer={created ? <Button onClick={onClose}>Fertig</Button> : <>
        <Button variant="secondary" onClick={onClose}>Abbrechen</Button>
        <Button icon="play" disabled={busy} onClick={() => void run(async () => {
          setCreated(await api.post<RemoteSession>(`/devices/${device.id}/remote-sessions`, { protocol, ...f, allowed_cidr: f.allowed_cidr || null, reason: f.reason || null }));
          onOpened();
        })}>{busy ? "Öffne …" : "Sitzung starten"}</Button>
      </>}>
      <ErrorBox error={error} />
      {created ? (
        <div className="flex flex-col gap-3">
          <Notice tone="green" title="Zugang aktiv">Läuft ab in <Countdown until={created.expires_at} /> · Zugriff nur von <span className="font-mono">{created.allowed_cidr}</span></Notice>
          <CodeBlock text={created.connect} highlight={false} />
          <div className="grid gap-2 sm:grid-cols-2">
            <div className="flex flex-col gap-0.5"><span className="text-xs text-fg3">Benutzer</span><span className="font-mono">{created.username}</span></div>
            <div className="flex flex-col gap-0.5"><span className="text-xs text-fg3">Passwort (nur jetzt sichtbar)</span><span className="font-mono">{created.password}</span></div>
          </div>
        </div>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2">
          <Select label="Dauer" value={f.duration_minutes} onChange={(e) => setF({ ...f, duration_minutes: Number(e.target.value) })}>
            {[15, 30, 60, 120, 240].map((m) => <option key={m} value={m}>{m} Minuten</option>)}
          </Select>
          <Input label="Erlaubte Quell-IP/CIDR" hint="leer = meine aktuelle IP" value={f.allowed_cidr} onChange={(e) => setF({ ...f, allowed_cidr: e.target.value })} className="font-mono" />
          <div className="sm:col-span-2"><Input label="Grund / Ticket" value={f.reason} onChange={(e) => setF({ ...f, reason: e.target.value })} placeholder="z. B. Ticket #4711 – Failover-Analyse" /></div>
        </div>
      )}
    </Modal>
  );
}

export default function RemoteTab({ device }: { device: Device }) {
  const { can, me } = useAuth();
  const list = useFetch<RemoteSession[]>(`/devices/${device.id}/remote-sessions`);
  const [proto, setProto] = useState<string | null>(null);
  const { error, run } = useAction();
  useLive(() => void list.reload(), ["remote.session"]);
  const active = list.data?.filter((s) => s.status === "active") ?? [];
  const offline = device.status !== "online";
  return (
    <>
      {offline && <Notice tone="orange">Gerät nicht erreichbar – Fernzugriff ist erst wieder möglich, wenn der Management-Tunnel steht.</Notice>}
      <div className="grid gap-4 md:grid-cols-3">
        {Object.entries(PROTO).map(([k, p]) => (
          <div key={k} className="flex flex-col gap-2.5 rounded-lg border border-line bg-panel p-4">
            <div className="flex items-center gap-2.5">
              <span className="flex h-8 w-8 items-center justify-center rounded-[7px] bg-sunken text-[16px] text-fg2"><Icon name={p.icon} /></span>
              <span className="text-sm font-semibold">{p.name}</span>
            </div>
            <span className="text-pretty text-fg2">{p.desc}</span>
            <span className="font-mono text-xs text-fg3">Proxy → {device.tunnel_ip}:{p.port}</span>
            {can("technician") && <Button variant="secondary" size="sm" icon="play" className="self-start" disabled={offline} onClick={() => setProto(k)}>Sitzung starten</Button>}
          </div>
        ))}
      </div>
      <ErrorBox error={error} />
      <Card flush title="Sitzungen" subtitle={`${active.length} aktiv · werden im Audit-Log protokolliert`}>
        {!list.data ? <Loading rows={2} /> : list.data.length === 0 ? <EmptyState compact title="Noch keine Sitzungen" /> : (
          <div className="overflow-x-auto">
            <div className="min-w-[900px]">
              <div className="grid grid-cols-[150px_170px_80px_minmax(0,1fr)_120px_90px_140px_90px] gap-3 border-b border-line bg-panel2 px-4 py-2 text-xs font-medium text-fg3">
                <span>Start</span><span>Benutzer</span><span>Art</span><span>Verbindung</span><span>Status</span><span>Dauer</span><span>Traffic</span><span />
              </div>
              {list.data.map((s) => (
                <div key={s.id} className="grid min-h-[42px] grid-cols-[150px_170px_80px_minmax(0,1fr)_120px_90px_140px_90px] items-center gap-3 border-b border-line px-4 py-1.5 last:border-b-0">
                  <span className="font-mono text-xs text-fg2">{fmtFull(s.created_at)}</span>
                  <span className="flex min-w-0 flex-col leading-tight"><span className="truncate">{s.user_email}</span>{s.reason && <span className="truncate text-xs text-fg3">{s.reason}</span>}</span>
                  <span className="text-fg2">{PROTO[s.protocol]?.name ?? s.protocol}</span>
                  <span className="min-w-0 truncate font-mono text-xs" title={`von ${s.allowed_cidr}`}>{s.connect}</span>
                  <span>{s.status === "active" ? <StatusBadge status="active" label="Aktiv" /> : <StatusBadge status={s.status} />}</span>
                  <span className="text-fg2">{s.status === "active" ? <Countdown until={s.expires_at} /> : fmtSince(s.created_at, s.closed_at ?? s.expires_at)}</span>
                  <span className="text-xs text-fg2">↓ {fmtBytes(s.bytes_in)} ↑ {fmtBytes(s.bytes_out)} · {s.connections}×</span>
                  <span className="flex justify-end">
                    {s.status === "active" && (s.user_email === me?.user.email || can("admin")) && (
                      <Button size="sm" variant="ghost" onClick={() => void run(async () => { await api.post(`/remote-sessions/${s.id}/close`); await list.reload(); })}>Beenden</Button>
                    )}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
      </Card>
      {proto && <OpenDialog device={device} protocol={proto} onClose={() => setProto(null)} onOpened={() => void list.reload()} />}
    </>
  );
}
