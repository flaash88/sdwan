import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import TargetsModal, { type Targets } from "../components/TargetsModal";
import { Button, Card, CodeBlock, EmptyState, ErrorBox, IconButton, Input, Loading, Modal, Notice, PageHeader, Pill, Segment, Select, StatusBadge, Textarea, useAction, type Tone } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtFull } from "../lib/format";
import { useLive } from "../lib/live";
import { useFetch } from "../lib/useFetch";

interface Script { id: string; name: string; description: string | null; category: "read" | "change"; version: number; content: string; builtin: boolean; scope: "global" | "tenant"; warnings: string[] }
interface Run { id: string; script_name: string; script_version: number; category: string; content: string; status: string; batch_size: number; created_by: string; created_at: string; finished_at: string | null; last_error: string | null;
  items?: { id: string; device_id: string; device: string; batch_no: number; status: string; rendered: string; output: string | null; error: string | null; backup_id: string | null; finished_at: string | null }[];
  summary?: Record<string, number> }
interface Preview { devices: { device_id: string; device: string; status: string; rendered: string | null; error: string | null }[] }

const SUM: Record<string, string> = { queued: "wartend", running: "laufend", success: "erfolgreich", failed: "fehlgeschlagen", skipped: "übersprungen", cancelled: "abgebrochen" };
const CAT: Record<Script["category"], [string, Tone]> = { read: ["nur lesend", "green"], change: ["ändernd", "orange"] };

export default function Scripts() {
  const { can } = useAuth();
  const nav = useNavigate();
  const lib = useFetch<{ scripts: Script[]; variables: string[] }>("/scripts");
  const runs = useFetch<Run[]>("/script-runs?limit=30");
  const [tab, setTab] = useState<"lib" | "runs" | "search">("lib");
  const [edit, setEdit] = useState<Partial<Script> | null>(null);
  const [exec, setExec] = useState<Script | null>(null);
  const { error, run } = useAction();
  return (
    <>
      <PageHeader title="Scripts" subtitle="RouterOS-Scripts speichern, mit Variablen versehen und gestaffelt auf mehreren Geräten ausführen"
        actions={can("technician") && <Button icon="plus" onClick={() => setEdit({ category: "read", content: "" })}>Script</Button>} />
      <div className="mb-4"><Segment label="Ansicht" value={tab} onChange={setTab} options={[{ value: "lib", label: "Bibliothek" }, { value: "runs", label: "Ausführungen" }, { value: "search", label: "Ausgaben durchsuchen" }]} /></div>
      <ErrorBox error={error ?? lib.error} />
      {tab === "lib" && (!lib.data ? <Loading rows={4} /> : (
        <Card flush>
          {lib.data.scripts.length === 0 ? <EmptyState compact title="Keine Scripts" /> : lib.data.scripts.map((s) => (
            <div key={s.id} className="flex flex-wrap items-center gap-3 border-b border-line px-4 py-3 last:border-b-0">
              <div className="flex min-w-0 flex-1 flex-col">
                <span className="flex flex-wrap items-center gap-2 font-medium">{s.name}<Pill tone={CAT[s.category][1]}>{CAT[s.category][0]}</Pill>
                  {s.builtin ? <Pill tone="gray">vordefiniert</Pill> : s.scope === "global" ? <Pill tone="blue">global</Pill> : <Pill>Mandant</Pill>}<span className="text-xs text-fg3">v{s.version}</span></span>
                <span className="truncate text-xs text-fg2">{s.description}</span>
              </div>
              {s.warnings.length > 0 && <Pill tone="orange" icon="alert" title={s.warnings.join("\n")}>Hinweis</Pill>}
              {can("technician") && (s.category === "read" || can("admin")) && <Button size="sm" icon="play" onClick={() => setExec(s)}>Ausführen</Button>}
              {can("technician") && <IconButton icon="copy" label="Kopieren" onClick={() => void run(async () => { await api.post(`/scripts/${s.id}/copy`); await lib.reload(); })} />}
              {can("technician") && !s.builtin && (s.category === "read" || can("admin")) && <>
                <IconButton icon="edit" label="Bearbeiten" onClick={() => setEdit(s)} />
                <IconButton icon="trash" label="Löschen" onClick={() => confirm(`${s.name} löschen?`) && void run(async () => { await api.del(`/scripts/${s.id}`); await lib.reload(); })} />
              </>}
            </div>
          ))}
        </Card>
      ))}
      {tab === "runs" && <RunList runs={runs.data} />}
      {tab === "search" && <OutputSearch />}
      {edit && lib.data && <ScriptDialog script={edit} variables={lib.data.variables} onClose={() => setEdit(null)} onSaved={async () => { setEdit(null); await lib.reload(); }} />}
      {exec && <ExecWizard script={exec} onClose={() => setExec(null)} onStarted={(id) => nav(`/scripts/runs/${id}`)} />}
    </>
  );
}

