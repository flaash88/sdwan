import { useState } from "react";
import { Link } from "react-router-dom";
import { Badge, Button, Card, Checkbox, ErrorBox, Input, Modal, PageHeader, Select, Table, cls, useAction } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtAgo, fmtDate } from "../lib/format";
import { useLive } from "../lib/live";
import type { Device, Site } from "../lib/types";
import { useFetch } from "../lib/useFetch";
import { useMeta } from "../lib/meta";

export interface AlertItem { fires_at: string | null; id: string; device_id: string | null; device: string | null; status: string; severity: string; message: string; started_at: string; fired_at: string | null; resolved_at: string | null; notified: boolean; acknowledged_by: string | null }
interface Rule { id?: string; name: string; type: string; type_label?: string; severity: string; params: { threshold?: number; metric?: string }; duration_s: number; site_ids: string[]; device_ids: string[]; recipients: string[]; notify_resolved: boolean; enabled: boolean }

export const sevColor = (s: string) => (s === "critical" ? "red" : s === "warning" ? "yellow" : "blue");

export default function Alerts() {
  const { can, me } = useAuth();
  const tenant = me?.active_tenant_id;
  const [state, setState] = useState<"open" | "all">("open");
  const alerts = useFetch<AlertItem[]>(`/alerts?state=${state}`);
  const rules = useFetch<Rule[]>(tenant ? "/alert-rules" : null);
  const types = useFetch<Record<string, string>>("/alert-rules/types");
  const [edit, setEdit] = useState<Rule | null>(null);
  const { busy, error, run } = useAction();
  const meta = useMeta();
  useLive(() => void alerts.reload(), ["alert.firing", "alert.resolved", "device.status"]);
  return (
    <>
      <PageHeader title="Alarme" subtitle="Regelbasierte Überwachung mit E-Mail-Benachrichtigung"
        actions={can("technician") && tenant && <Button variant="secondary" disabled={busy} onClick={() => void run(async () => { await api.post("/alerts/evaluate"); await alerts.reload(); })}>Jetzt auswerten</Button>} />
      <ErrorBox error={error} />
      {meta && !meta.smtp_configured && (
        <div className="mb-4 rounded-lg border border-amber-200 bg-amber-50 px-4 py-2 text-sm text-amber-800">
          <b>E-Mail-Versand ist nicht eingerichtet</b> – Alarme erscheinen hier und als Hinweis im Dashboard, es werden aber keine Mails verschickt. SMTP-Zugangsdaten (<code>SMTP_HOST</code>, <code>SMTP_USER</code>, …) in <code>/opt/sdwan/.env</code> eintragen und <code>docker compose up -d</code> ausführen.
        </div>
      )}
      <Card title={<div className="flex gap-2">{(["open", "all"] as const).map((s) => <button key={s} onClick={() => setState(s)} className={cls("rounded px-2 py-1 text-sm", state === s ? "bg-brand-100 text-brand-800" : "text-slate-500")}>{s === "open" ? "Aktiv" : "Verlauf"}</button>)}</div>}>
        <Table head={["Schwere", "Meldung", "Gerät", "Seit", "Ende", "Benachrichtigt", ""]} empty={alerts.data?.length === 0}>
          {alerts.data?.map((a) => (
            <tr key={a.id} className={a.status === "firing" ? "" : "text-slate-500"}>
              <td className="px-3 py-2"><Badge color={a.status === "resolved" ? "green" : a.status === "pending" ? "gray" : sevColor(a.severity)}>{a.status === "resolved" ? "behoben" : a.status === "pending" ? "ausstehend" : a.severity}</Badge>{a.status === "pending" && a.fires_at && <div className="mt-1 text-xs text-slate-500">löst aus {new Date(a.fires_at) > new Date() ? `um ${new Date(a.fires_at).toLocaleTimeString("de-DE")}` : "gleich"}</div>}</td>
              <td className="px-3 py-2">{a.message}</td>
              <td className="px-3 py-2">{a.device_id ? <Link className="text-brand-700 hover:underline" to={`/devices/${a.device_id}`}>{a.device}</Link> : "–"}</td>
              <td className="px-3 py-2">{fmtDate(a.fired_at ?? a.started_at)}<div className="text-xs">{fmtAgo(a.started_at)}</div></td>
              <td className="px-3 py-2">{fmtDate(a.resolved_at)}</td>
              <td className="px-3 py-2">{a.notified ? "✓" : "–"}</td>
              <td className="px-3 py-2 text-right">{a.acknowledged_by ? <span className="text-xs">quittiert: {a.acknowledged_by}</span> : a.status === "firing" && can("technician") && <Button variant="ghost" onClick={() => void run(async () => { await api.post(`/alerts/${a.id}/ack`); await alerts.reload(); })}>Quittieren</Button>}</td>
            </tr>
          ))}
        </Table>
      </Card>
      {tenant && (
        <Card title="Regeln" className="mt-6" actions={can("admin") && <>
          {rules.data?.length === 0 && <Button variant="secondary" onClick={() => void run(async () => { await api.post("/alert-rules/defaults"); await rules.reload(); })}>Standardregeln anlegen</Button>}
          <Button onClick={() => setEdit({ name: "", type: "device_offline", severity: "warning", params: {}, duration_s: 300, site_ids: [], device_ids: [], recipients: [], notify_resolved: true, enabled: true })}>+ Regel</Button>
        </>}>
          <Table head={["Name", "Typ", "Schwere", "Verzögerung", "Geltung", "Empfänger", ""]} empty={rules.data?.length === 0}>
            {rules.data?.map((r) => (
              <tr key={r.id} className={r.enabled ? "" : "text-slate-400"}>
                <td className="px-3 py-2 font-medium">{r.name}</td>
                <td className="px-3 py-2">{r.type_label}{r.params.threshold != null && ` (> ${r.params.threshold})`}</td>
                <td className="px-3 py-2"><Badge color={sevColor(r.severity)}>{r.severity}</Badge></td>
                <td className="px-3 py-2">{r.duration_s >= 60 ? `${r.duration_s / 60} min` : `${r.duration_s} s`}</td>
                <td className="px-3 py-2 text-xs">{r.device_ids.length ? `${r.device_ids.length} Geräte` : r.site_ids.length ? `${r.site_ids.length} Standorte` : "alle Geräte"}</td>
                <td className="px-3 py-2 text-xs">{r.recipients.join(", ") || "Mandanten-Kontakt"}</td>
                <td className="px-3 py-2 text-right">{can("admin") && <Button variant="ghost" onClick={() => setEdit(r)}>Bearbeiten</Button>}</td>
              </tr>
            ))}
          </Table>
        </Card>
      )}
      {edit && types.data && <RuleModal rule={edit} types={types.data} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); void rules.reload(); }} />}
    </>
  );
}

