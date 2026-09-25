import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Icon } from "../components/Icon";
import { Button, Card, Checkbox, EmptyState, ErrorBox, Input, Loading, Pill, Select, cls, useAction } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { useFleetState } from "../lib/fleet";
import { fmtDuration, fmtFull, fmtSince } from "../lib/format";
import { ifaceLabel, useInterfaces } from "../lib/interfaces";
import { useLive } from "../lib/live";
import { useMeta } from "../lib/meta";
import type { Device } from "../lib/types";
import { useFetch } from "../lib/useFetch";
import type { EventsResponse } from "../pages/DeviceDetail";

interface VrrpInstance {
  id?: string;
  name: string;
  interface: string;
  vrid: number;
  priority: number;
  interval_ms: number;
  preemption: boolean;
  version: number;
  vip: string;
  local_address: string | null;
  linked_wan_slot: number | null;
  enabled: boolean;
  peer_address: string | null;
  peer_description: string | null;
  state?: string;
  last_change_at?: string | null;
  peer_reachable?: boolean | null;
  peer_rtt_ms?: number | null;
  peer_checked_at?: string | null;
}
interface VrrpConfig { instances: VrrpInstance[]; last_error: string | null }
interface WanSlot { slot: number; name: string; interface: string; active?: boolean }

const DAYS = 14;
const blank = (i: number): VrrpInstance => ({ name: `vrrp${i + 1}`, interface: "", vrid: 1, priority: 100, interval_ms: 1000, preemption: true, version: 3, vip: "", local_address: null, linked_wan_slot: null, enabled: true, peer_address: null, peer_description: null });
/** VIP auf dem MikroTik ist immer /32 */
const vip32 = (v: string) => (v.includes("/") ? v : `${v}/32`);
const interval = (ms: number) => (ms % 1000 === 0 ? `${ms / 1000} s` : `${ms} ms`);

function RolePill({ state }: { state: string }) {
  return state === "master" ? <Pill tone="orange" icon="alert">Master</Pill> : state === "backup" ? <Pill tone="neutral" icon="pause">Backup</Pill> : <Pill tone="gray" icon="minusCircle">{state === "disabled" ? "Deaktiviert" : "Unbekannt"}</Pill>;
}

function RoleCard({ v, device, wan, simulator, editable, onChanged }: { v: VrrpInstance; device: Device; wan: WanSlot[]; simulator: boolean; editable: boolean; onChanged: () => void }) {
  const fleet = useFleetState();
  const nav = useNavigate();
  const { error, run } = useAction();
  const offline = device.status !== "online";
  const state = offline ? "unknown" : v.enabled ? v.state ?? "unknown" : "disabled";
  const master = state === "master";
  const aw = fleet.data?.devices[device.id]?.active_wan;
  const linked = wan.find((w) => w.slot === v.linked_wan_slot);
  const since = v.last_change_at ? `Seit ${fmtFull(v.last_change_at)} (${fmtSince(v.last_change_at)}).` : "";
  return (
    <div className={cls("flex flex-col gap-3.5 rounded-lg border px-[22px] py-5", master ? "border-orange bg-orange-bg" : "border-line bg-panel")}>
      <div className="flex items-center justify-between gap-2">
        <span className={cls("text-xs font-semibold uppercase tracking-[.05em]", master ? "text-orange-text" : "text-fg3")}>Aktuelle Rolle</span>
        <span className={cls("font-mono text-xs", master ? "text-orange-text" : "text-fg3")}>{v.name} · VRID {v.vrid}</span>
      </div>
      <div className="flex items-center gap-3.5">
        <Icon name={master ? "alert" : state === "backup" ? "pause" : "minusCircle"} className={cls("text-[40px]", master ? "text-orange" : "text-fg3")} />
        <div className="flex flex-col gap-0.5">
          <span className={cls("text-[40px] font-bold leading-none tracking-[-0.02em]", master ? "text-orange-text" : "text-fg")}>
            {master ? "Master" : state === "backup" ? "Backup" : state === "disabled" ? "Deaktiviert" : "Unbekannt"}
          </span>
          <span className="text-[15px] font-semibold text-fg">{master ? "Standort läuft über Backup" : state === "backup" ? "Hauptsystem aktiv" : offline ? "Gerät nicht erreichbar" : "Noch kein Status vom Router"}</span>
        </div>
      </div>
      <div className="text-pretty text-fg2">
        {master && <>{since} Das Hauptsystem sendet keine Advertisements mehr – dieser Router hält die virtuelle IP <span className="font-mono text-xs">{vip32(v.vip)}</span>.
          {aw && <> Datenverkehr läuft über <span className="font-mono text-xs">{aw.interface}</span> ({aw.name}).</>}
          {linked && <> Die Default-Route von WAN{linked.slot} ({linked.name}) ist deaktiviert, bis dessen Prüfung wieder „up“ meldet.</>}</>}
        {state === "backup" && <>{since} Das Hauptsystem hält die virtuelle IP <span className="font-mono text-xs">{vip32(v.vip)}</span>; dieser Router steht mit Priorität {v.priority} bereit.</>}
        {state === "unknown" && !offline && "Der Status wird mit der nächsten Abfrage gelesen."}
      </div>
      <ErrorBox error={error} />
      <div className="mt-auto flex flex-wrap gap-2">
        {master && <Button variant="secondary" icon="bell" onClick={() => nav("/alerts")}>Zugehöriger Alarm</Button>}
        {editable && simulator && v.id && !offline && v.enabled && (
          <Button variant="secondary" onClick={() => void run(async () => { await api.post(`/devices/${device.id}/vrrp/${v.id}/simulate?master=${!master}`); onChanged(); })}>{master ? "Backup werden (Simulator)" : "Master werden (Simulator)"}</Button>
        )}
      </div>
    </div>
  );
}

