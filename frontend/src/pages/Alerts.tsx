import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Button, Checkbox, EmptyState, ErrorBox, Input, Loading, Modal, Notice, PageHeader, Segment, Select, SeverityBadge, Tabs, Toggle, cls, useAction } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { alarmState, alertTitle, useDevices, useOpenAlerts, useSites, type AlarmState, type AlertItem } from "../lib/fleet";
import { fmtShort, fmtSince } from "../lib/format";
import { useLive } from "../lib/live";
import { useMeta } from "../lib/meta";
import { useFetch } from "../lib/useFetch";

export type { AlertItem };
export const sevColor = (s: string) => (s === "critical" ? "red" : s === "warning" ? "yellow" : "blue");

interface Rule {
  id?: string; name: string; type: string; type_label?: string; severity: string;
  params: { threshold?: number; metric?: string; thresholds?: number[] }; duration_s: number;
  site_ids: string[]; device_ids: string[]; recipients: string[]; notify_resolved: boolean; enabled: boolean;
  webhook?: string | null; webhook_url?: string | null; webhook_format?: string;
}

const dur = (s: number) => (s <= 0 ? "sofort" : s < 60 ? `${s} s` : s % 3600 === 0 ? `${s / 3600} h` : `${Math.round(s / 60)} min`);
/** Bedingung als lesbarer Satz aus Typ + Parametern (keine freie Syntax). */
export function conditionText(r: Rule): string {
  const d = r.duration_s > 0 ? ` für mindestens ${dur(r.duration_s)}` : " (sofort)";
  const p = r.params ?? {};
  switch (r.type) {
    case "device_offline": return `Gerät nicht erreichbar${d}`;
    case "wan_down": return `WAN-Leitung down${d}`;
    case "latency": return p.metric === "mgmt" ? `Latenz zur Cloud über ${p.threshold ?? 150} ms${d}` : `WAN-Latenz über ${p.threshold ?? 150} ms${d}`;
    case "mesh_down": return `VPN-Tunnel down${d}`;
    case "cpu_high": return `CPU-Last über ${p.threshold ?? 90} %${d}`;
    case "vrrp_master": return `VRRP-Rolle ist Master${d}`;
    case "wan_backup_active": return `Backup-WAN trägt die Default-Route${d}`;
    case "wan_volume": return `Monatsvolumen erreicht ${(p.thresholds ?? [80, 100]).join(" % / ")} % des Limits`;
    default: return r.type_label ?? r.type;
  }
}

const EMPTY_RULE: Rule = { name: "", type: "device_offline", severity: "warning", params: {}, duration_s: 300, site_ids: [], device_ids: [], recipients: [], notify_resolved: true, enabled: true };

