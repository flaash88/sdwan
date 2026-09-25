import { useEffect, useRef, useState } from "react";
import { Icon } from "../components/Icon";
import { Button, Card, Checkbox, EmptyState, ErrorBox, Input, Loading, Pill, Select, StatusBadge, cls, useAction } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtAgo, fmtSince } from "../lib/format";
import { ifaceLabel, useInterfaces } from "../lib/interfaces";
import { useLive } from "../lib/live";
import { useMeta } from "../lib/meta";
import type { Device } from "../lib/types";
import { useFetch } from "../lib/useFetch";

interface WanLink {
  id?: string;
  slot?: number;
  name: string;
  interface: string;
  gateway: string;
  resolved_gateway?: string | null;
  priority: number;
  weight: number;
  check_type: "ping" | "http";
  check_target: string;
  check_interval_s: number;
  check_timeout_ms: number;
  loss_threshold_pct: number;
  latency_threshold_ms: number | null;
  monthly_limit_gb?: number | null;
  enabled: boolean;
  status?: string;
  active?: boolean;
  last_latency_ms?: number | null;
  last_loss_pct?: number | null;
  last_check_at?: string | null;
  last_change_at?: string | null;
  vol_month?: string | null;
  vol_bytes?: number | null;
}
interface WanConfig {
  mode: string;
  recovery_delay_s: number;
  flush_connections: boolean;
  last_apply: string | null;
  last_error: string | null;
  links: WanLink[];
}
interface Route { comment: string; kind: string; slot: number | null; dst_address: string; gateway: string; distance: number | null; routing_table: string; check_gateway: string | null; active: boolean; disabled: boolean }

const MODE_LABEL: Record<string, string> = { failover: "Failover (nach Priorität)", loadbalance_pcc: "Lastverteilung PCC", loadbalance_ecmp: "Lastverteilung ECMP" };
const TARGETS = ["1.1.1.1", "9.9.9.9", "8.8.4.4", "208.67.222.222"];
const blank = (i: number): WanLink => ({ name: `WAN${i + 1}`, interface: "", gateway: "dhcp", priority: i + 1, weight: 1, check_type: "ping", check_target: TARGETS[i] ?? "", check_interval_s: 10, check_timeout_ms: 1000, loss_threshold_pct: 50, latency_threshold_ms: null, monthly_limit_gb: null, enabled: true });
const MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"];
const gb = (b: number) => (b >= 1e12 ? `${(b / 1e12).toLocaleString("de-DE", { maximumFractionDigits: 2 })} TB` : `${(b / 1e9).toLocaleString("de-DE", { maximumFractionDigits: b < 1e10 ? 2 : 1 })} GB`);

function Volume({ link }: { link: WanLink }) {
  const now = new Date();
  const month = now.toISOString().slice(0, 7);
  const used = link.vol_month === month ? link.vol_bytes ?? 0 : 0;
  const limit = link.monthly_limit_gb ? link.monthly_limit_gb * 1e9 : null;
  const pct = limit ? (100 * used) / limit : null;
  const tone = pct == null ? "text-fg3" : pct >= 100 ? "text-red-text" : pct >= 80 ? "text-orange-text" : "text-fg3";
  return (
    <div className="flex flex-col gap-2 px-4 py-3.5">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-xs text-fg3">Monatsvolumen {MONTHS[now.getMonth()]}</span>
        <span><span className="font-semibold">{gb(used)}</span>{limit && <span className="text-fg3"> von {gb(limit)}</span>}</span>
      </div>
      {limit && (
        <div className="relative h-1.5 overflow-hidden rounded-[3px] bg-sunken" role="meter" aria-valuenow={Math.round(pct!)} aria-valuemin={0} aria-valuemax={100} aria-label="Datenvolumen">
          <div className={cls("h-full rounded-[3px]", pct! >= 100 ? "bg-red" : pct! >= 80 ? "bg-orange" : "bg-gray")} style={{ width: `${Math.min(pct!, 100)}%` }} />
          <div className="absolute inset-y-0 left-[80%] w-px bg-line-strong" />
        </div>
      )}
      <div className={cls("flex items-center gap-[5px] text-xs", tone)}>
        <Icon name={pct != null && pct >= 80 ? "alert" : "checkCircle"} className="text-[13px]" />
        {pct == null ? "kein Monatslimit hinterlegt" : pct >= 100 ? `${Math.round(pct)} % · Limit erreicht` : pct >= 80 ? `${Math.round(pct)} % · Warnschwelle 80 % überschritten` : `${Math.round(pct)} % · unter Warnschwelle (80 %)`}
      </div>
    </div>
  );
}

