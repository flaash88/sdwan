import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Icon, type IconName } from "../components/Icon";
import PairingBox from "../components/PairingBox";
import { DeviceStatusBadge } from "../components/fleet";
import { Button, Card, EmptyState, ErrorBox, Input, Loading, Pill, Select, StatusBadge, Tabs, cls, useAction, type Tone } from "../components/ui";
import { deviceTabs } from "../deviceTabs";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { useFleetState, useSites, type DeviceState } from "../lib/fleet";
import { fmtAgo, fmtBytes, fmtDate, fmtShort, fmtSince, fmtUptime } from "../lib/format";
import { useInterfaces } from "../lib/interfaces";
import { useLive } from "../lib/live";
import { useMeta } from "../lib/meta";
import type { Device, PairingInfo, Site } from "../lib/types";
import { useFetch } from "../lib/useFetch";

export interface DeviceEvent { subject: string; kind: "device" | "wan" | "wanactive" | "vrrp"; label: string; status: string; at: string }
export interface EventsResponse { since: string; events: DeviceEvent[]; initial: Record<string, { status: string; label: string }> }

/** Lesbarer Text für einen Statuswechsel (prev = vorheriger Zustand desselben Subjekts). */
export function eventText(e: DeviceEvent, prev?: string): { text: string; icon: IconName; tone: Tone } {
  if (e.kind === "device") return e.status === "online" ? { text: "Gerät wieder erreichbar", icon: "checkCircle", tone: "green" } : { text: "Gerät nicht erreichbar", icon: "xCircle", tone: "red" };
  if (e.kind === "wan") {
    if (e.status === "down") return { text: `${e.label}: Prüfziel nicht erreichbar (down)`, icon: "xCircle", tone: "red" };
    if (e.status === "degraded") return { text: `${e.label}: Latenz über Schwelle`, icon: "alert", tone: "orange" };
    return { text: `${e.label}: wieder up`, icon: "checkCircle", tone: "green" };
  }
  if (e.kind === "wanactive") return e.status === "active" ? { text: `${e.label} trägt die Default-Route`, icon: "arrowRight", tone: "blue" } : { text: `${e.label} nicht mehr aktiv`, icon: "pause", tone: "neutral" };
  const from = prev === "master" ? "Master" : prev === "backup" ? "Backup" : null;
  const to = e.status === "master" ? "Master" : "Backup";
  return { text: `VRRP ${e.label}: Rolle ${from ? `${from} → ` : ""}${to}`, icon: e.status === "master" ? "alert" : "pause", tone: e.status === "master" ? "orange" : "neutral" };
}

export default function DeviceDetail() {
  const { id } = useParams();
  const [params, setParams] = useSearchParams();
  const dev = useFetch<Device>(`/devices/${id}`);
  const fleet = useFleetState();
  const sites = useSites();
  useLive((e) => { if ((e.data as { id?: string }).id === id) void dev.reload(); }, ["device.status", "device.paired", "device.poll"]);
  const d = dev.data;
  if (!d) return dev.error ? <ErrorBox error={dev.error} /> : <Loading rows={5} />;
  const st = fleet.data?.devices[d.id];
  const paired = d.pairing_status === "paired";
  const tabs = [{ key: "overview", label: "Übersicht" }, ...deviceTabs.filter((t) => !t.pairedOnly || paired)];
  const tab = tabs.some((t) => t.key === params.get("tab")) ? params.get("tab")! : "overview";
  const setTab = (k: string) => setParams(k === "overview" ? {} : { tab: k }, { replace: true });
  const counts: Record<string, number | null> = { wan: st?.wan_links || null, vrrp: st?.vrrp.length || null };
  const Active = deviceTabs.find((t) => t.key === tab)?.component;
  return (
    <>
      <nav aria-label="Pfad" className="-mt-1 mb-3 flex items-center gap-1.5 text-xs text-fg3">
        <Link to="/devices">Geräte</Link><Icon name="chevRight" className="text-[12px]" /><span className="text-fg2">{d.name}</span>
      </nav>
      <DeviceHeader device={d} state={st} site={sites.data?.find((s) => s.id === d.site_id)} onTab={setTab}>
        <Tabs className="border-t border-line px-4" value={tab} onChange={setTab} tabs={tabs.map((t) => ({ key: t.key, label: t.label, count: counts[t.key] ?? null }))} />
      </DeviceHeader>
      <div className="mt-4 flex flex-col gap-4">
        {tab === "overview" ? <Overview device={d} state={st} reload={dev.reload} /> : Active ? <Active device={d} /> : null}
      </div>
    </>
  );
}

