import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Icon } from "../components/Icon";
import { WanPill } from "../components/fleet";
import { EmptyState, KpiTile, Loading, PageHeader, SeverityBadge, StatusDot, cls, type Tone } from "../components/ui";
import { useAuth } from "../lib/auth";
import { alarmState, alertTitle, firmwareUpdate, type DeviceState, useDevices, useFleetState, useOpenAlerts, useSites } from "../lib/fleet";
import { fmtShort, fmtSince } from "../lib/format";
import type { Device, Site } from "../lib/types";

const SEV_ORDER: Record<string, number> = { critical: 0, warning: 1, info: 2 };
const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

export default function Dashboard() {
  const { me } = useAuth();
  const nav = useNavigate();
  const devices = useDevices();
  const sites = useSites();
  const fleet = useFleetState();
  const alerts = useOpenAlerts();
  const [updated, setUpdated] = useState(new Date());
  useEffect(() => setUpdated(new Date()), [devices.data, fleet.data, alerts.data]);

  const paired = useMemo(() => devices.data?.filter((d) => d.pairing_status === "paired") ?? [], [devices.data]);
  const siteName = (id: string | null) => sites.data?.find((s) => s.id === id)?.name ?? "–";
  const tenant = me?.tenants.find((t) => t.id === me.active_tenant_id)?.name ?? (me?.user.is_superuser ? "Alle Mandanten" : me?.tenants[0]?.name);

  if (!devices.data || !sites.data) return <><PageHeader title="Dashboard" /><Loading rows={4} /></>;

  const online = paired.filter((d) => d.status === "online");
  const offline = paired.filter((d) => d.status === "offline");
  const state = fleet.data?.devices ?? {};
  const onBackup = paired.filter((d) => state[d.id]?.on_backup);
  const updates = paired.filter((d) => firmwareUpdate(d)?.update_available);
  const targets = [...new Set(updates.map((d) => firmwareUpdate(d)!.latest))];
  const openList = (alerts.data ?? []).filter((a) => ["active", "ack"].includes(alarmState(a)))
    .sort((a, b) => (SEV_ORDER[a.severity] ?? 3) - (SEV_ORDER[b.severity] ?? 3) || b.started_at.localeCompare(a.started_at));
  const active = openList.filter((a) => alarmState(a) === "active");
  const bySev = (s: string) => active.filter((a) => a.severity === s).length;
  const devById = new Map(devices.data.map((d) => [d.id, d]));
  const firstOffline = [...offline].sort((a, b) => (a.last_seen_at ?? "").localeCompare(b.last_seen_at ?? ""))[0];
  const firstBackup = onBackup[0];

  return (
    <>
      <PageHeader
        title="Dashboard"
        subtitle={`${tenant ?? ""} · ${plural(sites.data.length, "Standort", "Standorte")} · ${plural(paired.length, "Gerät", "Geräte")}`}
        actions={<span className="flex items-center gap-1.5 text-xs text-fg3"><Icon name="rotate" className="text-[13px]" />Aktualisiert {updated.toLocaleString("de-DE", { dateStyle: "short", timeStyle: "medium" })}</span>}
      />
      <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-3 2xl:grid-cols-6">
        <KpiTile label="Geräte gesamt" icon="router" value={paired.length} sub={plural(sites.data.length, "Standort", "Standorte")} subIcon="pin" onClick={() => nav("/devices")} />
        <KpiTile label="Online" icon="checkCircle" value={online.length} unit={`von ${paired.length}`} onClick={() => nav("/devices?status=online")}
          sub={offline.length ? plural(offline.length, "Gerät nicht erreichbar", "Geräte nicht erreichbar") : "Alle Geräte erreichbar"} subIcon={offline.length ? "alert" : "checkCircle"} tone={offline.length ? undefined : "green"} />
        <KpiTile label="Offline" icon="xCircle" value={offline.length} valueTone={offline.length ? "red" : undefined} onClick={() => nav("/devices?status=offline")}
          sub={firstOffline ? `${firstOffline.name} · ${fmtSince(firstOffline.last_seen_at)}` : "Keine Ausfälle"} subIcon={firstOffline ? "xCircle" : "checkCircle"} tone={firstOffline ? "red" : "green"} />
        <KpiTile label="Offene Alarme" icon="bell" value={active.length} onClick={() => nav("/alerts")}
          sub={active.length ? `${bySev("critical")} kritisch · ${bySev("warning")} Warnung · ${bySev("info")} Info` : "Keine offenen Alarme"} subIcon={active.length ? "octagon" : "checkCircle"} tone={active.length ? undefined : "green"} />
        <KpiTile label="Standorte auf Backup" icon="signal" value={fleet.data?.sites_on_backup ?? "–"} unit={fleet.data ? `von ${fleet.data.sites_total}` : undefined}
          valueTone={onBackup.length ? "orange" : undefined} tone={onBackup.length ? "orange" : undefined} subIcon={onBackup.length ? "alert" : "checkCircle"}
          sub={firstBackup ? `${siteName(firstBackup.site_id)} ${state[firstBackup.id].active_wan?.backup ? `über ${state[firstBackup.id].active_wan!.name}` : "VRRP-Master"} seit ${fmtSince(state[firstBackup.id].backup_since)}` : "Alle auf Primärleitung"}
          onClick={firstBackup ? () => nav(`/devices/${firstBackup.id}`) : undefined} />
        <KpiTile label="Firmware-Updates" icon="cpu" value={updates.length} unit="ausstehend" onClick={() => nav("/firmware")}
          sub={updates.length ? `Ziel: RouterOS ${targets.join(", ")}` : "Alle Geräte aktuell"} subIcon={updates.length ? "upload" : "checkCircle"} />
      </div>

      <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1.65fr)_minmax(0,1fr)]">
        <section className="overflow-hidden rounded-lg border border-line bg-panel">
          <header className="flex items-center gap-2 border-b border-line px-4 py-3">
            <span className="font-semibold">Aktuelle Alarme</span>
            <span className="text-xs text-fg3">{active.length} offen{openList.length > active.length ? ` · ${openList.length - active.length} quittiert` : ""}</span>
            <div className="flex-1" />
            <Link to="/alerts" className="flex items-center gap-1 font-medium">Alle Alarme<Icon name="arrowRight" className="text-[14px]" /></Link>
          </header>
          {openList.length === 0 ? <EmptyState compact icon="checkCircle" title="Keine offenen Alarme" /> : (
            <div className="overflow-x-auto">
              <div className="min-w-[640px]">
                <div className="grid grid-cols-[104px_minmax(0,1fr)_110px_150px_90px] gap-3 border-b border-line bg-panel2 px-4 py-2 text-xs font-medium text-fg3">
                  <span>Schweregrad</span><span>Alarm</span><span>Standort</span><span>Gerät</span><span className="text-right">Dauer</span>
                </div>
                {openList.slice(0, 8).map((a) => {
                  const d = a.device_id ? devById.get(a.device_id) : undefined;
                  const ack = alarmState(a) === "ack";
                  return (
                    <Link key={a.id} to={a.device_id ? `/devices/${a.device_id}` : "/alerts"} className="grid h-11 grid-cols-[104px_minmax(0,1fr)_110px_150px_90px] items-center gap-3 border-b border-line px-4 text-fg last:border-b-0 hover:bg-hover hover:no-underline">
                      <span><SeverityBadge severity={a.severity} /></span>
                      <span className="flex min-w-0 flex-col leading-tight"><span className="truncate font-medium" title={a.message}>{alertTitle(a)}</span><span className="text-[11.5px] text-fg3">{ack ? `Quittiert · ${a.acknowledged_by}` : `Aktiv seit ${fmtShort(a.fired_at ?? a.started_at)}`}</span></span>
                      <span className="truncate">{d ? siteName(d.site_id) : "–"}</span>
                      <span className="truncate font-mono text-xs text-fg2">{a.device ?? "–"}</span>
                      <span className="text-right text-fg2">{fmtSince(a.started_at)}</span>
                    </Link>
                  );
                })}
              </div>
            </div>
          )}
        </section>

        <SiteList devices={paired} sites={sites.data} state={state} />
      </div>
    </>
  );
}