function InstanceFacts({ v, wan }: { v: VrrpInstance; wan: WanSlot[] }) {
  const linked = wan.find((w) => w.slot === v.linked_wan_slot);
  const kv: [string, string, boolean?][] = [
    ["Instanz", v.name, true], ["Interface", v.interface, true], ["Virtuelle IP (VIP)", vip32(v.vip), true], ["VRID", String(v.vrid), true],
    ["Priorität", `${v.priority}`], ["Preemption", v.preemption ? "Ja" : "Nein"], ["Advertisement-Intervall", `${interval(v.interval_ms)} · VRRPv${v.version}`],
    ["Lokale Adresse", v.local_address ?? "–", true], ["Verknüpftes WAN", linked ? `WAN${linked.slot} · ${linked.name} (${linked.interface})` : v.linked_wan_slot ? `WAN${v.linked_wan_slot}` : "–"],
  ];
  return (
    <Card title="Instanz" flush>
      <dl className="grid grid-cols-2">
        {kv.map(([k, val, mono]) => (
          <div key={k} className="flex min-w-0 flex-col gap-0.5 border-b border-line px-4 py-2.5">
            <dt className="text-xs text-fg3">{k}</dt>
            <dd className={cls("truncate font-medium", mono ? "font-mono text-[12.5px]" : "")}>{val}</dd>
          </div>
        ))}
      </dl>
    </Card>
  );
}

/** Gegenstelle (z. B. FortiGate): Erreichbarkeit per Ping von der lokalen Adresse aus. Priorität der Gegenstelle bewusst nicht angezeigt. */
function PeerCard({ v, device, editable, onChanged }: { v: VrrpInstance; device: Device; editable: boolean; onChanged: () => void }) {
  const { busy, error, run } = useAction();
  const offline = device.status !== "online";
  if (!v.peer_address) {
    return (
      <Card title="Gegenstelle">
        <p className="text-fg2">Keine Gegenstelle hinterlegt. Unter „Konfigurieren“ kann die echte IP des Hauptsystems eingetragen werden – sie wird bei jeder Abfrage angepingt.</p>
      </Card>
    );
  }
  const st = v.peer_checked_at == null || v.peer_reachable == null
    ? <Pill tone="gray" icon="minusCircle">Noch nicht geprüft</Pill>
    : v.peer_reachable ? <Pill tone="green" icon="checkCircle">Erreichbar</Pill> : <Pill tone="red" icon="xCircle">Nicht erreichbar</Pill>;
  return (
    <Card title="Gegenstelle" subtitle={v.peer_description ?? undefined}
      actions={editable && v.id && <Button size="sm" variant="secondary" icon={busy ? "loader" : "activity"} disabled={busy || offline}
        onClick={() => void run(async () => { await api.post(`/devices/${device.id}/vrrp/${v.id}/ping`); onChanged(); })}>{busy ? "Prüfe …" : "Peer prüfen"}</Button>}>
      <ErrorBox error={error} />
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
        <span className="font-mono text-[13px] font-medium">{v.peer_address}</span>
        {st}
        {v.peer_reachable && v.peer_rtt_ms != null && <span className="text-fg2">RTT {v.peer_rtt_ms.toLocaleString("de-DE")} ms</span>}
        {v.peer_checked_at && <span className="text-xs text-fg3" title={fmtFull(v.peer_checked_at)}>geprüft vor {fmtSince(v.peer_checked_at)}</span>}
      </div>
      <p className="mt-2 text-xs text-fg3">3 Pakete von {v.local_address ? <span className="font-mono">{v.local_address.split("/")[0]}</span> : "der lokalen Adresse"} aus, bei jeder Abfrage.</p>
    </Card>
  );
}

