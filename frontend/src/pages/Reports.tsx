import { useState } from "react";
import { Button, Card, Checkbox, ErrorBox, Input, PageHeader, Stat, Table, useAction, EmptyState } from "../components/ui";
import { api, download } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtDate } from "../lib/format";
import { useFetch } from "../lib/useFetch";

interface DevRow { device_id: string; device: string; site: string; availability_pct: number | null; downtime_s: number; outage_count: number; longest_outage_s: number; mttr_s: number; has_backup_wan?: boolean; has_vrrp?: boolean; backup_wan_s?: number; backup_wan_count?: number; vrrp_master_s?: number; vrrp_master_count?: number; wan: { name: string; availability_pct: number | null; downtime_s: number; outage_count: number }[] }
interface Rep { fleet_availability_pct: number | null; device_count: number; alert_count: number; alerts_by_severity: Record<string, number>; devices: DevRow[] }
interface Stored { settings: { monthly_report: boolean; report_recipients: string[]; contact_email: string | null }; reports: { id: string; period_start: string; period_end: string; created_at: string; fleet_availability_pct: number | null; sent_to: string[] }[] }

const iso = (d: Date) => d.toISOString().slice(0, 10);
const dur = (s: number) => (s < 3600 ? `${Math.round(s / 60)} min` : `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`);
const pct = (v: number | null) => (v == null ? "–" : `${v.toFixed(3)} %`);

export default function Reports() {
  const { can, me } = useAuth();
  const now = new Date();
  const [start, setStart] = useState(iso(new Date(now.getFullYear(), now.getMonth(), 1)));
  const [end, setEnd] = useState(iso(now));
  const q = `start=${start}&end=${end}`;
  const rep = useFetch<Rep>(me?.active_tenant_id ? `/reports/sla?${q}` : null);
  const stored = useFetch<Stored>(me?.active_tenant_id ? "/reports" : null);
  const [rcpt, setRcpt] = useState<string | null>(null);
  const { busy, error, run } = useAction();
  if (!me?.active_tenant_id) return <><PageHeader title="SLA-Berichte" /><Card><EmptyState icon="building" title="Bitte einen Mandanten wählen" text="Diese Ansicht gilt je Mandant – oben links auswählen." /></Card></>;
  const r = rep.data;
  const st = stored.data?.settings;
  return (
    <>
      <PageHeader title="SLA-Berichte" subtitle="Verfügbarkeit je Gerät und WAN-Link, berechnet aus allen Statuswechseln"
        actions={<>
          <Input type="date" aria-label="Von" value={start} onChange={(e) => setStart(e.target.value)} className="w-[150px]" />
          <span className="text-fg3">bis</span><Input type="date" aria-label="Bis" value={end} onChange={(e) => setEnd(e.target.value)} className="w-[150px]" />
          <Button variant="secondary" icon="download" onClick={() => void download(`/reports/sla?${q}&format=pdf`, `sla-${start}-${end}.pdf`)}>PDF</Button>
          {can("technician") && <Button disabled={busy} onClick={() => void run(async () => { await api.post(`/reports?${q}&send=true`); await stored.reload(); })}>Speichern & versenden</Button>}
        </>} />
      <ErrorBox error={error ?? rep.error} />
      <div className="mb-6 grid grid-cols-2 gap-4 md:grid-cols-4">
        <Stat label="Gesamtverfügbarkeit" value={pct(r?.fleet_availability_pct ?? null)} tone={r?.fleet_availability_pct != null ? (r.fleet_availability_pct >= 99.9 ? "green" : r.fleet_availability_pct >= 99 ? "yellow" : "red") : undefined} />
        <Stat label="Geräte" value={r?.device_count ?? "–"} />
        <Stat label="Alarme" value={r?.alert_count ?? "–"} sub={r && `${r.alerts_by_severity.critical ?? 0} kritisch`} />
        <Stat label="Ausfälle gesamt" value={r ? r.devices.reduce((a, d) => a + d.outage_count, 0) : "–"} />
      </div>
      <Card title="Verfügbarkeit je Gerät">
        <Table head={["Gerät", "Standort", "Verfügbarkeit", "Ausfallzeit", "Ausfälle", "Längster", "MTTR", "Auf Backup-WAN", "VRRP-Master", "WAN-Links"]} empty={r?.devices.length === 0}>
          {r?.devices.map((d) => (
            <tr key={d.device_id}>
              <td className="px-3 py-2 font-medium">{d.device}</td>
              <td className="px-3 py-2">{d.site}</td>
              <td className={`px-3 py-2 font-mono ${d.availability_pct != null && d.availability_pct < 99 ? "text-red-600" : ""}`}>{pct(d.availability_pct)}</td>
              <td className="px-3 py-2">{dur(d.downtime_s)}</td>
              <td className="px-3 py-2">{d.outage_count}</td>
              <td className="px-3 py-2">{d.outage_count ? dur(d.longest_outage_s) : "–"}</td>
              <td className="px-3 py-2">{d.outage_count ? dur(d.mttr_s) : "–"}</td>
              <td className="px-3 py-2">{d.has_backup_wan ? `${dur(d.backup_wan_s ?? 0)} (${d.backup_wan_count ?? 0}×)` : "–"}</td>
              <td className="px-3 py-2">{d.has_vrrp ? `${dur(d.vrrp_master_s ?? 0)} (${d.vrrp_master_count ?? 0}×)` : "–"}</td>
              <td className="px-3 py-2 text-xs">{d.wan.map((w) => `${w.name}: ${pct(w.availability_pct)}`).join(" · ") || "–"}</td>
            </tr>
          ))}
        </Table>
      </Card>
      <div className="mt-6 grid gap-4 xl:grid-cols-2">
        <Card title="Gespeicherte Berichte">
          <Table head={["Zeitraum", "Verfügbarkeit", "Versendet an", ""]} empty={stored.data?.reports.length === 0}>
            {stored.data?.reports.map((x) => (
              <tr key={x.id}>
                <td className="px-3 py-2">{fmtDate(x.period_start).split(",")[0]} – {fmtDate(x.period_end).split(",")[0]}</td>
                <td className="px-3 py-2 font-mono">{pct(x.fleet_availability_pct)}</td>
                <td className="px-3 py-2 text-xs">{x.sent_to.join(", ") || "–"}</td>
                <td className="px-3 py-2 text-right"><Button variant="ghost" onClick={() => void download(`/reports/${x.id}/pdf`, `sla-${x.period_start.slice(0, 10)}.pdf`)}>PDF</Button></td>
              </tr>
            ))}
          </Table>
        </Card>
        {st && can("admin") && (
          <Card title="Automatischer Monatsbericht">
            <div className="space-y-3">
              <Checkbox label="Am 1. jedes Monats den Vormonatsbericht als PDF versenden" checked={st.monthly_report} onChange={(v) => void run(async () => { await api.put("/reports/settings", { monthly_report: v, report_recipients: st.report_recipients }); await stored.reload(); })} />
              <Input label={`Empfänger (zusätzlich zum Kontakt ${st.contact_email ?? "–"})`} value={rcpt ?? st.report_recipients.join(", ")} onChange={(e) => setRcpt(e.target.value)} />
              <Button variant="secondary" onClick={() => void run(async () => { await api.put("/reports/settings", { monthly_report: st.monthly_report, report_recipients: (rcpt ?? "").split(/[\s,;]+/).filter(Boolean) }); setRcpt(null); await stored.reload(); })}>Speichern</Button>
            </div>
          </Card>
        )}
      </div>
    </>
  );
}