export default function Alerts() {
  const { can, me } = useAuth();
  const meta = useMeta();
  const [params, setParams] = useSearchParams();
  const view = params.get("view") === "rules" ? "rules" : "list";
  const tenant = me?.active_tenant_id;
  const open = useOpenAlerts();
  const hist = useFetch<AlertItem[]>("/alerts?state=all&limit=1000");
  const rules = useFetch<Rule[]>(tenant ? "/alert-rules" : null);
  const types = useFetch<Record<string, string>>("/alert-rules/types");
  const devices = useDevices();
  const sites = useSites();
  const [filter, setFilter] = useState<AlarmState | "all">("active");
  const [sev, setSev] = useState("");
  const [site, setSite] = useState("");
  const [edit, setEdit] = useState<Rule | null>(null);
  const { busy, error, run } = useAction();
  useLive(() => void hist.reload(), ["alert.firing", "alert.resolved"]);

  const all = useMemo(() => {
    const m = new Map<string, AlertItem>();
    for (const a of [...(hist.data ?? []), ...(open.data ?? [])]) m.set(a.id, a);
    return [...m.values()].sort((a, b) => b.started_at.localeCompare(a.started_at));
  }, [hist.data, open.data]);
  const devSite = (id: string | null) => {
    const d = devices.data?.find((x) => x.id === id);
    return d?.site_id ? sites.data?.find((s) => s.id === d.site_id) : undefined;
  };
  const ruleOf = (id: string | null) => rules.data?.find((r) => r.id === id);
  const scoped = all.filter((a) => (!sev || a.severity === sev) && (!site || devSite(a.device_id)?.id === site));
  const count = (s: AlarmState) => scoped.filter((a) => alarmState(a) === s).length;
  const rows = scoped.filter((a) => (filter === "all" ? alarmState(a) !== "pending" : alarmState(a) === filter));
  const activeTotal = all.filter((a) => alarmState(a) === "active").length;
  const since30 = Date.now() - 30 * 86400e3;
  const hits = (id?: string) => all.filter((a) => a.rule_id === id && a.fired_at && new Date(a.fired_at).getTime() >= since30).length;
  const channels = ["E-Mail", ...(rules.data?.some((r) => r.webhook) ? ["Webhook"] : [])].join(" und ");
  const toggleRule = (r: Rule, enabled: boolean) => void run(async () => { await api.put(`/alert-rules/${r.id}`, { ...r, enabled, webhook: undefined, webhook_url: null }); await rules.reload(); });

  return (
    <>
      <PageHeader title="Alarme" subtitle={`${me?.tenants.find((t) => t.id === tenant)?.name ?? "Alle Mandanten"} · Benachrichtigung per ${channels}`}
        actions={<>
          {can("technician") && tenant && view === "list" && <Button variant="secondary" icon="rotate" disabled={busy} onClick={() => void run(async () => { await api.post("/alerts/evaluate"); await Promise.all([open.reload(), hist.reload()]); })}>Jetzt auswerten</Button>}
          {can("admin") && tenant && view === "rules" && <Button icon="plus" onClick={() => setEdit({ ...EMPTY_RULE })}>Regel anlegen</Button>}
        </>} />
      {meta && !meta.smtp_configured && (
        <div className="mb-4"><Notice tone="orange" title="E-Mail-Versand ist nicht eingerichtet">
          Alarme erscheinen hier und in der Glocke, es werden aber keine Mails verschickt. SMTP-Zugangsdaten (<code>SMTP_HOST</code>, <code>SMTP_USER</code>, …) in der <code>.env</code> eintragen und die Dienste neu starten.
        </Notice></div>
      )}
      <ErrorBox error={error ?? hist.error} />
      <Tabs className="-mt-1 mb-4 border-b border-line" value={view} onChange={(v) => setParams(v === "rules" ? { view: "rules" } : {}, { replace: true })}
        tabs={[{ key: "list", label: "Alarme", count: activeTotal }, ...(tenant ? [{ key: "rules" as const, label: "Alarmregeln", count: rules.data?.length ?? null }] : [])]} />

      {view === "list" ? (
        <>
          <div className="mb-3 flex flex-wrap items-center gap-2.5">
            <Segment label="Alarmstatus" value={filter} onChange={setFilter} options={[
              { value: "active", label: "Aktiv", count: count("active") }, { value: "ack", label: "Quittiert", count: count("ack") },
              { value: "resolved", label: "Behoben", count: count("resolved") },
              ...(count("pending") ? [{ value: "pending" as const, label: "Ausstehend", count: count("pending"), title: "Bedingung erfüllt, Verzögerung läuft noch" }] : []),
              { value: "all", label: "Alle", count: scoped.filter((a) => alarmState(a) !== "pending").length },
            ]} />
            <FilterSelect label="Schweregrad" value={sev} onChange={setSev} options={[["critical", "Kritisch"], ["warning", "Warnung"], ["info", "Info"]]} />
            <FilterSelect label="Standort" value={site} onChange={setSite} options={(sites.data ?? []).map((s) => [s.id, s.name])} />
          </div>
          <section className="overflow-hidden rounded-lg border border-line bg-panel">
            {!hist.data ? <Loading rows={4} /> : rows.length === 0 ? (
              <EmptyState compact icon="checkCircle" title={filter === "active" ? "Keine aktiven Alarme" : "Keine Alarme für diesen Filter"} />
            ) : (
              <div className="overflow-x-auto">
                <div className="min-w-[980px]">
                  <div className="grid grid-cols-[104px_minmax(0,1fr)_110px_150px_110px_100px_190px] gap-3 border-b border-line bg-panel2 px-4 py-2 text-xs font-medium text-fg3">
                    <span>Schweregrad</span><span>Alarm</span><span>Standort</span><span>Gerät</span><span>Beginn</span><span>Dauer</span><span className="text-right">Status</span>
                  </div>
                  {rows.map((a) => {
                    const st = alarmState(a);
                    const r = ruleOf(a.rule_id);
                    return (
                      <div key={a.id} className={cls("grid min-h-[50px] grid-cols-[104px_minmax(0,1fr)_110px_150px_110px_100px_190px] items-center gap-3 border-b border-line px-4 py-1.5 last:border-b-0 hover:bg-hover", st === "resolved" && "text-fg2")}>
                        <span><SeverityBadge severity={a.severity} resolved={st === "resolved"} /></span>
                        <span className="flex min-w-0 flex-col leading-[1.3]">
                          <span className="truncate font-medium" title={a.message}>{alertTitle(a)}</span>
                          <span className="truncate text-xs text-fg3">{[...new Set([r?.type_label ?? types.data?.[r?.type ?? ""], r?.name].filter(Boolean))].join(" · ") || "Regel gelöscht"}</span>
                        </span>
                        <span className="truncate">{devSite(a.device_id)?.name ?? "–"}</span>
                        <span className="truncate font-mono text-xs text-fg2">{a.device_id ? <Link to={`/devices/${a.device_id}`}>{a.device}</Link> : "–"}</span>
                        <span className="font-mono text-xs text-fg2">{fmtShort(a.fired_at ?? a.started_at)}</span>
                        <span className="text-fg2">{fmtSince(a.started_at, a.resolved_at)}</span>
                        <span className="flex justify-end text-right">
                          {st === "active" && can("technician") ? (
                            <Button size="sm" variant="secondary" icon="check" onClick={() => void run(async () => { await api.post(`/alerts/${a.id}/ack`); await Promise.all([open.reload(), hist.reload()]); })}>Quittieren</Button>
                          ) : (
                            <span className="text-xs leading-[1.3] text-fg3">
                              {st === "ack" && `Quittiert von ${a.acknowledged_by}${a.acknowledged_at ? `, ${fmtShort(a.acknowledged_at)}` : ""}`}
                              {st === "resolved" && `Behoben ${fmtShort(a.resolved_at)}${a.acknowledged_by ? ` · quittiert von ${a.acknowledged_by}` : ""}`}
                              {st === "pending" && (a.fires_at ? `Löst aus ${new Date(a.fires_at) > new Date() ? `um ${new Date(a.fires_at).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" })}` : "gleich"}` : "Ausstehend")}
                              {st === "active" && "Aktiv"}
                            </span>
                          )}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </section>
        </>
      ) : (
        <section className="overflow-hidden rounded-lg border border-line bg-panel">
          {!rules.data ? <Loading rows={4} /> : rules.data.length === 0 ? (
            <EmptyState icon="bell" title="Noch keine Alarmregeln" text="Standardregeln decken Offline, WAN-Ausfall, Latenz, VPN-Tunnel und VRRP-Master ab."
              action={can("admin") && <Button onClick={() => void run(async () => { await api.post("/alert-rules/defaults"); await rules.reload(); })}>Standardregeln anlegen</Button>} />
          ) : (
            <div className="overflow-x-auto">
              <div className="min-w-[980px]">
                <div className="grid grid-cols-[48px_minmax(0,1fr)_minmax(0,1.3fr)_104px_150px_150px_70px] gap-3 border-b border-line bg-panel2 px-4 py-2 text-xs font-medium text-fg3">
                  <span>Aktiv</span><span>Regel</span><span>Bedingung</span><span>Schweregrad</span><span>Geltungsbereich</span><span>Benachrichtigung</span><span className="text-right">30 Tage</span>
                </div>
                {rules.data.map((r) => (
                  <div key={r.id} className={cls("grid min-h-12 grid-cols-[48px_minmax(0,1fr)_minmax(0,1.3fr)_104px_150px_150px_70px] items-center gap-3 border-b border-line px-4 py-1.5 last:border-b-0", !r.enabled && "opacity-55")}>
                    <span><Toggle checked={r.enabled} label={`${r.name} ${r.enabled ? "deaktivieren" : "aktivieren"}`} disabled={!can("admin")} onChange={(v) => toggleRule(r, v)} /></span>
                    <span className="min-w-0">
                      {can("admin") ? <button type="button" onClick={() => setEdit(r)} className="max-w-full cursor-pointer truncate text-left font-medium text-fg hover:text-blue-text hover:underline">{r.name}</button> : <span className="font-medium">{r.name}</span>}
                    </span>
                    <span className="text-fg2">{conditionText(r)}</span>
                    <span><SeverityBadge severity={r.severity} /></span>
                    <span className="truncate text-fg2">{r.device_ids.length ? `${r.device_ids.length} ${r.device_ids.length === 1 ? "Gerät" : "Geräte"}` : r.site_ids.length ? r.site_ids.map((id) => sites.data?.find((s) => s.id === id)?.name ?? "?").join(", ") : "Alle Standorte"}</span>
                    <span className="truncate text-fg2" title={r.recipients.join(", ")}>E-Mail{r.webhook ? `, ${r.webhook_format === "teams" ? "Teams" : "Webhook"}` : ""}{r.recipients.length ? ` (${r.recipients.length})` : ""}</span>
                    <span className="text-right text-fg2">{hits(r.id)}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </section>
      )}
      {edit && types.data && <RuleModal rule={edit} types={types.data} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); void rules.reload(); }} />}
    </>
  );
}

function FilterSelect({ label, value, onChange, options }: { label: string; value: string; onChange: (v: string) => void; options: string[][] }) {
  return (
    <label className="flex h-8 items-center gap-1.5 rounded-md border border-line-strong bg-panel pl-2.5">
      <span className="text-fg3">{label}:</span>
      <select value={value} onChange={(e) => onChange(e.target.value)} className="h-full cursor-pointer bg-transparent pr-2 font-medium outline-none focus-visible:outline-none">
        <option value="">Alle</option>
        {options.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
      </select>
    </label>
  );
}

function RuleModal({ rule, types, onClose, onSaved }: { rule: Rule; types: Record<string, string>; onClose: () => void; onSaved: () => void }) {
  const [r, setR] = useState<Rule>(structuredClone(rule));
  const [rcpt, setRcpt] = useState(r.recipients.join(", "));
  const [hook, setHook] = useState("");
  const [dropHook, setDropHook] = useState(false);
  const [ths, setThs] = useState((r.params.thresholds ?? [80, 100]).join(", "));
  const [testMsg, setTestMsg] = useState<string | null>(null);
  const sites = useSites();
  const devices = useDevices();
  const { busy, error, run } = useAction();
  const needsThr = r.type === "latency" || r.type === "cpu_high";
  const body = () => ({
    ...r, webhook: undefined, webhook_url: dropHook ? "" : hook.trim() || null, recipients: rcpt.split(/[\s,;]+/).filter(Boolean),
    params: r.type === "wan_volume" ? { thresholds: ths.split(/[\s,;]+/).filter(Boolean).map(Number) } : needsThr ? { threshold: r.params.threshold ?? (r.type === "latency" ? 150 : 90), ...(r.type === "latency" ? { metric: r.params.metric ?? "wan" } : {}) } : {},
  });
  return (
    <Modal open onClose={onClose} title={r.id ? "Regel bearbeiten" : "Neue Alarmregel"} subtitle={conditionText({ ...r, params: body().params })} size="lg"
      footer={<>
        {r.id && <Button variant="danger-outline" icon="trash" className="mr-auto" onClick={() => confirm("Regel löschen?") && void run(async () => { await api.del(`/alert-rules/${r.id}`); onSaved(); })}>Löschen</Button>}
        {r.id && <Button variant="secondary" onClick={() => void run(async () => { const x = await api.post<{ to: string[]; webhook: boolean | null }>(`/alert-rules/${r.id}/test`); setTestMsg(`Test gesendet – E-Mail: ${x.to.join(", ") || "–"}${x.webhook != null ? ` · Webhook: ${x.webhook ? "ok" : "fehlgeschlagen"}` : ""}`); })}>Test senden</Button>}
        <Button variant="secondary" onClick={onClose}>Abbrechen</Button>
        <Button disabled={busy || !r.name} onClick={() => void run(async () => { if (r.id) await api.put(`/alert-rules/${r.id}`, body()); else await api.post("/alert-rules", body()); onSaved(); })}>Speichern</Button>
      </>}>
      <ErrorBox error={error} />
      {testMsg && <div className="mb-3"><Notice tone="green">{testMsg}</Notice></div>}
      <div className="grid gap-3 md:grid-cols-2">
        <Input label="Name" value={r.name} onChange={(e) => setR({ ...r, name: e.target.value })} />
        <Select label="Typ" value={r.type} onChange={(e) => setR({ ...r, type: e.target.value, params: {} })}>{Object.entries(types).map(([k, l]) => <option key={k} value={k}>{l}</option>)}</Select>
        <Select label="Schweregrad" value={r.severity} onChange={(e) => setR({ ...r, severity: e.target.value })}>
          <option value="critical">Kritisch</option><option value="warning">Warnung</option><option value="info">Info</option>
        </Select>
        <Input label="Verzögerung (Sekunden)" hint="Bedingung muss so lange anliegen – verhindert Alarme bei kurzen Aussetzern" type="number" min={0} value={r.duration_s} onChange={(e) => setR({ ...r, duration_s: Number(e.target.value) })} />
        {needsThr && <Input label={r.type === "latency" ? "Schwelle (ms)" : "Schwelle (%)"} type="number" value={r.params.threshold ?? (r.type === "latency" ? 150 : 90)} onChange={(e) => setR({ ...r, params: { ...r.params, threshold: Number(e.target.value) } })} />}
        {r.type === "latency" && <Select label="Messung" value={r.params.metric ?? "wan"} onChange={(e) => setR({ ...r, params: { ...r.params, metric: e.target.value } })}><option value="wan">WAN-Leitungen (Netwatch)</option><option value="mgmt">Latenz zur Cloud</option></Select>}
        {r.type === "wan_volume" && <Input label="Schwellen (% des Monatslimits)" hint="z. B. 80, 100 – je Schwelle ein eigener Alarm" value={ths} onChange={(e) => setThs(e.target.value)} />}
      </div>
      <h4 className="mb-2 mt-5 font-semibold">Benachrichtigung</h4>
      <div className="grid gap-3 md:grid-cols-2">
        <Input label="E-Mail-Empfänger" hint="Kommagetrennt, leer = Kontakt-E-Mail des Mandanten" value={rcpt} onChange={(e) => setRcpt(e.target.value)} />
        <Input label="Webhook-URL (optional)" hint={r.webhook ? `Gesetzt: ${r.webhook} – leer lassen = unverändert` : "https, z. B. Teams-Workflow oder eigener Endpunkt"} placeholder="https://…" value={hook} onChange={(e) => setHook(e.target.value)} className="font-mono" />
        <Select label="Webhook-Format" value={r.webhook_format ?? "generic"} onChange={(e) => setR({ ...r, webhook_format: e.target.value })}>
          <option value="generic">Generisch (JSON)</option>
          <option value="teams">Microsoft Teams (Adaptive Card)</option>
        </Select>
        <div className="flex flex-col justify-end gap-1.5 pb-1">
          <Checkbox label="Entwarnung senden, wenn behoben" checked={r.notify_resolved} onChange={(v) => setR({ ...r, notify_resolved: v })} />
          {r.webhook && <Checkbox label="Webhook entfernen" checked={dropHook} onChange={setDropHook} />}
          <Checkbox label="Regel aktiv" checked={r.enabled} onChange={(v) => setR({ ...r, enabled: v })} />
        </div>
      </div>
      <h4 className="mb-2 mt-5 font-semibold">Geltungsbereich <span className="font-normal text-fg3">– leer = alle</span></h4>
      <div className="grid gap-3 md:grid-cols-2">
        <div><div className="mb-1.5 font-medium">Standorte</div><div className="max-h-36 space-y-1 overflow-y-auto rounded-md border border-line p-2">{sites.data?.length ? sites.data.map((s) => <Checkbox key={s.id} label={s.name} checked={r.site_ids.includes(s.id)} onChange={(v) => setR({ ...r, site_ids: v ? [...r.site_ids, s.id] : r.site_ids.filter((x) => x !== s.id) })} />) : <span className="text-fg3">Keine Standorte</span>}</div></div>
        <div><div className="mb-1.5 font-medium">Geräte</div><div className="max-h-36 space-y-1 overflow-y-auto rounded-md border border-line p-2">{devices.data?.map((d) => <Checkbox key={d.id} label={d.name} checked={r.device_ids.includes(d.id)} onChange={(v) => setR({ ...r, device_ids: v ? [...r.device_ids, d.id] : r.device_ids.filter((x) => x !== d.id) })} />)}</div></div>
      </div>
    </Modal>
  );
}
