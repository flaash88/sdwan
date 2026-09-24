import { useState } from "react";
import { Badge, Button, Card, ErrorBox, PageHeader, Select, Stat, StatusBadge, Table, useAction } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtAgo, fmtBytes, fmtDate } from "../lib/format";
import { useLive } from "../lib/live";
import { useFetch } from "../lib/useFetch";

interface MeshNode {
  device_id: string;
  name: string;
  site: string | null;
  is_hub: boolean;
  lan_subnets: string[];
  mesh_ip: string | null;
  mesh_endpoint: string | null;
  status: string;
  participating: boolean;
}
interface MeshLink {
  id: string;
  a: string;
  b: string;
  a_name: string;
  b_name: string;
  kind: string;
  status: string;
  last_handshake_at: string | null;
  rx_bytes: number;
  tx_bytes: number;
  last_error: string | null;
}
interface MeshData {
  topology: string;
  auto_apply: boolean;
  subnet: string | null;
  plan_error: string | null;
  planned_links: number;
  warnings: string[];
  nodes: MeshNode[];
  links: MeshLink[];
  last_apply: { at: string; devices: Record<string, { name: string; ok: boolean; error?: string }> } | null;
}

const linkColor = (s: string) => (s === "up" ? "#10b981" : s === "down" ? "#ef4444" : s === "no_endpoint" ? "#f59e0b" : "#94a3b8");

function Topology({ nodes, links }: { nodes: MeshNode[]; links: MeshLink[] }) {
  const W = 640, H = 380, cx = W / 2, cy = H / 2;
  const hub = nodes.find((n) => n.is_hub);
  const ring = nodes.filter((n) => n !== hub || links.every((l) => l.kind === "full_mesh"));
  const pos = new Map<string, { x: number; y: number }>();
  if (hub && !ring.includes(hub)) pos.set(hub.device_id, { x: cx, y: cy });
  ring.forEach((n, i) => {
    const a = (2 * Math.PI * i) / Math.max(ring.length, 1) - Math.PI / 2;
    pos.set(n.device_id, { x: cx + Math.cos(a) * 150, y: cy + Math.sin(a) * 140 });
  });
  if (nodes.length === 0) return <p className="py-12 text-center text-sm text-slate-400">Noch keine Mesh-Teilnehmer – Geräte gepairt und Standorten zugeordnet?</p>;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full">
      {links.map((l) => {
        const a = pos.get(l.a), b = pos.get(l.b);
        if (!a || !b) return null;
        return <line key={l.id} x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke={linkColor(l.status)} strokeWidth={3} strokeDasharray={l.status === "up" ? undefined : "6 4"} />;
      })}
      {nodes.map((n) => {
        const p = pos.get(n.device_id);
        if (!p) return null;
        return (
          <g key={n.device_id} transform={`translate(${p.x},${p.y})`}>
            <circle r={n.is_hub ? 26 : 20} fill={n.is_hub ? "#0f766e" : "#fff"} stroke={n.status === "online" ? "#10b981" : "#ef4444"} strokeWidth={3} />
            <text textAnchor="middle" dy="4" fontSize="10" fill={n.is_hub ? "#fff" : "#334155"} fontWeight="600">{n.is_hub ? "HUB" : n.mesh_ip?.split(".").pop()}</text>
            <text textAnchor="middle" dy={n.is_hub ? 44 : 36} fontSize="11" fill="#0f172a" fontWeight="600">{n.site ?? n.name}</text>
            <text textAnchor="middle" dy={n.is_hub ? 57 : 49} fontSize="9" fill="#64748b">{n.lan_subnets.join(", ")}</text>
          </g>
        );
      })}
    </svg>
  );
}

