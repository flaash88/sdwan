import { useState } from "react";
import { Icon } from "../components/Icon";
import { Button, Card, Checkbox, EmptyState, ErrorBox, Loading, Notice, PageHeader, Pill, Segment, StatusBadge, useAction } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtAgo, fmtBytes, fmtDate } from "../lib/format";
import { useLive } from "../lib/live";
import { useFetch } from "../lib/useFetch";

interface MeshNode { device_id: string; name: string; site: string | null; is_hub: boolean; lan_subnets: string[]; mesh_ip: string | null; mesh_endpoint: string | null; status: string; participating: boolean }
interface MeshLink { id: string; a: string; b: string; a_name: string; b_name: string; kind: string; status: string; last_handshake_at: string | null; rx_bytes: number; tx_bytes: number; last_error: string | null }
interface MeshData {
  topology: string; auto_apply: boolean; subnet: string | null; plan_error: string | null; planned_links: number; warnings: string[];
  nodes: MeshNode[]; links: MeshLink[];
  last_apply: { at: string; devices: Record<string, { name: string; ok: boolean; error?: string }> } | null;
}

type LinkState = "up" | "down" | "warn" | "unknown";
const linkState = (s: string): LinkState => (s === "up" ? "up" : s === "down" ? "down" : s === "no_endpoint" ? "warn" : "unknown");
const COL: Record<LinkState, string> = { up: "var(--green)", down: "var(--red)", warn: "var(--orange)", unknown: "var(--gray)" };
const TXT: Record<LinkState, string> = { up: "var(--green-text)", down: "var(--red-text)", warn: "var(--orange-text)", unknown: "var(--text3)" };

function Topology({ nodes, links, hubSpoke }: { nodes: MeshNode[]; links: MeshLink[]; hubSpoke: boolean }) {
  const W = 820, H = 540, cx = 410, cy = 270, rx = 300, ry = 196;
  const hub = hubSpoke ? nodes.find((n) => n.is_hub) : undefined;
  const ring = nodes.filter((n) => n !== hub);
  const pos = new Map<string, { x: number; y: number }>();
  if (hub) pos.set(hub.device_id, { x: cx, y: cy });
  ring.forEach((n, k) => {
    const a = -Math.PI / 2 + (2 * Math.PI * k) / Math.max(ring.length, 1);
    pos.set(n.device_id, { x: cx + rx * Math.cos(a), y: cy + ry * Math.sin(a) });
  });
  const nodeState = (n: MeshNode): LinkState => {
    if (n.status !== "online") return "down";
    const own = links.filter((l) => l.a === n.device_id || l.b === n.device_id);
    return own.some((l) => linkState(l.status) === "down") ? "warn" : own.some((l) => linkState(l.status) === "warn") ? "warn" : "up";
  };
  const trunc = (s: string, n: number) => (s.length > n ? s.slice(0, n - 1) + "…" : s);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="block h-auto w-full" role="img" aria-label={`Mesh-Topologie mit ${nodes.length} Standorten und ${links.length} Tunneln`}>
      {links.map((l) => {
        const a = pos.get(l.a), b = pos.get(l.b);
        if (!a || !b) return null;
        const s = linkState(l.status);
        const toHub = hub && (l.a === hub.device_id || l.b === hub.device_id);
        return <line key={l.id} x1={a.x} y1={a.y} x2={b.x} y2={b.y} style={{ stroke: COL[s], opacity: toHub || !hub ? 1 : 0.55 }} strokeWidth={toHub ? 2.5 : 1.4} strokeDasharray={s === "down" ? "7 6" : undefined}><title>{`${l.a_name} ↔ ${l.b_name}: ${l.status}`}</title></line>;
      })}
      {links.map((l) => {
        const a = pos.get(l.a), b = pos.get(l.b);
        if (!a || !b) return null;
        const s = linkState(l.status);
        const t = s === "down" ? "keine Verbindung" : s === "warn" ? "kein Endpoint" : l.last_handshake_at ? `Handshake ${fmtAgo(l.last_handshake_at)}` : "–";
        const w = t.length * 7 + 20;
        return (
          <g key={l.id + "l"} transform={`translate(${(a.x + b.x) / 2},${(a.y + b.y) / 2})`}>
            <rect x={-w / 2} y={-11} width={w} height={22} rx={11} style={{ fill: "var(--panel)", stroke: COL[s] }} />
            <text x={0} y={4} textAnchor="middle" fontSize={11} fontWeight={500} style={{ fill: TXT[s], fontFamily: "var(--font-mono)" }}>{t}</text>
          </g>
        );
      })}
      {ring.map((n) => {
        const p = pos.get(n.device_id)!;
        const s = nodeState(n);
        const icon = s === "up" ? "M-2.5 0l2 2 3.5-4" : s === "warn" ? "M0 -3.5v3.5M0 3h.01" : "M-2.5 -2.5l5 5M2.5 -2.5l-5 5";
        const sub = n.status !== "online" ? "Gerät offline" : s === "warn" ? "Tunnel gestört" : n.mesh_ip ?? "–";
        return (
          <g key={n.device_id} transform={`translate(${p.x},${p.y})`}>
            <title>{`${n.site ?? n.name} (${n.name}) · ${n.lan_subnets.join(", ")}`}</title>
            <rect x={-76} y={-24} width={152} height={48} rx={8} style={{ fill: "var(--panel)", stroke: s === "up" ? "var(--border-strong)" : COL[s] }} strokeWidth={s === "up" ? 1 : 1.5} />
            <circle cx={-58} cy={0} r={8} style={{ fill: COL[s] }} />
            <path d={icon} transform="translate(-58,0)" stroke="#fff" strokeWidth={1.8} fill="none" strokeLinecap="round" strokeLinejoin="round" />
            <text x={-42} y={-3} fontSize={13} fontWeight={600} style={{ fill: "var(--text)" }}>{trunc(n.site ?? n.name, 15)}</text>
            <text x={-42} y={13} fontSize={11} style={{ fill: s === "up" ? "var(--text3)" : TXT[s], fontFamily: "var(--font-mono)" }}>{trunc(sub, 17)}</text>
          </g>
        );
      })}
      {hub && (
        <g transform={`translate(${cx},${cy})`}>
          <rect x={-115} y={-30} width={230} height={60} rx={10} style={{ fill: "var(--blue-bg)", stroke: "var(--blue)" }} strokeWidth={1.5} />
          <rect x={-115} y={-30} width={230} height={60} rx={10} style={{ fill: "var(--panel)", opacity: 0.5 }} />
          <text x={0} y={-4} textAnchor="middle" fontSize={14} fontWeight={600} style={{ fill: "var(--text)" }}>{trunc(`Hub · ${hub.site ?? hub.name}`, 22)}</text>
          <text x={0} y={14} textAnchor="middle" fontSize={11} style={{ fill: "var(--text2)", fontFamily: "var(--font-mono)" }}>{trunc(`${hub.name} · ${hub.mesh_ip ?? "–"}`, 28)}</text>
        </g>
      )}
    </svg>
  );
}