function DeviceHeader({ device: d, state, site, onTab, children }: { device: Device; state?: DeviceState; site?: Site; onTab: (t: string) => void; children: React.ReactNode }) {
  const { can } = useAuth();
  const [bk, setBk] = useState<0 | 1 | 2>(0);
  const { error, run } = useAction();
  const paired = d.pairing_status === "paired";
  const backupNow = () => void run(async () => { setBk(1); try { await api.post(`/devices/${d.id}/backups`); setBk(2); setTimeout(() => setBk(0), 3000); } catch (e) { setBk(0); throw e; } });
  const backupText = state?.active_wan?.backup ? `Standort läuft über ${state.active_wan.name}` : "Standort läuft über Backup (VRRP-Master)";
  return (
    <section className="rounded-lg border border-line bg-panel">
      <div className="flex flex-wrap items-start gap-4 px-5 py-[18px]">
        <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-[9px] border border-line bg-sunken text-[22px] text-fg2"><Icon name="router" /></div>
        <div className="flex min-w-0 flex-1 flex-col gap-1.5">
          <div className="flex flex-wrap items-center gap-2.5">
            <h1 className="text-xl font-semibold tracking-[-0.01em]">{d.name}</h1>
            <DeviceStatusBadge device={d} />
            {state?.on_backup && d.status === "online" && <Pill tone="orange" icon="alert">{backupText}{state.backup_since ? ` · seit ${fmtSince(state.backup_since)}` : ""}</Pill>}
          </div>
          <div className="flex flex-wrap items-center gap-x-3.5 gap-y-1 text-fg2">
            {site && <span className="flex items-center gap-[5px]"><Icon name="pin" className="text-[13px] text-fg3" />{site.name}</span>}
            {d.model && <span>{d.model}</span>}
            {d.routeros_version && <span>RouterOS <span className="font-mono text-xs">{d.routeros_version}</span></span>}
            <span className="font-mono text-xs" title="Management-Tunnel-IP">{d.tunnel_ip}</span>
            {d.serial && <span>SN <span className="font-mono text-xs">{d.serial}</span></span>}
            {d.uptime && d.status === "online" && <span>Uptime {fmtUptime(d.uptime)}</span>}
            {d.status !== "online" && paired && <span className="text-red-text">zuletzt gesehen {fmtAgo(d.last_seen_at)}</span>}
          </div>
        </div>
        {paired && can("technician") && (
          <div className="flex flex-wrap gap-2">
            <Button icon="terminal" onClick={() => onTab("remote")}>Fernzugriff starten</Button>
            <Button variant="secondary" icon={bk === 2 ? "check" : "archive"} disabled={bk === 1 || d.status !== "online"} onClick={backupNow}>
              {bk === 1 ? "Backup läuft …" : bk === 2 ? "Backup erstellt" : "Backup jetzt"}
            </Button>
          </div>
        )}
      </div>
      {error && <div className="px-5 pb-3"><ErrorBox error={error} /></div>}
      {children}
    </section>
  );
}

function Tile({ label, value, sub }: { label: string; value: React.ReactNode; sub?: React.ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-1 rounded-lg border border-line bg-panel px-4 py-3.5">
      <span className="text-xs font-medium text-fg2">{label}</span>
      <span className="truncate text-[22px] font-semibold">{value}</span>
      {sub && <span className="truncate text-xs text-fg3">{sub}</span>}
    </div>
  );
}