function RuleModal({ rule, types, onClose, onSaved }: { rule: Rule; types: Record<string, string>; onClose: () => void; onSaved: () => void }) {
  const [r, setR] = useState<Rule>(structuredClone(rule));
  const [rcpt, setRcpt] = useState(r.recipients.join(", "));
  const sites = useFetch<Site[]>("/sites");
  const devices = useFetch<Device[]>("/devices");
  const { busy, error, run } = useAction();
  const needsThr = r.type === "latency" || r.type === "cpu_high";
  const body = () => ({ ...r, recipients: rcpt.split(/[\s,;]+/).filter(Boolean) });
  return (
    <Modal open onClose={onClose} title={r.id ? "Regel bearbeiten" : "Neue Regel"} wide>
      <ErrorBox error={error} />
      <div className="grid gap-3 md:grid-cols-2">
        <Input label="Name" value={r.name} onChange={(e) => setR({ ...r, name: e.target.value })} />
        <Select label="Typ" value={r.type} onChange={(e) => setR({ ...r, type: e.target.value })}>{Object.entries(types).map(([k, l]) => <option key={k} value={k}>{l}</option>)}</Select>
        <Select label="Schwere" value={r.severity} onChange={(e) => setR({ ...r, severity: e.target.value })}><option value="info">info</option><option value="warning">warning</option><option value="critical">critical</option></Select>
        <Input label="Verzögerung (s) – Bedingung muss so lange anliegen" type="number" min={0} value={r.duration_s} onChange={(e) => setR({ ...r, duration_s: Number(e.target.value) })} />
        {needsThr && <Input label={r.type === "latency" ? "Schwelle (ms)" : "Schwelle (%)"} type="number" value={r.params.threshold ?? ""} onChange={(e) => setR({ ...r, params: { ...r.params, threshold: Number(e.target.value) } })} />}
        {r.type === "latency" && <Select label="Messung" value={r.params.metric ?? "wan"} onChange={(e) => setR({ ...r, params: { ...r.params, metric: e.target.value } })}><option value="wan">WAN-Links (Netwatch)</option><option value="mgmt">Latenz zur Cloud</option></Select>}
        <Input label="Empfänger (leer = Mandanten-Kontakt)" value={rcpt} onChange={(e) => setRcpt(e.target.value)} />
        <div className="space-y-1 pt-6">
          <Checkbox label="Entwarnung senden" checked={r.notify_resolved} onChange={(v) => setR({ ...r, notify_resolved: v })} />
          <Checkbox label="aktiv" checked={r.enabled} onChange={(v) => setR({ ...r, enabled: v })} />
        </div>
      </div>
      <div className="mt-4 grid gap-3 md:grid-cols-2">
        <div><div className="mb-1 text-sm font-medium">Nur Standorte (optional)</div><div className="max-h-32 overflow-y-auto rounded border p-2">{sites.data?.map((s) => <Checkbox key={s.id} label={s.name} checked={r.site_ids.includes(s.id)} onChange={(v) => setR({ ...r, site_ids: v ? [...r.site_ids, s.id] : r.site_ids.filter((x) => x !== s.id) })} />)}</div></div>
        <div><div className="mb-1 text-sm font-medium">Nur Geräte (optional)</div><div className="max-h-32 overflow-y-auto rounded border p-2">{devices.data?.map((d) => <Checkbox key={d.id} label={d.name} checked={r.device_ids.includes(d.id)} onChange={(v) => setR({ ...r, device_ids: v ? [...r.device_ids, d.id] : r.device_ids.filter((x) => x !== d.id) })} />)}</div></div>
      </div>
      <div className="mt-4 flex justify-between">
        <div className="flex gap-2">
          {r.id && <Button variant="danger" onClick={() => confirm("Regel löschen?") && void run(async () => { await api.del(`/alert-rules/${r.id}`); onSaved(); })}>Löschen</Button>}
          {r.id && <Button variant="secondary" onClick={() => void run(async () => { const x = await api.post<{ to: string[] }>(`/alert-rules/${r.id}/test`); alert(`Test-Mail an ${x.to.join(", ")} gesendet`); })}>Test-Mail</Button>}
        </div>
        <Button disabled={busy} onClick={() => void run(async () => { if (r.id) await api.put(`/alert-rules/${r.id}`, body()); else await api.post("/alert-rules", body()); onSaved(); })}>Speichern</Button>
      </div>
    </Modal>
  );
}