export default function Mesh() {
  const { can, me } = useAuth();
  const mesh = useFetch<MeshData>(me?.active_tenant_id ? "/mesh" : null);
  const { busy, error, run } = useAction();
  const [report, setReport] = useState<MeshData["last_apply"] | null>(null);
  useLive(() => void mesh.reload(), ["mesh.link", "mesh.applied", "device.status"]);
  if (!me?.active_tenant_id) {
    return <><PageHeader title="VPN-Mesh" /><Card><EmptyState icon="building" title="Bitte einen Mandanten wählen" text="Das Site-to-Site-Mesh wird je Mandant geplant. Wähle oben links einen Mandanten." /></Card></>;
  }
  const m = mesh.data;
  if (!m) return <><PageHeader title="VPN-Mesh" /><ErrorBox error={mesh.error} />{!mesh.error && <Loading rows={4} />}</>;
  const nodes = m.nodes.filter((n) => n.participating);
  const up = m.links.filter((l) => l.status === "up").length;
  const last = report ?? m.last_apply;
  const hubSpoke = m.topology === "hub_spoke";
  const setTopo = (topology: string) => void run(async () => { await api.put("/mesh/settings", { topology, auto_apply: m.auto_apply }); await mesh.reload(); });
  const configured = m.topology !== "none" && (m.links.length > 0 || nodes.length > 1);
  const nameOf = (l: MeshLink) => {
    if (!hubSpoke) return `${l.a_name} ↔ ${l.b_name}`;
    const hub = m.nodes.find((n) => n.is_hub);
    const spoke = hub && l.a === hub.device_id ? l.b : l.a;
    const n = m.nodes.find((x) => x.device_id === spoke);
    return n?.site ?? n?.name ?? `${l.a_name} ↔ ${l.b_name}`;
  };

  return (
    <>
      <PageHeader
        title="VPN-Mesh"
        subtitle={`WireGuard Site-to-Site · ${nodes.length} ${nodes.length === 1 ? "Standort" : "Standorte"} · ${m.links.length} Tunnel${m.subnet ? ` · Transfernetz ${m.subnet}` : ""}`}
        actions={<>
          <span className="text-xs text-fg3">Topologie</span>
          {can("admin") ? (
            <Segment label="Topologie" value={m.topology} onChange={setTopo} options={[{ value: "hub_spoke", label: "Hub-and-Spoke" }, { value: "full_mesh", label: "Full-Mesh" }, { value: "none", label: "Aus" }]} />
          ) : <Pill>{m.topology === "full_mesh" ? "Full-Mesh" : m.topology === "none" ? "Aus" : "Hub-and-Spoke"}</Pill>}
          {can("technician") && configured && <Button icon="upload" disabled={busy} onClick={() => void run(async () => { setReport(await api.post("/mesh/apply")); await mesh.reload(); })}>{busy ? "Wende an …" : "Konfiguration anwenden"}</Button>}
        </>}
      />
      <ErrorBox error={error ?? m.plan_error} />
      {m.warnings.length > 0 && <div className="mb-4"><Notice tone="orange">{m.warnings.map((w) => <div key={w}>{w}</div>)}</Notice></div>}

      {!configured ? (
        <Card>
          <EmptyState icon="network" title={m.topology === "none" ? "Site-to-Site-Mesh ist ausgeschaltet" : "Noch kein Mesh konfiguriert"}
            text={<>Das Mesh verbindet die LANs der Standorte direkt per WireGuard. Voraussetzung: mindestens zwei verbundene Geräte an Standorten mit LAN-Subnetzen{hubSpoke ? " und ein Hub-Standort (unter „Standorte“ festlegen)" : ""}. Der Management-Tunnel zur Cloud ist davon unabhängig und wird hier nicht angezeigt.</>}
            action={can("admin") && m.topology === "none" && <Button onClick={() => setTopo("hub_spoke")}>Hub-and-Spoke aktivieren</Button>} />
        </Card>
      ) : (
        <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
          <section className="overflow-hidden rounded-lg border border-line bg-panel">
            <div className="p-2"><Topology nodes={nodes} links={m.links} hubSpoke={hubSpoke} /></div>
            <div className="flex flex-wrap items-center gap-x-[18px] gap-y-2 border-t border-line px-4 py-2.5 text-xs text-fg2">
              <span className="flex items-center gap-1.5"><span className="h-[2.5px] w-[18px] bg-green" /><Icon name="checkCircle" className="text-[13px] text-green-text" />Up</span>
              <span className="flex items-center gap-1.5"><span className="h-[2.5px] w-[18px] bg-orange" /><Icon name="alert" className="text-[13px] text-orange-text" />Kein Endpoint / gestört</span>
              <span className="flex items-center gap-1.5"><span className="w-[18px] border-t-[2.5px] border-dashed border-red" /><Icon name="xCircle" className="text-[13px] text-red-text" />Down</span>
              <div className="flex-1" /><span className="text-fg3">Beschriftung: letzter WireGuard-Handshake</span>
            </div>
          </section>
          <div className="flex flex-col gap-4">
            <section className="overflow-hidden rounded-lg border border-line bg-panel">
              <header className="flex items-center gap-2 border-b border-line px-4 py-3">
                <span className="font-semibold">{hubSpoke ? "Tunnel zum Hub" : "Tunnel"}</span><div className="flex-1" />
                <span className="text-xs text-fg3">{up} von {m.links.length} aktiv</span>
              </header>
              {m.links.length === 0 && <EmptyState compact title="Noch keine Tunnel – Konfiguration anwenden" />}
              {m.links.map((l) => (
                <div key={l.id} className="flex flex-col gap-1.5 border-b border-line px-4 py-3 last:border-b-0">
                  <div className="flex items-center gap-2"><span className="flex-1 truncate font-medium">{nameOf(l)}</span><StatusBadge status={l.status} /></div>
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-fg2">
                    <span className="font-mono">{l.kind === "hub_spoke" ? "Hub-Spoke" : "Mesh"}</span>
                    <span>↓ {fmtBytes(l.rx_bytes)} ↑ {fmtBytes(l.tx_bytes)}</span>
                    <div className="flex-1" />
                    <span className="text-fg3">{l.last_handshake_at ? `Handshake ${fmtAgo(l.last_handshake_at)}` : "noch kein Handshake"}</span>
                  </div>
                  {l.last_error && <div className="text-xs text-red-text">{l.last_error}</div>}
                </div>
              ))}
            </section>
            {can("admin") && (
              <Card title="Einstellungen">
                <Checkbox label="Änderungen automatisch anwenden (alle 5 min)" checked={m.auto_apply} onChange={(v) => void run(async () => { await api.put("/mesh/settings", { topology: m.topology, auto_apply: v }); await mesh.reload(); })} />
                <p className="mt-2 text-xs text-fg3">Hub-Standort unter „Standorte“ festlegen. Öffentliche Endpoints je Gerät im Gerätedetail (sonst die vom Hub erkannte Adresse).</p>
              </Card>
            )}
            {last && (
              <Card title="Letzte Anwendung" subtitle={fmtDate(last.at)} flush>
                {Object.entries(last.devices).map(([id, d]) => (
                  <div key={id} className="flex items-center justify-between gap-2 border-b border-line px-4 py-2 last:border-b-0">
                    <span className="truncate">{d.name}</span>
                    {d.ok ? <StatusBadge status="ok" /> : <span className="truncate text-xs text-red-text" title={d.error}>{d.error}</span>}
                  </div>
                ))}
              </Card>
            )}
          </div>
        </div>
      )}
    </>
  );
}