function Overview({ device: d, state, reload }: { device: Device; state?: DeviceState; reload: () => Promise<void> }) {
  const paired = d.pairing_status === "paired";
  const online = d.status === "online";
  const f = d.facts as Record<string, number | undefined>;
  const ifaces = useInterfaces(paired ? d.id : null);
  const wan = useFetch<{ links: { slot: number; name: string; interface: string }[] }>(paired ? `/devices/${d.id}/wan` : null);
  const events = useFetch<EventsResponse>(paired ? `/devices/${d.id}/events?days=30&limit=40` : null);
  useLive((e) => { if ((e.data as { device_id?: string; id?: string }).device_id === d.id || (e.data as { id?: string }).id === d.id) void events.reload(); }, ["wan.link", "vrrp.state", "device.status"]);
  const wanOf = (n: string) => wan.data?.links.find((l) => l.interface === n);
  const aw = state?.active_wan;
  const evs = events.data?.events ?? [];
  const prevOf = (i: number) => evs.slice(i + 1).find((x) => x.subject === evs[i].subject)?.status ?? events.data?.initial[evs[i].subject]?.status;
  return (
    <>
      {paired && (
        <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
          <Tile label="CPU" value={online && f.cpu_load != null ? `${f.cpu_load} %` : "–"} sub={f.cpu_count ? `${f.cpu_count} ${f.cpu_count === 1 ? "Kern" : "Kerne"}${d.architecture ? ` · ${d.architecture}` : ""}` : d.architecture ?? undefined} />
          <Tile label="Arbeitsspeicher" value={online && f.total_memory ? fmtBytes((f.total_memory ?? 0) - (f.free_memory ?? 0)) : "–"} sub={f.total_memory ? `von ${fmtBytes(f.total_memory)}` : undefined} />
          <Tile label="Latenz zur Cloud" value={online && f.mgmt_rtt_ms != null ? `${Math.round(f.mgmt_rtt_ms)} ms` : "–"} sub="über den Management-Tunnel" />
          <Tile label="Aktiver WAN" value={online && aw ? aw.name : "–"} sub={online && aw ? `${aw.interface}${aw.backup ? " · Backup-Leitung" : ""}${state?.backup_since && aw.backup ? ` · seit ${fmtSince(state.backup_since)}` : ""}` : state?.wan_links ? "unbekannt" : "kein WAN verwaltet"} />
        </div>
      )}
      {paired && (
        <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)]">
          <Card title="Schnittstellen" subtitle={online ? "live" : "Stand letzter Abfrage"} flush>
            {!ifaces.data ? <Loading rows={3} /> : ifaces.data.length === 0 ? <EmptyState compact title="Keine Schnittstellen gemeldet" /> : ifaces.data.map((i) => {
              const w = wanOf(i.name);
              return (
                <div key={i.name} className="grid h-11 grid-cols-[110px_minmax(0,1fr)_90px_110px] items-center gap-3 border-b border-line px-4 last:border-b-0">
                  <span className="truncate font-mono text-xs font-medium">{i.name}</span>
                  <span className="truncate text-fg2">{[w && `WAN${w.slot} · ${w.name}`, i.comment, i.default_name && i.default_name !== i.name && `(${i.default_name})`].filter(Boolean).join(" · ") || "–"}</span>
                  <span className="text-xs text-fg3">{i.type ?? ""}</span>
                  <span>{i.disabled ? <StatusBadge status="disabled" /> : <StatusBadge status={i.running ? "up" : "down"} label={i.running ? "Up" : "Kein Link"} />}</span>
                </div>
              );
            })}
          </Card>
          <Card title="Letzte Ereignisse" subtitle="Statuswechsel der letzten 30 Tage" flush>
            {!events.data ? <Loading rows={3} /> : evs.length === 0 ? <EmptyState compact title="Keine Statuswechsel im Zeitraum" /> : evs.slice(0, 10).map((e, i) => {
              const t = eventText(e, prevOf(i));
              const col: Record<Tone, string> = { green: "text-green-text", red: "text-red-text", orange: "text-orange-text", blue: "text-blue-text", gray: "text-fg3", neutral: "text-fg3" };
              return (
                <div key={e.subject + e.at} className="flex gap-3 border-b border-line px-4 py-2.5 last:border-b-0">
                  <span className="shrink-0 pt-px font-mono text-xs text-fg3" title={fmtDate(e.at)}>{fmtShort(e.at)}</span>
                  <Icon name={t.icon} className={cls("mt-0.5 text-[14px]", col[t.tone])} />
                  <span>{t.text}</span>
                </div>
              );
            })}
          </Card>
        </div>
      )}
      <DeviceAdmin device={d} reload={reload} />
    </>
  );
}