function SiteList({ devices, sites, state }: { devices: Device[]; sites: Site[]; state: Record<string, DeviceState> }) {
  const rows = sites.map((s) => {
    const devs = devices.filter((d) => d.site_id === s.id);
    const off = devs.filter((d) => d.status !== "online");
    const backup = devs.find((d) => state[d.id]?.on_backup);
    const lead = backup ?? devs.find((d) => d.status === "online") ?? devs[0];
    const tone: Tone = !devs.length ? "gray" : off.length === devs.length ? "red" : backup ? "orange" : off.length ? "orange" : "green";
    const text = !devs.length ? "Keine Geräte" : off.length === devs.length ? "Offline" : backup ? "Über Backup-WAN" : off.length ? `${off.length} offline` : "Online";
    return { s, devs, lead, tone, text, allOff: devs.length > 0 && off.length === devs.length };
  });
  return (
    <section className="overflow-hidden rounded-lg border border-line bg-panel">
      <header className="flex items-center gap-2 border-b border-line px-4 py-3">
        <span className="font-semibold">Standorte</span><span className="text-xs text-fg3">{rows.length}</span>
        <div className="flex-1" /><span className="text-xs text-fg3">Aktiver WAN · Latenz</span>
      </header>
      {rows.length === 0 && <EmptyState compact title="Noch keine Standorte" />}
      {rows.map(({ s, devs, lead, tone, text, allOff }) => {
        const st = lead ? state[lead.id] : undefined;
        const lat = st?.active_wan?.latency_ms;
        return (
          <Link key={s.id} to={lead ? `/devices/${lead.id}` : "/sites"} className="flex h-12 items-center gap-3 border-b border-line px-4 text-fg last:border-b-0 hover:bg-hover hover:no-underline">
            <StatusDot tone={tone} />
            <span className="flex min-w-0 flex-1 flex-col leading-tight">
              <span className="truncate font-medium">{s.name}</span>
              <span className={cls("text-[11.5px]", tone === "green" || tone === "gray" ? "text-fg3" : tone === "red" ? "text-red-text" : "text-orange-text")}>{devs.length} {devs.length === 1 ? "Gerät" : "Geräte"} · {text}</span>
            </span>
            {devs.length > 0 && <WanPill state={st} offline={allOff} />}
            <span className="w-12 text-right text-fg2">{lat != null && !allOff ? `${Math.round(lat)} ms` : "–"}</span>
          </Link>
        );
      })}
    </section>
  );
}
