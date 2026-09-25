import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Badge, Button, Card, Checkbox, ErrorBox, Input, Modal, PageHeader, Select, StatusBadge, Table, Textarea, cls, useAction } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtDate } from "../lib/format";
import { useLive } from "../lib/live";
import type { Device, Site } from "../lib/types";
import { useFetch } from "../lib/useFetch";

type Rule = Record<string, string>;
interface Content { address_lists: { list: string; address: string; comment?: string }[]; filter: Rule[]; nat: Rule[] }
interface Policy {
  id: string; name: string; description: string | null; version: number; scope: "global" | "tenant";
  content: Content; updated_at: string; assigned_devices: number;
  assignments?: { device_id: string; device: string; status: string; deployed_version: number | null; deployed_at: string | null; last_error: string | null }[];
}
interface Deployment { id: string; policy_id: string | null; policy_version: number | null; atomic: boolean; status: string; started_by: string; results: Record<string, { name: string; ok: boolean; error?: string; rolled_back?: boolean }>; created_at: string; finished_at: string | null }

const FILTER_COLS = ["chain", "action", "protocol", "src-address", "src-address-list", "dst-address", "dst-address-list", "dst-port", "in-interface-list", "connection-state", "comment"];
const NAT_COLS = ["chain", "action", "protocol", "dst-address", "dst-port", "in-interface-list", "out-interface-list", "to-addresses", "to-ports", "comment"];
const CHAINS: Record<string, string[]> = { filter: ["input", "forward", "output"], nat: ["dstnat", "srcnat"] };
const ACTIONS: Record<string, string[]> = { filter: ["accept", "drop", "reject", "fasttrack-connection", "log", "passthrough", "return", "add-src-to-address-list", "tarpit"], nat: ["dst-nat", "src-nat", "masquerade", "redirect", "netmap", "accept", "return"] };
const empty = (): Content => ({ address_lists: [], filter: [], nat: [] });

export default function Policies() {
  const { can } = useAuth();
  const nav = useNavigate();
  const pols = useFetch<Policy[]>("/policies");
  const deps = useFetch<Deployment[]>("/deployments?limit=20");
  useLive(() => void deps.reload(), ["policy.deployment"]);
  const { busy, error, run } = useAction();
  const [name, setName] = useState("");
  const [open, setOpen] = useState(false);
  return (
    <>
      <PageHeader title="Firewall-Policies" subtitle="Zentral definierte Address-Lists, Filter- und NAT-Regeln – versioniert und mit Rollback" actions={can("technician") && <Button onClick={() => setOpen(true)}>+ Policy</Button>} />
      <p className="-mt-3 mb-4 text-sm text-slate-500">Bestehende Regeln eines Routers anzeigen oder übernehmen: <b>Geräte → Gerät → Firewall → „Als Policy übernehmen“</b>.</p>
      <Card>
        <ErrorBox error={pols.error} />
        <Table head={["Name", "Geltung", "Version", "Regeln", "Geräte", "Geändert"]} empty={pols.data?.length === 0}>
          {pols.data?.map((p) => (
            <tr key={p.id} className="hover:bg-slate-50">
              <td className="px-3 py-2 font-medium"><Link to={`/policies/${p.id}`} className="text-brand-700 hover:underline">{p.name}</Link><div className="text-xs text-slate-500">{p.description}</div></td>
              <td className="px-3 py-2">{p.scope === "global" ? <Badge color="blue">global (MSP)</Badge> : <Badge>Mandant</Badge>}</td>
              <td className="px-3 py-2">v{p.version}</td>
              <td className="px-3 py-2 text-xs text-slate-600">{p.content.filter?.length ?? 0} Filter · {p.content.nat?.length ?? 0} NAT · {p.content.address_lists?.length ?? 0} Adressen</td>
              <td className="px-3 py-2">{p.assigned_devices}</td>
              <td className="px-3 py-2 text-slate-500">{fmtDate(p.updated_at)}</td>
            </tr>
          ))}
        </Table>
      </Card>
      <DeploymentsCard deps={deps.data ?? []} policies={pols.data ?? []} />
      <Modal open={open} onClose={() => setOpen(false)} title="Neue Policy">
        <ErrorBox error={error} />
        <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); void run(async () => { const p = await api.post<Policy>("/policies", { name, content: empty() }); nav(`/policies/${p.id}`); }); }}>
          <Input label="Name" value={name} required onChange={(e) => setName(e.target.value)} />
          <p className="text-xs text-slate-500">Als MSP-Admin ohne gewählten Mandanten entsteht eine globale Policy, die alle Mandanten nutzen können.</p>
          <div className="flex justify-end"><Button disabled={busy}>Anlegen</Button></div>
        </form>
      </Modal>
    </>
  );
}