export default function Mesh() {
  const { can, me } = useAuth();
  const mesh = useFetch<MeshData>(me?.active_tenant_id ? "/mesh" : null);
  const { busy, error, run } = useAction();
  const [report, setReport] = useState<MeshData["last_apply"] | null>(null);
  useLive(() => void mesh.reload(), ["mesh.link", "mesh.applied"]);
  if (!me?.active_tenant_id) return <><PageHeader title="VPN-Mesh" /><Card><p className="text-sm text-slate-500">Bitte links einen Mandanten wählen.</p></Card></>;
  const m = mesh.data;
  const up = m?.links.filter((l) => l.status === "up").length ?? 0;
  const last = report ?? m?.last_apply;
  return (
    <>
      <PageHeader
        title="VPN-Mesh"
        subtitle="Site-to-Site-WireGuard zwischen den Standorten dieses Mandanten"
        actions={can("technician") && <Button disabled={busy} onClick={() => void run(async () => { setReport(await api.post("/mesh/apply")); await mesh.reload(); })}>{busy ? "Wende an …" : "Konfiguration anwenden"}</Button>}
      />
      <ErrorBox error={error ?? mesh.error ?? m?.plan_error ?? null} />
      <div className="mb-6 grid grid-cols-2 gap-4 md:grid-cols-4">
        <Stat label="Topologie" value={m?.topology === "full_mesh" ? "Full-Mesh" : m?.topology === "none" ? "aus" : "Hub & Spoke"} />
        <Stat label="Transfernetz" value={<span className="font-mono text-lg">{m?.subnet ?? "–"}</span>} />
        <Stat label="Tunnel aktiv" value={`${up} / ${m?.links.length ?? 0}`} tone={m && up === m.links.length && up > 0 ? "green" : m && m.links.length > 0 ? "yellow" : undefined} />
        <Stat label="Geplante Verbindungen" value={m?.planned_links ?? "–"} />
      </div>
      {m && m.warnings.length > 0 && (
        <div className="mb-4 rounded-lg border border-amber-200 bg-amber-50 px-4 py-2 text-sm text-amber-800">
          {m.warnings.map((w) => <div key={w}>⚠ {w}</div>)}
        </div>
      )}
      <div className="grid gap-6 xl:grid-cols-3">
        <Card title="Topologie" className="xl:col-span-2">
          {m && <Topology nodes={m.nodes.filter((n) => n.participating)} links={m.links} />}
          <div className="mt-2 flex gap-4 text-xs text-slate-500">
            <span><span className="mr-1 inline-block h-0.5 w-4 bg-emerald-500 align-middle" />aktiv</span>
            <span><span className="mr-1 inline-block h-0.5 w-4 bg-red-500 align-middle" />down</span>
            <span><span className="mr-1 inline-block h-0.5 w-4 bg-amber-500 align-middle" />kein Endpoint</span>
          </div>
        </Card>
        <div className="space-y-6">
          {can("admin") && m && (
            <Card title="Einstellungen">
              <div className="space-y-3">
                <Select label="Topologie" value={m.topology} onChange={(e) => void run(async () => { await api.put("/mesh/settings", { topology: e.target.value, auto_apply: m.auto_apply }); await mesh.reload(); })}>
                  <option value="hub_spoke">Hub-and-Spoke (Standard)</option>
                  <option value="full_mesh">Full-Mesh</option>
                  <option value="none">Aus</option>
                </Select>
                <label className="flex items-center gap-2 text-sm">
                  <input type="checkbox" checked={m.auto_apply} onChange={(e) => void run(async () => { await api.put("/mesh/settings", { topology: m.topology, auto_apply: e.target.checked }); await mesh.reload(); })} />
                  Änderungen automatisch anwenden (alle 5 min)
                </label>
                <p className="text-xs text-slate-500">Hub-Standort unter „Standorte“ festlegen. Öffentliche Endpoints je Gerät im Geräte-Detail (sonst vom Hub erkannte Adresse).</p>
              </div>
            </Card>
          )}
          {last && (
            <Card title="Letzte Anwendung">
              <p className="mb-2 text-xs text-slate-500">{fmtDate(last.at)}</p>
              <ul className="space-y-1 text-sm">
                {Object.entries(last.devices).map(([id, d]) => (
                  <li key={id} className="flex justify-between gap-2">
                    <span>{d.name}</span>
                    {d.ok ? <Badge color="green">ok</Badge> : <span className="truncate text-xs text-red-600" title={d.error}>{d.error}</span>}
                  </li>
                ))}
              </ul>
            </Card>
          )}
        </div>
      </div>
      <Card title="Tunnel" className="mt-6">
        <Table head={["Verbindung", "Typ", "Status", "Letzter Handshake", "RX", "TX", "Hinweis"]} empty={m?.links.length === 0}>
          {m?.links.map((l) => (
            <tr key={l.id}>
              <td className="px-3 py-2 font-medium">{l.a_name} ↔ {l.b_name}</td>
              <td className="px-3 py-2">{l.kind === "hub_spoke" ? "Hub-Spoke" : "Mesh"}</td>
              <td className="px-3 py-2"><StatusBadge status={l.status} /></td>
              <td className="px-3 py-2 text-slate-500">{fmtAgo(l.last_handshake_at)}</td>
              <td className="px-3 py-2">{fmtBytes(l.rx_bytes)}</td>
              <td className="px-3 py-2">{fmtBytes(l.tx_bytes)}</td>
              <td className="px-3 py-2 text-xs text-slate-500">{l.last_error ?? ""}</td>
            </tr>
          ))}
        </Table>
      </Card>
    </>
  );
}