function RunList({ runs }: { runs: Run[] | null }) {
  if (!runs) return <Loading rows={3} />;
  return (
    <Card flush>
      {runs.length === 0 ? <EmptyState compact title="Noch keine Ausführungen" /> : runs.map((r) => (
        <Link key={r.id} to={`/scripts/runs/${r.id}`} className="flex flex-wrap items-center gap-3 border-b border-line px-4 py-2.5 last:border-b-0 hover:bg-hover">
          <span className="flex-1 font-medium">{r.script_name} <span className="text-xs text-fg3">v{r.script_version}</span></span>
          <Pill tone={CAT[r.category as Script["category"]]?.[1] ?? "gray"}>{CAT[r.category as Script["category"]]?.[0] ?? r.category}</Pill>
          <StatusBadge status={r.status === "completed" ? "success" : r.status} />
          <span className="text-xs text-fg2">{fmtFull(r.created_at)} · {r.created_by}</span>
        </Link>
      ))}
    </Card>
  );
}

function OutputSearch() {
  const [q, setQ] = useState("");
  const [res, setRes] = useState<{ run_id: string; script_name: string; device_id: string; device: string; finished_at: string; lines: string[] }[] | null>(null);
  const { busy, error, run } = useAction();
  return (
    <div className="flex flex-col gap-4">
      <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); void run(async () => setRes(await api.get(`/script-runs/search?q=${encodeURIComponent(q)}`))); }}>
        <input aria-label="In Ausgaben suchen" className="h-9 flex-1 rounded-md border border-line bg-panel px-3 font-mono text-sm" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Text in den Ausgaben" />
        <Button icon="search" disabled={busy || q.length < 2}>Suchen</Button>
      </form>
      <ErrorBox error={error} />
      {res && (res.length === 0 ? <Card><EmptyState compact title="Keine Treffer" /></Card> : <Card flush>{res.map((h, i) => (
        <div key={i} className="border-b border-line px-4 py-2 last:border-b-0">
          <div className="flex gap-2 text-sm"><Link to={`/scripts/runs/${h.run_id}`} className="font-medium hover:underline">{h.script_name}</Link><span className="text-fg2">· {h.device} · {h.finished_at && fmtFull(h.finished_at)}</span></div>
          <pre className="m-0 font-mono text-xs text-fg2">{h.lines.join("\n")}</pre>
        </div>))}</Card>)}
    </div>
  );
}

function ScriptDialog({ script, variables, onClose, onSaved }: { script: Partial<Script>; variables: string[]; onClose: () => void; onSaved: () => Promise<void> }) {
  const { can } = useAuth();
  const [f, setF] = useState({ name: script.name ?? "", description: script.description ?? "", category: script.category ?? "read", content: script.content ?? "", note: "" });
  const { busy, error, run } = useAction();
  return (
    <Modal open onClose={onClose} title={script.id ? `Script ${script.name}` : "Neues Script"} size="xl"
      footer={<><Button variant="secondary" onClick={onClose}>Abbrechen</Button><Button disabled={busy || !f.name || !f.content} onClick={() => void run(async () => {
        if (script.id) await api.patch(`/scripts/${script.id}`, f); else await api.post("/scripts", f);
        await onSaved();
      })}>Speichern</Button></>}>
      <ErrorBox error={error} />
      <div className="grid gap-3 sm:grid-cols-2">
        <Input label="Name" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />
        <Select label="Kategorie" value={f.category} onChange={(e) => setF({ ...f, category: e.target.value as Script["category"] })}>
          <option value="read">nur lesend (ab Techniker)</option>{can("admin") && <option value="change">ändernd (nur Admin, mit Bestätigung und Backup)</option>}
        </Select>
        <div className="sm:col-span-2"><Input label="Beschreibung" value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} /></div>
        <div className="sm:col-span-2"><Textarea label="RouterOS-Script" rows={12} className="font-mono text-xs" value={f.content} onChange={(e) => setF({ ...f, content: e.target.value })} /></div>
        <p className="text-xs text-fg3 sm:col-span-2">Variablen: {variables.map((v) => <code key={v} className="mr-2 font-mono">{`{{ ${v} }}`}</code>)}</p>
        {script.id && <div className="sm:col-span-2"><Input label="Änderungsnotiz" value={f.note} onChange={(e) => setF({ ...f, note: e.target.value })} /></div>}
      </div>
    </Modal>
  );
}