function DeploymentsCard({ deps, policies }: { deps: Deployment[]; policies: Policy[] }) {
  const [sel, setSel] = useState<Deployment | null>(null);
  const pname = (id: string | null) => policies.find((p) => p.id === id)?.name ?? (id ? id.slice(0, 8) : "Entzug");
  return (
    <Card title="Letzte Pushes" className="mt-6">
      <Table head={["Zeit", "Policy", "Version", "Status", "Geräte", "Von"]} empty={deps.length === 0}>
        {deps.map((d) => {
          const r = Object.values(d.results);
          return (
            <tr key={d.id} className="cursor-pointer hover:bg-slate-50" onClick={() => setSel(d)}>
              <td className="px-3 py-2 text-slate-500">{fmtDate(d.created_at)}</td>
              <td className="px-3 py-2">{pname(d.policy_id)}</td>
              <td className="px-3 py-2">{d.policy_version ? `v${d.policy_version}` : "–"}{d.atomic && <span className="ml-1 text-xs text-slate-500">(atomar)</span>}</td>
              <td className="px-3 py-2"><StatusBadge status={d.status === "success" ? "success" : d.status === "partial" ? "degraded" : d.status} /></td>
              <td className="px-3 py-2">{r.filter((x) => x.ok).length}/{r.length} ok</td>
              <td className="px-3 py-2 text-xs">{d.started_by}</td>
            </tr>
          );
        })}
      </Table>
      <Modal open={!!sel} onClose={() => setSel(null)} title="Push-Ergebnis" wide>
        {sel && (
          <ul className="space-y-2 text-sm">
            {Object.entries(sel.results).map(([id, r]) => (
              <li key={id} className="flex justify-between gap-4 border-b pb-2">
                <span className="font-medium">{r.name}</span>
                <span className="text-right">{r.ok ? <Badge color="green">ok</Badge> : <><Badge color="red">Fehler</Badge>{r.rolled_back && <Badge color="yellow">zurückgerollt</Badge>}<div className="text-xs text-red-600">{r.error}</div></>}</span>
              </li>
            ))}
          </ul>
        )}
      </Modal>
    </Card>
  );
}

