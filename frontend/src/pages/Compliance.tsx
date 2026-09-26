import { useState } from "react";
import { Link } from "react-router-dom";
import TargetsModal from "../components/TargetsModal";
import { Button, Card, EmptyState, ErrorBox, IconButton, Input, Loading, Modal, PageHeader, Pill, Segment, Select, Textarea, useAction, type Tone } from "../components/ui";
import { api, download } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtFull } from "../lib/format";
import { useFetch } from "../lib/useFetch";

interface Rule { id: string; name: string; type: string; params: Record<string, unknown> }
interface RuleSet { id: string; name: string; description: string | null; rules: Rule[]; builtin: boolean; scope: "global" | "tenant"; assignments: { id: string; targets: { device_ids: string[]; site_ids: string[]; tags: string[] } }[] }
interface Cell { status: "ok" | "fail" | "unknown"; detail: string }
interface Report { rules: { rule_set_id: string; rule_id: string; name: string }[]; rows: { device_id: string; device: string; rule_set_id: string; rule_set: string; evaluated_at: string; passed: number; failed: number; unknown: number; cells: Record<string, Cell> }[] }
interface TrendDay { day: string; passed: number; failed: number; ratio: number | null; devices_failing: number }

export const RULE_TYPES: Record<string, { label: string; params: { key: string; label: string; list?: boolean; options?: string[] }[] }> = {
  contains: { label: "Export enthält Text", params: [{ key: "text", label: "Text" }] },
  not_contains: { label: "Export enthält Text nicht", params: [{ key: "text", label: "Text" }] },
  regex: { label: "Export: regulärer Ausdruck", params: [{ key: "pattern", label: "Ausdruck (max. 200 Zeichen)" }, { key: "expect", label: "Erwartung", options: ["match", "no_match"] }] },
  service_disabled: { label: "Dienst deaktiviert (live)", params: [{ key: "service", label: "Dienst (z. B. www, telnet, ftp)" }] },
  no_user: { label: "Benutzer existiert nicht (live)", params: [{ key: "name", label: "Benutzername" }] },
  ntp_enabled: { label: "NTP-Client aktiv (live)", params: [] },
  service_restricted_to_tunnel: { label: "Dienste nur aus Tunnel-Netz (live)", params: [{ key: "services", label: "Dienste (kommagetrennt)", list: true }] },
  channel_in: { label: "Update-Kanal (live)", params: [{ key: "channels", label: "Erlaubte Kanäle (kommagetrennt)", list: true }] },
  min_version: { label: "Mindestversion RouterOS (live)", params: [{ key: "version", label: "Version, z. B. 7.15" }] },
};
const CELL: Record<Cell["status"], [string, Tone]> = { ok: ["ok", "green"], fail: ["verletzt", "red"], unknown: ["?", "gray"] };

export default function Compliance() {
  const [tab, setTab] = useState<"report" | "sets">("report");
  return (
    <>
      <PageHeader title="Compliance" subtitle="Regeln gegen das letzte Backup und Live-Werte – ausgewertet nach jedem Backup" />
      <div className="mb-4"><Segment label="Ansicht" value={tab} onChange={setTab} options={[{ value: "report", label: "Flottenbericht" }, { value: "sets", label: "Regelsets" }]} /></div>
      {tab === "report" ? <ReportView /> : <SetsView />}
    </>
  );
}