function DeviceAdmin({ device: d, reload }: { device: Device; reload: () => Promise<void> }) {
  const { can } = useAuth();
  const meta = useMeta();
  const nav = useNavigate();
  const sites = useSites();
  const [pairing, setPairing] = useState<PairingInfo | null>(null);
  const [name, setName] = useState(d.name);
  const [siteId, setSiteId] = useState(d.site_id ?? "");
  const [tags, setTags] = useState(d.tags.join(", "));
  const [meshEp, setMeshEp] = useState(d.mesh_endpoint ?? "");
  const { busy, error, run } = useAction();
  const rows: [string, string, boolean?][] = [
    ["Identity", d.identity ?? "–"], ["Seriennummer", d.serial ?? "–", true], ["RouterOS", d.routeros_version ?? "–", true],
    ["Architektur", d.architecture ?? "–"], ["Tunnel-IP", d.tunnel_ip, true], ["Mesh-IP", d.mesh_ip ?? "–", true],
    ["WG-Public-Key", d.wg_public_key ?? "–", true], ["Gepairt", fmtDate(d.paired_at)], ["Letzter API-Kontakt", fmtAgo(d.last_seen_at)],
    ["Letzter WG-Handshake", fmtAgo(d.last_handshake_at)],
  ];
  return (
    <div className="grid items-start gap-4 xl:grid-cols-2">
      <Card title="Systeminformationen" flush>
        <dl className="grid grid-cols-2">
          {rows.map(([k, v, mono]) => (
            <div key={k} className="flex min-w-0 flex-col gap-0.5 border-b border-line px-4 py-2.5 [&:nth-last-child(-n+2)]:border-b-0">
              <dt className="text-xs text-fg3">{k}</dt>
              <dd className={cls("break-all font-medium", mono && "font-mono text-[12.5px]")}>{v}</dd>
            </div>
          ))}
        </dl>
      </Card>
      <div className="flex flex-col gap-4">
        {(d.pairing_status !== "paired" || pairing) && can("technician") && (
          <Card title="Onboarding">
            <ErrorBox error={error} />
            {pairing ? <PairingBox pairing={pairing} /> : (
              <div className="space-y-3">
                <p className="text-fg2">Dieses Gerät ist noch nicht verbunden. Erzeuge einen neuen Onboarding-Befehl:</p>
                <div className="flex flex-wrap gap-2">
                  <Button disabled={busy} onClick={() => void run(async () => setPairing(await api.post<PairingInfo>(`/devices/${d.id}/pairing-token`)))}>Onboarding-Befehl erzeugen</Button>
                  {meta?.simulator && d.pairing_status === "pending" && (
                    <Button variant="secondary" disabled={busy} onClick={() => void run(async () => { await api.post(`/devices/${d.id}/simulate-pair`); await reload(); })}>Pairing simulieren</Button>
                  )}
                </div>
              </div>
            )}
          </Card>
        )}
        {d.ztp_state !== "none" && (
          <Card title="Zero-Touch" actions={<StatusBadge status={d.ztp_state} />} flush>
            {d.ztp_log.length === 0 ? <EmptyState compact title="Noch keine Einträge" /> : d.ztp_log.map((l, i) => (
              <div key={i} className="flex gap-3 border-b border-line px-4 py-2 last:border-b-0"><span className="shrink-0 font-mono text-xs text-fg3">{fmtDate(l.at)}</span><span>{l.msg}</span></div>
            ))}
          </Card>
        )}
        {can("technician") && (
          <Card title="Einstellungen">
            <ErrorBox error={pairing ? null : error} />
            <form className="grid gap-3 sm:grid-cols-2" onSubmit={(e) => { e.preventDefault(); void run(async () => { await api.patch(`/devices/${d.id}`, { name, site_id: siteId || null, mesh_endpoint: meshEp || null, tags: tags.split(",").map((t) => t.trim()).filter(Boolean) }); await reload(); }); }}>
              <Input label="Name" value={name} onChange={(e) => setName(e.target.value)} />
              <Select label="Standort" value={siteId} onChange={(e) => setSiteId(e.target.value)}>
                <option value="">– kein Standort –</option>
                {sites.data?.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
              </Select>
              <Input label="Tags" hint="Kommagetrennt" value={tags} onChange={(e) => setTags(e.target.value)} />
              <Input label="Öffentlicher Mesh-Endpoint" hint="Hostname/IP, leer = automatisch erkannt" value={meshEp} onChange={(e) => setMeshEp(e.target.value)} className="font-mono" />
              <div className="flex flex-wrap justify-between gap-2 sm:col-span-2">
                <Button disabled={busy}>Speichern</Button>
                {can("admin") && (
                  <div className="flex gap-2">
                    {d.pairing_status === "paired" && <Button type="button" variant="danger-outline" onClick={() => confirm("Gerät sperren? Der Tunnel wird getrennt.") && void run(async () => { await api.post(`/devices/${d.id}/revoke`); await reload(); })}>Sperren</Button>}
                    <Button type="button" variant="danger" icon="trash" onClick={() => confirm("Gerät endgültig löschen?") && void run(async () => { await api.del(`/devices/${d.id}`); nav("/devices"); })}>Löschen</Button>
                  </div>
                )}
              </div>
            </form>
          </Card>
        )}
      </div>
    </div>
  );
}