function History({ v, events }: { v: VrrpInstance; events: EventsResponse | null }) {
  if (!v.id) return null;
  const subject = `vrrp:${v.id}`;
  const now = Date.now();
  const start = now - DAYS * 86400e3;
  const evs = (events?.events ?? []).filter((e) => e.subject === subject); // neueste zuerst
  const asc = [...evs].reverse();
  const initial = events?.initial[subject]?.status;
  // Master-Abschnitte für die Zeitleiste
  const spans: [number, number][] = [];
  let cur: number | null = initial === "master" ? start : null;
  for (const e of asc) {
    const t = new Date(e.at).getTime();
    if (e.status === "master" && cur == null) cur = t;
    if (e.status !== "master" && cur != null) { spans.push([cur, t]); cur = null; }
  }
  if (cur != null) spans.push([cur, now]);
  const pos = (t: number) => ((t - start) / (now - start)) * 100;
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => new Date(start + f * (now - start)).toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit" }));
  const rows = evs.map((e, i) => {
    const prev = evs[i + 1]?.status ?? (i === evs.length - 1 ? initial : undefined);
    const toMaster = e.status === "master";
    const next = toMaster ? asc.find((x) => new Date(x.at) > new Date(e.at) && x.status !== "master") : undefined;
    const why = !prev ? "Erster erfasster Zustand"
      : toMaster ? "Keine Advertisements vom Hauptsystem empfangen"
        : v.preemption ? "Hauptsystem wieder aktiv (Preemption)" : "Hauptsystem wieder aktiv";
    const dur = toMaster ? (next ? fmtDuration((new Date(next.at).getTime() - new Date(e.at).getTime()) / 1000) : `${fmtSince(e.at)} · läuft`) : "—";
    return { e, prev, why, dur };
  });
  return (
    <Card flush title={`Rollenverlauf · ${v.name}`} subtitle={`letzte ${DAYS} Tage`} actions={<>
      <span className="flex items-center gap-1.5 text-xs text-fg2"><span className="h-2.5 w-2.5 rounded-sm border border-line-strong bg-sunken" />Backup</span>
      <span className="flex items-center gap-1.5 text-xs text-fg2"><span className="h-2.5 w-2.5 rounded-sm bg-orange" />Master</span>
    </>}>
      <div className="px-4 pb-2.5 pt-4">
        <div className="relative h-[22px] overflow-hidden rounded border border-line bg-sunken" role="img" aria-label={`${spans.length} Master-Phasen in den letzten ${DAYS} Tagen`}>
          {spans.map(([a, b], i) => (
            <div key={i} className="absolute inset-y-0 bg-orange" style={{ left: `${pos(a)}%`, width: `max(3px, ${pos(b) - pos(a)}%)` }} title={`Master ${fmtFull(new Date(a).toISOString())} – ${b >= now - 1000 ? "jetzt" : fmtFull(new Date(b).toISOString())}`} />
          ))}
        </div>
        <div className="mt-1.5 flex justify-between text-[11px] text-fg3">{ticks.map((t, i) => <span key={i}>{t}</span>)}</div>
      </div>
      {!events ? <Loading rows={2} /> : rows.length === 0 ? <EmptyState compact title="Keine Rollenwechsel im Zeitraum" /> : (
        <div className="overflow-x-auto">
          <div className="min-w-[640px]">
            <div className="grid grid-cols-[170px_190px_minmax(0,1fr)_120px] gap-3 border-y border-line bg-panel2 px-4 py-2 text-xs font-medium text-fg3"><span>Zeitpunkt</span><span>Wechsel</span><span>Ursache (abgeleitet)</span><span className="text-right">Master-Dauer</span></div>
            {rows.map(({ e, prev, why, dur }) => (
              <div key={e.at} className="grid h-[42px] grid-cols-[170px_190px_minmax(0,1fr)_120px] items-center gap-3 border-b border-line px-4 last:border-b-0">
                <span className="font-mono text-xs text-fg2">{fmtFull(e.at)}</span>
                <span className="flex items-center gap-1.5">{prev && <><RolePill state={prev} /><Icon name="arrowRight" className="text-[13px] text-fg3" /></>}<RolePill state={e.status} /></span>
                <span className="truncate text-fg2">{why}</span>
                <span className="text-right text-fg2">{dur}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </Card>
  );
}

export default function VrrpTab({ device }: { device: Device }) {
  const { can } = useAuth();
  const meta = useMeta();
  const vrrp = useFetch<VrrpConfig>(`/devices/${device.id}/vrrp`);
  const wan = useFetch<{ links: WanSlot[] }>(`/devices/${device.id}/wan`);
  const events = useFetch<EventsResponse>(`/devices/${device.id}/events?prefix=vrrp&days=${DAYS}`);
  const [editing, setEditing] = useState(false);
  useLive((e) => { if ((e.data as { device_id?: string }).device_id === device.id) { void vrrp.reload(); void events.reload(); } }, ["vrrp.state", "vrrp.peer", "wan.link"]);
  const editable = can("technician");
  if (!vrrp.data) return vrrp.error ? <ErrorBox error={vrrp.error} /> : <Loading rows={4} />;
  const slots = wan.data?.links ?? [];
  const reload = () => { void vrrp.reload(); void events.reload(); };
  return (
    <>
      <ErrorBox error={vrrp.data.last_error} />
      {vrrp.data.instances.length === 0 && !editing && (
        <Card><EmptyState icon="shield" title="Kein VRRP konfiguriert"
          text="Mit VRRP springt dieser Router als Backup ein, wenn das Hauptsystem (z. B. eine FortiGate) ausfällt – optional mit Umschalten auf eine Backup-Leitung."
          action={editable && <Button icon="plus" onClick={() => setEditing(true)}>VRRP einrichten</Button>} /></Card>
      )}
      {vrrp.data.instances.map((v) => (
        <div key={v.id} className="flex flex-col gap-4">
          <div className="grid items-stretch gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.25fr)]">
            <RoleCard v={v} device={device} wan={slots} simulator={!!meta?.simulator} editable={editable} onChanged={reload} />
            <div className="flex min-w-0 flex-col gap-4">
              <InstanceFacts v={v} wan={slots} />
              <PeerCard v={v} device={device} editable={editable} onChanged={reload} />
            </div>
          </div>
          <History v={v} events={events.data} />
        </div>
      ))}
      {editable && vrrp.data.instances.length > 0 && !editing && (
        <div className="flex gap-2">
          <Button variant="secondary" icon="settings" onClick={() => setEditing(true)}>Konfigurieren</Button>
          <Button variant="secondary" icon="rotate" onClick={() => void api.post(`/devices/${device.id}/vrrp/apply`).then(reload)}>Erneut anwenden</Button>
        </div>
      )}
      {editing && editable && <VrrpEditor device={device} initial={vrrp.data.instances} slots={slots} onSaved={reload} onClose={() => setEditing(false)} />}
    </>
  );
}

function VrrpEditor({ device, initial, slots, onSaved, onClose }: { device: Device; initial: VrrpInstance[]; slots: WanSlot[]; onSaved: () => void; onClose: () => void }) {
  const ifaces = useInterfaces(device.id);
  const [draft, setDraft] = useState<VrrpInstance[]>(() => (initial.length ? structuredClone(initial) : [blank(0)]));
  const { busy, error, run } = useAction();
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => ref.current?.scrollIntoView({ behavior: "smooth", block: "start" }), []);
  const set = (i: number, patch: Partial<VrrpInstance>) => setDraft(draft.map((x, j) => (j === i ? { ...x, ...patch } : x)));
  return (
    <div ref={ref}>
      <Card title="VRRP-Konfiguration" subtitle="Speichern überträgt die Instanzen auf den Router" actions={<Button size="sm" variant="ghost" icon="x" onClick={onClose}>Schließen</Button>}>
        <ErrorBox error={error} />
        <div className="space-y-3">
          {draft.map((v, i) => (
            <fieldset key={i} className="rounded-lg border border-line p-4">
              <legend className="px-1 font-semibold">VRRP-Instanz {i + 1}</legend>
              <div className="mb-3 flex justify-end gap-3">
                <Checkbox label="aktiv" checked={v.enabled} onChange={(c) => set(i, { enabled: c })} />
                <Button size="sm" variant="ghost" icon="trash" onClick={() => setDraft(draft.filter((_, j) => j !== i))}>Entfernen</Button>
              </div>
              <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
                <Input label="Name (VRRP-Interface)" value={v.name} onChange={(e) => set(i, { name: e.target.value })} className="font-mono" />
                <Select label="Interface" value={v.interface} onChange={(e) => set(i, { interface: e.target.value })}>
                  {!ifaces.data?.some((x) => x.name === v.interface) && <option value={v.interface}>{v.interface || "– wählen –"}</option>}
                  {ifaces.data?.filter((x) => x.type !== "vrrp").map((x) => <option key={x.name} value={x.name}>{ifaceLabel(x.name, x)}</option>)}
                </Select>
                <Input label="VRID" hint="1–255, wie beim Hauptsystem" type="number" min={1} max={255} value={v.vrid} onChange={(e) => set(i, { vrid: Number(e.target.value) })} />
                <Input label="Priorität" hint="1–254, kleiner als beim Master" type="number" min={1} max={254} value={v.priority} onChange={(e) => set(i, { priority: Number(e.target.value) })} />
                <Input label="Virtuelle IP (VIP)" hint="wird als /32 gesetzt" placeholder="192.168.110.1" value={v.vip} onChange={(e) => set(i, { vip: e.target.value })} className="font-mono" />
                <Input label="Lokale Adresse" hint="optional, mit Präfix" placeholder="192.168.110.21/24" value={v.local_address ?? ""} onChange={(e) => set(i, { local_address: e.target.value || null })} className="font-mono" />
                <Select label="Gekoppeltes WAN" hint="wird als Master abgeschaltet" value={v.linked_wan_slot ?? ""} onChange={(e) => set(i, { linked_wan_slot: e.target.value ? Number(e.target.value) : null })}>
                  <option value="">– keines –</option>
                  {slots.map((l) => <option key={l.slot} value={l.slot}>WAN{l.slot} – {l.name} ({l.interface})</option>)}
                </Select>
                <Input label="Gegenstelle (IP)" hint="optional, im Netz der lokalen Adresse" placeholder="192.168.110.2" value={v.peer_address ?? ""} onChange={(e) => set(i, { peer_address: e.target.value || null })} className="font-mono" />
                <Input label="Beschreibung Gegenstelle" hint="optional, z. B. FortiGate Zentrale" maxLength={100} value={v.peer_description ?? ""} onChange={(e) => set(i, { peer_description: e.target.value || null })} />
                <Input label="Intervall (ms)" type="number" min={10} value={v.interval_ms} onChange={(e) => set(i, { interval_ms: Number(e.target.value) })} />
                <Select label="VRRP-Version" value={v.version} onChange={(e) => set(i, { version: Number(e.target.value) })}>
                  <option value={3}>3 (Standard RouterOS 7)</option>
                  <option value={2}>2</option>
                </Select>
                <div className="pt-7"><Checkbox label="Preemption" checked={v.preemption} onChange={(c) => set(i, { preemption: c })} /></div>
              </div>
            </fieldset>
          ))}
        </div>
        <div className="mt-4 flex flex-wrap justify-between gap-2">
          <Button variant="secondary" icon="plus" onClick={() => setDraft([...draft, blank(draft.length)])}>VRRP-Instanz</Button>
          <Button disabled={busy} onClick={() => void run(async () => {
            const r = await api.put<{ instances: VrrpInstance[]; push: { ok: boolean; error?: string } | null }>(`/devices/${device.id}/vrrp`, { instances: draft, push: true });
            setDraft(structuredClone(r.instances));
            onSaved();
            if (r.push && !r.push.ok) throw new Error(`Gespeichert, aber Push fehlgeschlagen: ${r.push.error}`);
          })}>{busy ? "Übertrage …" : "Speichern & auf Router anwenden"}</Button>
        </div>
      </Card>
    </div>
  );
}