function ReportView() {
  const { can } = useAuth();
  const sets = useFetch<RuleSet[]>("/compliance/rule-sets");
  const [setId, setSetId] = useState("");
  const rep = useFetch<Report>(`/compliance/report${setId ? `?rule_set_id=${setId}` : ""}`);
  const trend = useFetch<TrendDay[]>("/compliance/trend?days=30");
  const { busy, error, run } = useAction();
  const q = setId ? `?rule_set_id=${setId}` : "";
  const r = rep.data;
  return (
    <div className="flex flex-col gap-4">
      <ErrorBox error={error ?? rep.error} />
      <div className="flex flex-wrap items-end gap-2">
        <div className="w-64"><Select label="Regelset" value={setId} onChange={(e) => setSetId(e.target.value)}>
          <option value="">alle</option>{sets.data?.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
        </Select></div>
        <span className="flex-1" />
        {can("technician") && <Button variant="secondary" icon={busy ? "loader" : "rotate"} disabled={busy} onClick={() => void run(async () => { await api.post("/compliance/evaluate", {}); await rep.reload(); await trend.reload(); })}>Jetzt prüfen</Button>}
        <Button variant="secondary" icon="download" onClick={() => void download(`/compliance/report.csv${q}`, "compliance.csv")}>CSV</Button>
        <Button variant="secondary" icon="download" onClick={() => void download(`/compliance/report.pdf${q}`, "compliance.pdf")}>PDF</Button>
      </div>
      <Card title="Trend" subtitle={`Anteil bestandener Regeln je Tag (30 Tage)${trend.data?.length ? ` · zuletzt ${Math.round((trend.data[trend.data.length - 1].ratio ?? 0) * 100)} %` : ""}`}>
        {!trend.data?.length ? <p className="text-fg3">Noch keine Auswertungen.</p> : (
          <div className="flex h-24 items-end gap-1" role="img" aria-label="Trend bestandener Regeln">
            {trend.data.map((d) => (
              <div key={d.day} className="flex max-w-[18px] flex-1 flex-col justify-end" title={`${d.day}: ${d.passed} ok, ${d.failed} verletzt, ${d.devices_failing} Geräte mit Verstößen`}>
                <div className="rounded-t-sm bg-green" style={{ height: `${Math.max(4, (d.ratio ?? 0) * 88)}px` }} />
              </div>
            ))}
          </div>
        )}
      </Card>
      <Card flush title="Geräte × Regeln">
        {!r ? <Loading rows={3} /> : r.rows.length === 0 ? <EmptyState compact title="Noch keine Ergebnisse" text="Regelset unter „Regelsets“ Geräten zuweisen, dann „Jetzt prüfen“ oder das nächste Backup abwarten." /> : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead><tr className="border-b border-line bg-panel2 text-left text-xs text-fg3">
                <th className="px-3 py-2 font-medium">Gerät</th><th className="px-3 py-2 font-medium">Regelset</th>
                {r.rules.map((c) => <th key={c.rule_set_id + c.rule_id} className="max-w-32 px-2 py-2 font-medium" title={c.name}><span className="line-clamp-2">{c.name}</span></th>)}
                <th className="px-3 py-2 font-medium">Stand</th>
              </tr></thead>
              <tbody>
                {r.rows.map((row) => (
                  <tr key={row.device_id + row.rule_set_id} className="border-b border-line last:border-b-0">
                    <td className="px-3 py-2 font-medium"><Link to={`/devices/${row.device_id}`} className="hover:underline">{row.device}</Link></td>
                    <td className="px-3 py-2 text-fg2">{row.rule_set}</td>
                    {r.rules.map((c) => {
                      const cell = c.rule_set_id === row.rule_set_id ? row.cells[c.rule_id] : undefined;
                      return <td key={c.rule_set_id + c.rule_id} className="px-2 py-2">{cell ? <Pill tone={CELL[cell.status][1]} icon={cell.status === "ok" ? "checkCircle" : cell.status === "fail" ? "xCircle" : "minusCircle"} title={cell.detail}>{CELL[cell.status][0]}</Pill> : null}</td>;
                    })}
                    <td className="whitespace-nowrap px-3 py-2 text-xs text-fg3">{fmtFull(row.evaluated_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}

function SetsView() {
  const { can } = useAuth();
  const sets = useFetch<RuleSet[]>("/compliance/rule-sets");
  const [edit, setEdit] = useState<Partial<RuleSet> | null>(null);
  const [assign, setAssign] = useState<RuleSet | null>(null);
  const { error, run } = useAction();
  return (
    <div className="flex flex-col gap-4">
      <ErrorBox error={error ?? sets.error} />
      {can("admin") && <div><Button icon="plus" onClick={() => setEdit({ rules: [] })}>Regelset</Button></div>}
      {!sets.data ? <Loading rows={3} /> : sets.data.map((s) => (
        <Card key={s.id} title={<span className="flex items-center gap-2">{s.name}{s.builtin ? <Pill tone="gray">vordefiniert</Pill> : s.scope === "global" ? <Pill tone="blue">global</Pill> : <Pill>Mandant</Pill>}</span>}
          subtitle={s.description ?? undefined}
          actions={<>
            {can("technician") && <Button size="sm" variant="secondary" icon="plus" onClick={() => setAssign(s)}>Zuweisen</Button>}
            {can("admin") && <IconButton icon="copy" label="Kopieren" onClick={() => void run(async () => { await api.post(`/compliance/rule-sets/${s.id}/copy`); await sets.reload(); })} />}
            {can("admin") && !s.builtin && <IconButton icon="edit" label="Bearbeiten" onClick={() => setEdit(structuredClone(s))} />}
            {can("admin") && !s.builtin && <IconButton icon="trash" label="Löschen" onClick={() => confirm(`${s.name} löschen?`) && void run(async () => { await api.del(`/compliance/rule-sets/${s.id}`); await sets.reload(); })} />}
          </>}>
          <ul className="flex flex-col gap-1">{s.rules.map((r) => <li key={r.id} className="flex gap-2"><span>{r.name}</span><span className="text-xs text-fg3">{RULE_TYPES[r.type]?.label ?? r.type}</span></li>)}</ul>
          {s.assignments.length > 0 && (
            <div className="mt-3 flex flex-wrap gap-2 border-t border-line pt-3">
              {s.assignments.map((a) => (
                <span key={a.id} className="flex items-center gap-1 rounded-md border border-line px-2 py-1 text-xs">
                  {[a.targets.device_ids.length && `${a.targets.device_ids.length} Geräte`, a.targets.site_ids.length && `${a.targets.site_ids.length} Standorte`, a.targets.tags.length && `Tags: ${a.targets.tags.join(", ")}`].filter(Boolean).join(" · ")}
                  {can("technician") && <IconButton icon="x" label="Zuweisung entfernen" onClick={() => void run(async () => { await api.del(`/compliance/assignments/${a.id}`); await sets.reload(); })} />}
                </span>
              ))}
            </div>
          )}
        </Card>
      ))}
      {assign && <TargetsModal title={`${assign.name} zuweisen`} onClose={() => setAssign(null)} onSubmit={async (t) => { await api.post(`/compliance/rule-sets/${assign.id}/assign`, t); await sets.reload(); }} />}
      {edit && <SetDialog set={edit} onClose={() => setEdit(null)} onSaved={async () => { setEdit(null); await sets.reload(); }} />}
    </div>
  );
}

function SetDialog({ set, onClose, onSaved }: { set: Partial<RuleSet>; onClose: () => void; onSaved: () => Promise<void> }) {
  const [name, setName] = useState(set.name ?? "");
  const [desc, setDesc] = useState(set.description ?? "");
  const [rules, setRules] = useState<Rule[]>(set.rules ?? []);
  const { busy, error, run } = useAction();
  const upd = (i: number, patch: Partial<Rule>) => setRules(rules.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  return (
    <Modal open onClose={onClose} title={set.id ? "Regelset bearbeiten" : "Neues Regelset"} size="xl"
      footer={<><Button variant="secondary" onClick={onClose}>Abbrechen</Button><Button disabled={busy || !name} onClick={() => void run(async () => {
        const body = { name, description: desc || null, rules };
        if (set.id) await api.patch(`/compliance/rule-sets/${set.id}`, body); else await api.post("/compliance/rule-sets", body);
        await onSaved();
      })}>Speichern</Button></>}>
      <ErrorBox error={error} />
      <div className="grid gap-3 sm:grid-cols-2"><Input label="Name" value={name} onChange={(e) => setName(e.target.value)} /><Input label="Beschreibung" value={desc} onChange={(e) => setDesc(e.target.value)} /></div>
      <div className="mt-4 flex flex-col gap-3">
        {rules.map((r, i) => (
          <div key={i} className="grid gap-2 rounded-md border border-line p-3 md:grid-cols-[1fr_1fr_2fr_32px]">
            <Input label="Bezeichnung" value={r.name} onChange={(e) => upd(i, { name: e.target.value })} />
            <Select label="Typ" value={r.type} onChange={(e) => upd(i, { type: e.target.value, params: {} })}>
              {Object.entries(RULE_TYPES).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
            </Select>
            <div className="grid gap-2 sm:grid-cols-2">
              {(RULE_TYPES[r.type]?.params ?? []).map((p) => p.options ? (
                <Select key={p.key} label={p.label} value={String(r.params[p.key] ?? p.options[0])} onChange={(e) => upd(i, { params: { ...r.params, [p.key]: e.target.value } })}>
                  {p.options.map((o) => <option key={o}>{o}</option>)}
                </Select>
              ) : p.key === "pattern" || p.key === "text" ? (
                <Textarea key={p.key} label={p.label} rows={2} className="font-mono text-xs" value={String(r.params[p.key] ?? "")} onChange={(e) => upd(i, { params: { ...r.params, [p.key]: e.target.value } })} />
              ) : (
                <Input key={p.key} label={p.label} value={p.list ? ((r.params[p.key] as string[] | undefined) ?? []).join(", ") : String(r.params[p.key] ?? "")}
                  onChange={(e) => upd(i, { params: { ...r.params, [p.key]: p.list ? e.target.value.split(",").map((x) => x.trim()).filter(Boolean) : e.target.value } })} />
              ))}
            </div>
            <div className="self-end"><IconButton icon="trash" label="Regel entfernen" onClick={() => setRules(rules.filter((_, j) => j !== i))} /></div>
          </div>
        ))}
        <div><Button size="sm" variant="ghost" icon="plus" onClick={() => setRules([...rules, { id: "", name: "", type: "contains", params: {} }])}>Regel</Button></div>
      </div>
    </Modal>
  );
}