function LinkCard({ l, cfg, device, editable, simulator, onChanged }: { l: WanLink; cfg: WanConfig; device: Device; editable: boolean; simulator: boolean; onChanged: () => void }) {
  const ifaces = useInterfaces(device.id);
  const [test, setTest] = useState<{ state: "run" | "ok" | "fail"; text?: string } | null>(null);
  const { error, run } = useAction();
  const iface = ifaces.data?.find((x) => x.name === l.interface);
  const isLte = iface?.type === "lte";
  const best = Math.min(...cfg.links.filter((x) => x.enabled).map((x) => x.priority));
  const failover = cfg.mode === "failover";
  const down = l.status === "down";
  const offline = device.status !== "online";
  const doTest = () => void run(async () => {
    setTest({ state: "run" });
    try {
      const r = await api.post<{ sent?: number | string; received?: number | string; avg_ms: number | null; loss_pct: number | string | null }>(`/devices/${device.id}/wan/${l.slot}/test`);
      const sent = Number(r.sent ?? 0), recv = Number(r.received ?? 0);
      const ok = recv > 0;
      setTest({ state: ok ? "ok" : "fail", text: `${ok ? "Test ok" : "Test fehlgeschlagen"} · ${recv}/${sent} Pings${r.avg_ms != null ? ` · ${Math.round(r.avg_ms)} ms` : ""}` });
    } catch (e) { setTest({ state: "fail", text: (e as Error).message }); }
  });
  return (
    <div className={cls("flex flex-col rounded-lg border bg-panel", l.active && !offline ? "border-blue" : "border-line", !l.enabled && "opacity-60")}>
      <div className="flex items-start gap-3 p-4">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-sunken text-[18px] text-fg2"><Icon name={isLte || l.priority > best ? "signal" : "cable"} /></div>
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="truncate text-sm font-semibold">{l.name}</span>
          <span className="truncate font-mono text-xs text-fg3" title={ifaceLabel(l.interface, iface)}>{l.interface} · GW {l.gateway}{l.resolved_gateway && l.resolved_gateway !== l.gateway ? ` → ${l.resolved_gateway}` : ""}</span>
        </div>
        {offline ? <Pill tone="gray" icon="minusCircle">Unbekannt</Pill> : !l.enabled ? <StatusBadge status="disabled" /> : <StatusBadge status={l.status ?? "unknown"} />}
      </div>
      <div className="flex flex-wrap items-center gap-2 px-4 pb-3.5">
        {offline ? null : l.active ? <Pill tone="blue" icon="check">{failover ? "Aktive Leitung" : "Trägt Traffic"}</Pill> : <Pill tone="neutral" icon="pause">Nicht aktiv</Pill>}
        <span className={cls("text-xs", down ? "text-red-text" : "text-fg3")}>
          {down && l.last_change_at ? `Ausfall seit ${new Date(l.last_change_at).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" })} · ${fmtSince(l.last_change_at)}` : l.last_change_at ? `Letzter Wechsel ${fmtAgo(l.last_change_at)}` : ""}
        </span>
      </div>
      <div className="grid grid-cols-3 border-y border-line">
        <div className="flex flex-col gap-0.5 border-r border-line px-4 py-3"><span className="text-xs text-fg3">Latenz</span><span className="text-lg font-semibold">{!offline && !down && l.last_latency_ms != null ? `${Math.round(l.last_latency_ms)} ms` : "—"}</span></div>
        <div className="flex flex-col gap-0.5 border-r border-line px-4 py-3"><span className="text-xs text-fg3">Paketverlust</span><span className={cls("text-lg font-semibold", !offline && (l.last_loss_pct ?? 0) >= l.loss_threshold_pct && "text-red-text")}>{!offline && l.last_loss_pct != null ? `${l.last_loss_pct.toLocaleString("de-DE")} %` : "—"}</span></div>
        <div className="flex flex-col gap-0.5 px-4 py-3"><span className="text-xs text-fg3">{failover ? "Priorität" : "Gewichtung"}</span><span className="text-lg font-semibold">{failover ? l.priority : l.weight} <span className="text-xs font-medium text-fg3">{failover ? (l.priority === best ? "Primär" : "Backup") : ""}</span></span></div>
      </div>
      <Volume link={l} />
      <div className="mt-auto flex flex-wrap items-center justify-between gap-2 rounded-b-lg border-t border-line bg-panel2 px-4 py-3">
        <span className={cls("text-xs", test?.state === "ok" ? "text-green-text" : test?.state === "fail" || error ? "text-red-text" : "text-fg3")}>
          {test?.state === "run" ? "Test läuft …" : test?.text ?? error ?? (l.last_check_at ? `Zuletzt geprüft ${fmtAgo(l.last_check_at)}` : "Noch nicht geprüft")}
        </span>
        <div className="flex gap-2">
          {editable && simulator && <Button size="sm" variant="ghost" onClick={() => void run(async () => { await api.post(`/devices/${device.id}/wan/${l.slot}/simulate-outage?down=${!down}`); onChanged(); })}>{down ? "Wiederherstellen" : "Ausfall simulieren"}</Button>}
          {editable && <Button size="sm" variant="secondary" icon="activity" disabled={test?.state === "run" || offline} onClick={doTest}>Leitung testen</Button>}
        </div>
      </div>
    </div>
  );
}