function RuleTable({ kind, rules, onChange, readOnly }: { kind: "filter" | "nat"; rules: Rule[]; onChange: (r: Rule[]) => void; readOnly: boolean }) {
  const cols = kind === "filter" ? FILTER_COLS : NAT_COLS;
  const set = (i: number, k: string, v: string) => onChange(rules.map((r, j) => (j === i ? { ...r, [k]: v } : r)));
  const move = (i: number, d: number) => { const n = [...rules]; const [x] = n.splice(i, 1); n.splice(i + d, 0, x); onChange(n); };
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead><tr className="text-left text-slate-500"><th className="px-1">#</th>{cols.map((c) => <th key={c} className="px-1 py-1 font-medium">{c}</th>)}<th /></tr></thead>
        <tbody>
          {rules.map((r, i) => (
            <tr key={i} className="border-t">
              <td className="px-1 text-slate-400">{i + 1}</td>
              {cols.map((c) => (
                <td key={c} className="px-0.5 py-1">
                  {c === "chain" || c === "action" ? (
                    <select disabled={readOnly} className="rounded border border-slate-300 px-1 py-1" value={r[c] ?? ""} onChange={(e) => set(i, c, e.target.value)}>
                      {(c === "chain" ? CHAINS[kind] : ACTIONS[kind]).map((o) => <option key={o}>{o}</option>)}
                    </select>
                  ) : (
                    <input disabled={readOnly} className="w-full min-w-20 rounded border border-slate-300 px-1 py-1 font-mono" value={r[c] ?? ""} onChange={(e) => set(i, c, e.target.value)} />
                  )}
                </td>
              ))}
              <td className="whitespace-nowrap px-1">
                {!readOnly && <>
                  <button className="px-1 text-slate-400 hover:text-slate-700" disabled={i === 0} onClick={() => move(i, -1)}>↑</button>
                  <button className="px-1 text-slate-400 hover:text-slate-700" disabled={i === rules.length - 1} onClick={() => move(i, 1)}>↓</button>
                  <button className="px-1 text-red-400 hover:text-red-700" onClick={() => onChange(rules.filter((_, j) => j !== i))}>✕</button>
                </>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {!readOnly && <Button variant="ghost" className="mt-2" onClick={() => onChange([...rules, { chain: CHAINS[kind][0], action: ACTIONS[kind][0] }])}>+ Regel</Button>}
    </div>
  );
}

export function PolicyDetail() {
  const { id } = useParams();
  const { can, me } = useAuth();
  const nav = useNavigate();
  const pol = useFetch<Policy>(`/policies/${id}`);
  const versions = useFetch<{ version: number; note: string | null; created_by: string; created_at: string }[]>(`/policies/${id}/versions`);
  const devices = useFetch<Device[]>("/devices");
  const sites = useFetch<Site[]>("/sites");
  const [draft, setDraft] = useState<Content | null>(null);
  const [note, setNote] = useState("");
  const [tab, setTab] = useState<"filter" | "nat" | "address_lists" | "json">("filter");
  const [assignOpen, setAssignOpen] = useState(false);
  const [atomic, setAtomic] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const { busy, error, run } = useAction();
  useLive(() => void pol.reload(), ["policy.deployment"]);
  const p = pol.data;
  if (!p) return <ErrorBox error={pol.error} />;
  const content = draft ?? p.content;
  const readOnly = !can("technician") || (p.scope === "global" && !me?.user.is_superuser);
  const dirty = draft !== null && JSON.stringify(draft) !== JSON.stringify(p.content);
  const save = () => run(async () => { await api.patch(`/policies/${p.id}`, { content: draft, note: note || null }); setDraft(null); setNote(""); await pol.reload(); await versions.reload(); });

  return (
    <>
      <PageHeader
        title={p.name}
        subtitle={<>{p.scope === "global" ? <Badge color="blue">global (MSP)</Badge> : <Badge>Mandant</Badge>} · Version {p.version}</>}
        actions={can("technician") && <>
          <Button variant="secondary" onClick={() => setAssignOpen(true)}>Geräte zuweisen</Button>
          <label className="flex items-center gap-1 text-sm"><input type="checkbox" checked={atomic} onChange={(e) => setAtomic(e.target.checked)} /> atomar</label>
          <Button disabled={busy || dirty || !p.assignments?.length} title={dirty ? "Erst speichern" : ""} onClick={() => void run(async () => { await api.post(`/policies/${p.id}/deploy`, { atomic }); setMsg("Push gestartet"); await pol.reload(); })}>Auf alle Geräte pushen</Button>
        </>}
      />
      <ErrorBox error={error} />
      {msg && <div className="mb-4 rounded-lg bg-emerald-50 px-4 py-2 text-sm text-emerald-800">{msg}</div>}
      <div className="grid gap-6 xl:grid-cols-3">
        <Card className="xl:col-span-2" title={
          <div className="flex gap-1">
            {([["filter", `Filter (${content.filter.length})`], ["nat", `NAT (${content.nat.length})`], ["address_lists", `Address-Lists (${content.address_lists.length})`], ["json", "JSON"]] as const).map(([k, l]) => (
              <button key={k} onClick={() => setTab(k)} className={cls("rounded px-2 py-1 text-sm", tab === k ? "bg-brand-100 text-brand-800" : "text-slate-500 hover:bg-slate-100")}>{l}</button>
            ))}
          </div>
        }>
          {tab === "filter" && <RuleTable kind="filter" rules={content.filter} readOnly={readOnly} onChange={(r) => setDraft({ ...content, filter: r })} />}
          {tab === "nat" && <RuleTable kind="nat" rules={content.nat} readOnly={readOnly} onChange={(r) => setDraft({ ...content, nat: r })} />}
          {tab === "address_lists" && (
            <div className="space-y-1">
              {content.address_lists.map((a, i) => (
                <div key={i} className="flex gap-2">
                  {(["list", "address", "comment"] as const).map((k) => (
                    <input key={k} disabled={readOnly} placeholder={k} className="flex-1 rounded border border-slate-300 px-2 py-1 font-mono text-xs" value={a[k] ?? ""} onChange={(e) => setDraft({ ...content, address_lists: content.address_lists.map((x, j) => (j === i ? { ...x, [k]: e.target.value } : x)) })} />
                  ))}
                  {!readOnly && <button className="text-red-400" onClick={() => setDraft({ ...content, address_lists: content.address_lists.filter((_, j) => j !== i) })}>✕</button>}
                </div>
              ))}
              {!readOnly && <Button variant="ghost" onClick={() => setDraft({ ...content, address_lists: [...content.address_lists, { list: "", address: "" }] })}>+ Eintrag</Button>}
            </div>
          )}
          {tab === "json" && <JsonEditor value={content} readOnly={readOnly} onChange={setDraft} />}
          {!readOnly && (
            <div className="mt-4 flex items-end gap-2 border-t pt-4">
              <div className="flex-1"><Input label="Änderungsnotiz" value={note} onChange={(e) => setNote(e.target.value)} /></div>
              <Button variant="secondary" disabled={!dirty} onClick={() => setDraft(null)}>Verwerfen</Button>
              <Button disabled={!dirty || busy} onClick={() => void save()}>Als v{p.version + 1} speichern</Button>
            </div>
          )}
        </Card>
        <div className="space-y-6">
          <Card title="Zugewiesene Geräte">
            <ul className="space-y-2 text-sm">
              {p.assignments?.map((a) => (
                <li key={a.device_id} className="flex items-start justify-between gap-2">
                  <div><Link className="text-brand-700 hover:underline" to={`/devices/${a.device_id}`}>{a.device}</Link>
                    <div className="text-xs text-slate-500">{a.deployed_version ? `v${a.deployed_version} · ${fmtDate(a.deployed_at)}` : "noch nicht gepusht"}</div>
                    {a.last_error && <div className="text-xs text-red-600">{a.last_error}</div>}
                  </div>
                  <div className="flex items-center gap-1">
                    <StatusBadge status={a.status === "deployed" && a.deployed_version !== p.version ? "pending" : a.status} />
                    {can("technician") && <button className="text-xs text-slate-400 hover:text-red-600" title="Zuweisung entfernen (Regeln werden vom Gerät gelöscht)" onClick={() => confirm(`Policy von ${a.device} entfernen?`) && void run(async () => { await api.del(`/policies/${p.id}/assign/${a.device_id}`); await pol.reload(); })}>✕</button>}
                  </div>
                </li>
              ))}
              {!p.assignments?.length && <li className="text-slate-400">Keine</li>}
            </ul>
          </Card>
          <Card title="Versionen">
            <ul className="space-y-2 text-sm">
              {versions.data?.map((v) => (
                <li key={v.version} className="flex justify-between gap-2">
                  <div><b>v{v.version}</b> <span className="text-xs text-slate-500">{fmtDate(v.created_at)} · {v.created_by}</span><div className="text-xs text-slate-600">{v.note}</div></div>
                  {!readOnly && v.version !== p.version && <Button variant="ghost" onClick={() => confirm(`Auf v${v.version} zurücksetzen und pushen?`) && void run(async () => { await api.post(`/policies/${p.id}/rollback`, { version: v.version, deploy: (p.assignments?.length ?? 0) > 0 }); await pol.reload(); await versions.reload(); })}>Rollback</Button>}
                </li>
              ))}
            </ul>
          </Card>
          {!readOnly && <Button variant="danger" onClick={() => confirm("Policy löschen?") && void run(async () => { await api.del(`/policies/${p.id}`); nav("/policies"); })}>Policy löschen</Button>}
        </div>
      </div>
      <AssignModal open={assignOpen} onClose={() => setAssignOpen(false)} policyId={p.id} devices={devices.data ?? []} sites={sites.data ?? []} onDone={() => void pol.reload()} />
    </>
  );
}

function JsonEditor({ value, readOnly, onChange }: { value: Content; readOnly: boolean; onChange: (c: Content) => void }) {
  const [text, setText] = useState(JSON.stringify(value, null, 2));
  const [err, setErr] = useState<string | null>(null);
  return (
    <>
      <Textarea rows={18} value={text} disabled={readOnly} onChange={(e) => { setText(e.target.value); try { onChange({ ...empty(), ...JSON.parse(e.target.value) }); setErr(null); } catch (x) { setErr((x as Error).message); } }} />
      {err && <p className="mt-1 text-xs text-red-600">{err}</p>}
    </>
  );
}

function AssignModal({ open, onClose, policyId, devices, sites, onDone }: { open: boolean; onClose: () => void; policyId: string; devices: Device[]; sites: Site[]; onDone: () => void }) {
  const [mode, setMode] = useState<"devices" | "sites" | "tags">("devices");
  const [sel, setSel] = useState<string[]>([]);
  const [tags, setTags] = useState("");
  const { busy, error, run } = useAction();
  const toggle = (id: string) => setSel(sel.includes(id) ? sel.filter((x) => x !== id) : [...sel, id]);
  return (
    <Modal open={open} onClose={onClose} title="Policy zuweisen">
      <ErrorBox error={error} />
      <Select label="Ziel" value={mode} onChange={(e) => { setMode(e.target.value as typeof mode); setSel([]); }}>
        <option value="devices">Einzelne Geräte</option>
        <option value="sites">Alle Geräte von Standorten</option>
        <option value="tags">Alle Geräte mit Tags</option>
      </Select>
      <div className="mt-3 max-h-72 space-y-1 overflow-y-auto">
        {mode === "devices" && devices.filter((d) => d.pairing_status !== "revoked").map((d) => <Checkbox key={d.id} label={d.name} checked={sel.includes(d.id)} onChange={() => toggle(d.id)} />)}
        {mode === "sites" && sites.map((s) => <Checkbox key={s.id} label={s.name} checked={sel.includes(s.id)} onChange={() => toggle(s.id)} />)}
        {mode === "tags" && <Input label="Tags (kommagetrennt)" value={tags} onChange={(e) => setTags(e.target.value)} />}
      </div>
      <div className="mt-4 flex justify-end">
        <Button disabled={busy} onClick={() => void run(async () => {
          await api.post(`/policies/${policyId}/assign`, mode === "devices" ? { device_ids: sel } : mode === "sites" ? { site_ids: sel } : { tags: tags.split(",").map((t) => t.trim()).filter(Boolean) });
          onDone();
          onClose();
        })}>Zuweisen</Button>
      </div>
    </Modal>
  );
}