function ExecWizard({ script, onClose, onStarted }: { script: Script; onClose: () => void; onStarted: (id: string) => void }) {
  const [targets, setTargets] = useState<Targets | null>(null);
  const [pick, setPick] = useState(true);
  const [pv, setPv] = useState<Preview | null>(null);
  const [batch, setBatch] = useState(5);
  const [maxFail, setMaxFail] = useState(1);
  const [confirmName, setConfirmName] = useState("");
  const { busy, error, run } = useAction();
  const change = script.category === "change";
  if (pick) return <TargetsModal title={`${script.name}: Ziele wählen`} submitLabel="Weiter zur Vorschau" onClose={onClose}
    onSubmit={async (t) => { setTargets(t); setPv(await api.post(`/scripts/${script.id}/preview`, t)); setPick(false); }} />;
  const errs = pv?.devices.filter((d) => d.error) ?? [];
  return (
    <Modal open onClose={onClose} title={`${script.name} ausführen`} subtitle={`${pv?.devices.length ?? 0} Geräte`} size="xl"
      footer={<><Button variant="secondary" onClick={onClose}>Abbrechen</Button>
        <Button variant={change ? "danger" : "primary"} icon="play" disabled={busy || errs.length > 0 || (change && confirmName !== script.name)} onClick={() => void run(async () => {
          const r = await api.post<Run>("/script-runs", { script_id: script.id, ...targets, batch_size: batch, max_failures: maxFail, confirm_name: change ? confirmName : null });
          onStarted(r.id);
        })}>Ausführen</Button></>}>
      <ErrorBox error={error} />
      {script.warnings.map((w) => <div key={w} className="mb-2"><Notice tone="orange" icon="alert">{w}</Notice></div>)}
      {change && <Notice tone="orange" title="Ändernde Ausführung">Vor der Ausführung wird je Gerät ein Backup erstellt; schlägt es fehl, wird das Script dort nicht ausgeführt.</Notice>}
      <div className="my-3 grid gap-3 sm:grid-cols-3">
        <Input label="Geräte je Gruppe" type="number" min={1} value={batch} onChange={(e) => setBatch(Number(e.target.value))} />
        <Input label="Abbruch nach Fehlern" type="number" min={1} value={maxFail} onChange={(e) => setMaxFail(Number(e.target.value))} />
        {change && <Input label={`Zur Bestätigung „${script.name}“ eingeben`} value={confirmName} onChange={(e) => setConfirmName(e.target.value)} />}
      </div>
      <div className="flex max-h-[45vh] flex-col gap-2 overflow-y-auto">
        {pv?.devices.map((d) => (
          <details key={d.device_id} className="rounded-md border border-line" open={pv.devices.length <= 2}>
            <summary className="flex cursor-pointer items-center gap-2 px-3 py-2"><b>{d.device}</b><StatusBadge status={d.status} />{d.error && <Pill tone="red">{d.error}</Pill>}</summary>
            {d.rendered && <div className="px-3 pb-3"><CodeBlock highlight={false} text={d.rendered} /></div>}
          </details>
        ))}
      </div>
    </Modal>
  );
}

export function ScriptRunPage() {
  const { id } = useParams();
  const { can } = useAuth();
  const r = useFetch<Run>(`/script-runs/${id}`);
  useLive(() => void r.reload(), ["device.poll"]);
  const { error, run } = useAction();
  const x = r.data;
  if (!x) return r.error ? <ErrorBox error={r.error} /> : <Loading rows={4} />;
  const act = (a: string) => run(async () => { await api.post(`/script-runs/${x.id}/${a}`); await r.reload(); });
  return (
    <>
      <PageHeader title={`${x.script_name} v${x.script_version}`} subtitle={<><Link to="/scripts" className="hover:underline">Scripts</Link> · {fmtFull(x.created_at)} · {x.created_by}</>}
        actions={can("technician") && <>
          <Button variant="secondary" icon="rotate" onClick={() => void r.reload()}>Aktualisieren</Button>
          {x.status === "running" && <Button variant="secondary" icon="pause" onClick={() => void act("pause")}>Anhalten</Button>}
          {x.status === "paused" && <Button icon="play" onClick={() => void act("resume")}>Fortsetzen</Button>}
          {["running", "paused"].includes(x.status) && <Button variant="danger-outline" icon="x" onClick={() => confirm("Ausführung abbrechen?") && void act("cancel")}>Abbrechen</Button>}
        </>} />
      <ErrorBox error={error ?? x.last_error} />
      <div className="mb-4 flex flex-wrap items-center gap-3"><StatusBadge status={x.status === "completed" ? "success" : x.status} />
        {Object.entries(x.summary ?? {}).filter(([, n]) => n).map(([k, n]) => <span key={k} className="text-sm text-fg2">{n} {SUM[k] ?? k}</span>)}</div>
      <Card flush title="Geräte">
        {x.items?.map((i) => (
          <details key={i.id} className="border-b border-line last:border-b-0">
            <summary className="flex cursor-pointer flex-wrap items-center gap-3 px-4 py-2.5">
              <span className="w-8 text-xs text-fg3">G{i.batch_no + 1}</span>
              <Link to={`/devices/${i.device_id}`} className="font-medium hover:underline">{i.device}</Link>
              <StatusBadge status={i.status} />{i.error && <span className="text-xs text-red-text">{i.error}</span>}
              {i.backup_id && <Pill tone="gray" icon="archive">Backup</Pill>}
            </summary>
            <div className="grid gap-3 px-4 pb-3 lg:grid-cols-2">
              <div><p className="mb-1 text-xs text-fg3">Befehle</p><CodeBlock highlight={false} text={i.rendered} /></div>
              <div><p className="mb-1 text-xs text-fg3">Ausgabe</p><pre className="m-0 max-h-80 overflow-auto rounded-md bg-code p-3 font-mono text-xs">{i.output ?? "–"}</pre></div>
            </div>
          </details>
        ))}
      </Card>
    </>
  );
}