export default function WanTab({ device }: { device: Device }) {
  const { can } = useAuth();
  const meta = useMeta();
  const wan = useFetch<WanConfig>(`/devices/${device.id}/wan`);
  const routes = useFetch<{ live: boolean; error: string | null; routes: Route[] }>(`/devices/${device.id}/wan/routes`);
  const [editing, setEditing] = useState(false);
  const editorRef = useRef<HTMLDivElement>(null);
  useLive((e) => { if ((e.data as { device_id?: string }).device_id === device.id) { void wan.reload(); void routes.reload(); } }, ["wan.link", "vrrp.state"]);
  useEffect(() => { if (editing) editorRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }); }, [editing]);
  const w = wan.data;
  if (!w) return wan.error ? <ErrorBox error={wan.error} /> : <Loading rows={4} />;
  const editable = can("technician");
  const reload = () => { void wan.reload(); void routes.reload(); };
  const linkName = (slot: number | null) => w.links.find((l) => l.slot === slot)?.name;
  const mainRoutes = (routes.data?.routes ?? []).filter((r) => r.routing_table === "main");
  const tableRoutes = (routes.data?.routes ?? []).filter((r) => r.routing_table !== "main");

  return (
    <>
      <ErrorBox error={w.last_error} />
      <div className="flex flex-wrap items-center gap-x-[18px] gap-y-2 rounded-lg border border-line bg-panel px-3.5 py-2.5 text-fg2">
        <span><span className="text-fg3">Modus:</span> <span className="font-medium text-fg">{MODE_LABEL[w.mode] ?? w.mode}</span></span>
        {w.links.length > 0 && <span><span className="text-fg3">Prüfziele:</span> <span className="font-mono text-xs text-fg">{w.links.map((l) => l.check_target).join(", ")}</span></span>}
        {w.links.length > 0 && <span><span className="text-fg3">Intervall:</span> {[...new Set(w.links.map((l) => `${l.check_interval_s} s`))].join(" / ")} · Verlust ab {[...new Set(w.links.map((l) => `${l.loss_threshold_pct} %`))].join(" / ")}</span>}
        <span><span className="text-fg3">Rückschwenk:</span> nach {w.recovery_delay_s} s stabil</span>
        {w.flush_connections && <span><span className="text-fg3">Verbindungen:</span> bei Ausfall neu aufgebaut</span>}
        <div className="flex-1" />
        <span className="text-fg3">{w.links.length} von 4 Leitungen belegt</span>
        {editable && <Button size="sm" variant="secondary" icon="settings" onClick={() => setEditing(!editing)}>{editing ? "Editor schließen" : "Konfigurieren"}</Button>}
      </div>

      {w.links.length === 0 && !editing ? (
        <Card><EmptyState icon="cable" title="Keine WAN-Leitungen verwaltet" text="Lege bis zu vier Leitungen an. Health-Checks laufen per Netwatch direkt auf dem Router – das Umschalten funktioniert auch ohne Verbindung zur Cloud."
          action={editable && <Button icon="plus" onClick={() => setEditing(true)}>Leitung hinzufügen</Button>} /></Card>
      ) : (
        <div className="grid items-stretch gap-4 md:grid-cols-2 2xl:grid-cols-3">
          {w.links.map((l) => <LinkCard key={l.id} l={l} cfg={w} device={device} editable={editable} simulator={!!meta?.simulator} onChanged={reload} />)}
          {editable && w.links.length < 4 && (
            <button type="button" onClick={() => setEditing(true)}
              className="flex min-h-[280px] cursor-pointer flex-col items-center justify-center gap-2 rounded-lg border-[1.5px] border-dashed border-line-strong text-fg3 hover:border-blue hover:text-blue-text">
              <Icon name="plus" className="text-[20px]" />
              <span className="font-medium">Leitung hinzufügen</span>
              <span className="text-xs">z. B. Kabel, DSL, 5G oder Starlink · max. 4</span>
            </button>
          )}
        </div>
      )}

      <Card title="Routen" subtitle={routes.data?.live === false ? `nicht live abrufbar – ${routes.data.error}` : "verwaltete Routen (sdwan:wan), live vom Router"} flush>
        {!routes.data ? <Loading rows={2} /> : mainRoutes.length + tableRoutes.length === 0 ? <EmptyState compact title={routes.data.live ? "Keine verwalteten Routen" : "Router nicht erreichbar"} /> : (
          <div className="overflow-x-auto">
            <div className="min-w-[640px]">
              {[...mainRoutes, ...tableRoutes].map((r) => (
                <div key={r.comment + r.routing_table} className="grid h-[42px] grid-cols-[110px_minmax(0,1fr)_200px] items-center gap-3 border-b border-line px-4 last:border-b-0">
                  <span>{r.disabled ? <Pill tone="gray" icon="minusCircle">Deaktiviert</Pill> : r.active ? <Pill tone="blue" icon="check">Aktiv</Pill> : <Pill tone="neutral" icon="pause">Inaktiv</Pill>}</span>
                  <span className="truncate font-mono text-xs">{r.dst_address} gateway={r.gateway}{r.distance != null ? ` distance=${r.distance}` : ""}{r.routing_table !== "main" ? ` routing-table=${r.routing_table}` : ""}{r.check_gateway ? ` check-gateway=${r.check_gateway}` : ""}</span>
                  <span className="truncate text-right text-fg3">{r.kind === "default" ? "Default" : r.kind === "check" ? "Prüfroute" : r.kind} · WAN{r.slot} {linkName(r.slot) ?? ""}{r.disabled && r.kind === "default" ? " · von Netwatch/VRRP deaktiviert" : ""}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </Card>

      {editing && editable && <div ref={editorRef}><WanEditor device={device} cfg={w} onSaved={() => { reload(); }} onClose={() => setEditing(false)} /></div>}
    </>
  );
}

function WanEditor({ device, cfg, onSaved, onClose }: { device: Device; cfg: WanConfig; onSaved: () => void; onClose: () => void }) {
  const ifaces = useInterfaces(device.id);
  const [draft, setDraft] = useState<WanConfig>(() => {
    const c = structuredClone(cfg);
    if (c.links.length === 0) c.links.push(blank(0));
    return c;
  });
  const { busy, error, run } = useAction();
  const setLink = (i: number, patch: Partial<WanLink>) => setDraft({ ...draft, links: draft.links.map((l, j) => (j === i ? { ...l, ...patch } : l)) });
  return (
    <Card title="WAN-Konfiguration" subtitle="Änderungen werden beim Speichern auf den Router übertragen" actions={<Button size="sm" variant="ghost" icon="x" onClick={onClose}>Schließen</Button>}>
      <ErrorBox error={error} />
      <div className="mb-4 grid gap-4 md:grid-cols-3">
        <Select label="Modus" value={draft.mode} onChange={(e) => setDraft({ ...draft, mode: e.target.value })}>
          <option value="failover">Failover (nach Priorität)</option>
          <option value="loadbalance_pcc">Lastverteilung PCC (gewichtet)</option>
          <option value="loadbalance_ecmp">Lastverteilung ECMP (gleichmäßig)</option>
        </Select>
        <Input label="Recovery-Verzögerung (s)" hint="Leitung muss so lange stabil sein, bevor zurückgeschwenkt wird" type="number" min={0} value={draft.recovery_delay_s} onChange={(e) => setDraft({ ...draft, recovery_delay_s: Number(e.target.value) })} />
        <div className="pt-7"><Checkbox label="Verbindungen bei Ausfall neu aufbauen" checked={draft.flush_connections} onChange={(v) => setDraft({ ...draft, flush_connections: v })} /></div>
      </div>
      <div className="space-y-3">
        {draft.links.map((l, i) => (
          <fieldset key={i} className="rounded-lg border border-line p-4">
            <legend className="px-1 font-semibold">WAN-Leitung {i + 1}</legend>
            <div className="mb-3 flex justify-end gap-3">
              <Checkbox label="aktiv" checked={l.enabled} onChange={(v) => setLink(i, { enabled: v })} />
              <Button size="sm" variant="ghost" icon="trash" onClick={() => setDraft({ ...draft, links: draft.links.filter((_, j) => j !== i) })}>Entfernen</Button>
            </div>
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
              <Input label="Name" value={l.name} onChange={(e) => setLink(i, { name: e.target.value })} />
              <Select label="Interface" value={l.interface} onChange={(e) => setLink(i, { interface: e.target.value })}>
                {!ifaces.data?.some((x) => x.name === l.interface) && <option value={l.interface}>{l.interface || "– wählen –"}</option>}
                {ifaces.data?.map((x) => <option key={x.name} value={x.name}>{ifaceLabel(x.name, x)}{x.running ? "" : " (kein Link)"}{x.type && x.type !== "ether" ? ` [${x.type}]` : ""}</option>)}
              </Select>
              <Input label="Gateway" hint="IP, Interface oder dhcp" value={l.gateway} onChange={(e) => setLink(i, { gateway: e.target.value })} className="font-mono" />
              <Input label="Prüfziel" hint="IP, pro Leitung eindeutig" value={l.check_target} onChange={(e) => setLink(i, { check_target: e.target.value })} className="font-mono" />
              <Select label="Prüfart" value={l.check_type} onChange={(e) => setLink(i, { check_type: e.target.value as "ping" | "http" })}>
                <option value="ping">Ping (ICMP)</option>
                <option value="http">HTTP-GET</option>
              </Select>
              <Input label={draft.mode === "failover" ? "Priorität (1 = bevorzugt)" : "Priorität (Fallback)"} type="number" min={1} max={4} value={l.priority} onChange={(e) => setLink(i, { priority: Number(e.target.value) })} />
              <Input label="Gewichtung (PCC)" type="number" min={1} max={10} value={l.weight} disabled={draft.mode !== "loadbalance_pcc"} onChange={(e) => setLink(i, { weight: Number(e.target.value) })} />
              <Input label="Intervall (s)" type="number" min={1} value={l.check_interval_s} onChange={(e) => setLink(i, { check_interval_s: Number(e.target.value) })} />
              <Input label="Verlust-Schwelle (%)" type="number" min={1} max={100} value={l.loss_threshold_pct} onChange={(e) => setLink(i, { loss_threshold_pct: Number(e.target.value) })} />
              <Input label="Latenz-Schwelle (ms)" hint="optional" type="number" value={l.latency_threshold_ms ?? ""} onChange={(e) => setLink(i, { latency_threshold_ms: e.target.value ? Number(e.target.value) : null })} />
              <Input label="Monatslimit (GB)" hint="optional, z. B. 5G-Tarif" type="number" min={0} step="any" value={l.monthly_limit_gb ?? ""} onChange={(e) => setLink(i, { monthly_limit_gb: e.target.value ? Number(e.target.value) : null })} />
            </div>
          </fieldset>
        ))}
      </div>
      <div className="mt-4 flex flex-wrap justify-between gap-2">
        <Button variant="secondary" icon="plus" disabled={draft.links.length >= 4} onClick={() => setDraft({ ...draft, links: [...draft.links, blank(draft.links.length)] })}>Leitung ({draft.links.length}/4)</Button>
        <Button disabled={busy} onClick={() => void run(async () => {
          const r = await api.put<WanConfig & { push: { ok: boolean; error?: string } | null }>(`/devices/${device.id}/wan`, { ...draft, push: true });
          setDraft(structuredClone(r));
          onSaved();
          if (r.push && !r.push.ok) throw new Error(`Gespeichert, aber Push fehlgeschlagen: ${r.push.error}`);
        })}>{busy ? "Übertrage …" : "Speichern & auf Router anwenden"}</Button>
      </div>
    </Card>
  );
}
