import { useState } from "react";
import { Link } from "react-router-dom";
import { Badge, Button, Card, Checkbox, ErrorBox, Input, Modal, PageHeader, Select, StatusBadge, StatusDot, Table, useAction } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtDate } from "../lib/format";
import { useLive } from "../lib/live";
import { useFetch } from "../lib/useFetch";

interface Row { device_id: string; device: string; status: string; model: string | null; routeros_version: string | null; update: { installed: string; latest: string; channel: string; update_available: boolean; checked_at: string } | null }
interface Job { id: string; name: string; channel: string; batch_size: number; batch_interval_s: number; status: string; current_batch: number; next_batch_at: string | null; created_by: string; created_at: string; last_error: string | null; summary?: Record<string, number>; items?: { device: string; device_id: string; batch_no: number; status: string; from_version: string | null; to_version: string | null; error: string | null }[] }

export default function Firmware() {
  const { can } = useAuth();
  const rows = useFetch<Row[]>("/firmware/overview");
  const jobs = useFetch<Job[]>("/firmware/jobs");
  const [sel, setSel] = useState<string[]>([]);
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<string | null>(null);
  const { busy, error, run } = useAction();
  useLive(() => { void jobs.reload(); void rows.reload(); }, ["firmware.job", "firmware.item"]);
  const upd = rows.data?.filter((r) => r.update?.update_available) ?? [];
  return (
    <>
      <PageHeader title="Firmware" subtitle="RouterOS-Updates für die Flotte – in Batches, mit Backup vor jedem Update"
        actions={can("technician") && <>
          <Button variant="secondary" disabled={busy} onClick={() => void run(async () => { await api.post("/firmware/check"); await rows.reload(); })}>{busy ? "Prüfe …" : "Nach Updates suchen"}</Button>
          <Button disabled={!sel.length} onClick={() => setOpen(true)}>{sel.length} Geräte aktualisieren</Button>
        </>} />
      <ErrorBox error={error} />
      <Card title={`Geräte (${upd.length} Updates verfügbar)`} actions={<Button variant="ghost" onClick={() => setSel(upd.map((r) => r.device_id))}>Alle mit Update wählen</Button>}>
        <Table head={["", "", "Gerät", "Modell", "Installiert", "Verfügbar", "Kanal", "Geprüft"]} empty={rows.data?.length === 0}>
          {rows.data?.map((r) => (
            <tr key={r.device_id}>
              <td className="px-3 py-2"><input type="checkbox" checked={sel.includes(r.device_id)} onChange={(e) => setSel(e.target.checked ? [...sel, r.device_id] : sel.filter((x) => x !== r.device_id))} /></td>
              <td className="px-3 py-2"><StatusDot status={r.status} /></td>
              <td className="px-3 py-2 font-medium"><Link to={`/devices/${r.device_id}`} className="text-brand-700 hover:underline">{r.device}</Link></td>
              <td className="px-3 py-2">{r.model ?? "–"}</td>
              <td className="px-3 py-2 font-mono text-xs">{r.update?.installed ?? r.routeros_version ?? "–"}</td>
              <td className="px-3 py-2 font-mono text-xs">{r.update ? (r.update.update_available ? <Badge color="yellow">{r.update.latest}</Badge> : <Badge color="green">aktuell</Badge>) : "–"}</td>
              <td className="px-3 py-2">{r.update?.channel ?? "–"}</td>
              <td className="px-3 py-2 text-slate-500">{fmtDate(r.update?.checked_at)}</td>
            </tr>
          ))}
        </Table>
      </Card>
      <Card title="Update-Jobs" className="mt-6">
        <Table head={["Erstellt", "Name", "Kanal", "Batches", "Status", "Fortschritt", ""]} empty={jobs.data?.length === 0}>
          {jobs.data?.map((j) => {
            const s = j.summary ?? {};
            const total = Object.values(s).reduce((a, b) => a + b, 0);
            const done = (s.success ?? 0) + (s.skipped ?? 0) + (s.failed ?? 0) + (s.cancelled ?? 0);
            return (
              <tr key={j.id} className="cursor-pointer hover:bg-slate-50" onClick={() => setDetail(j.id)}>
                <td className="px-3 py-2 text-slate-500">{fmtDate(j.created_at)}</td>
                <td className="px-3 py-2 font-medium">{j.name}<div className="text-xs text-slate-500">{j.created_by}</div></td>
                <td className="px-3 py-2">{j.channel}</td>
                <td className="px-3 py-2">{j.batch_size} / {j.batch_interval_s}s</td>
                <td className="px-3 py-2"><StatusBadge status={j.status === "completed" ? "success" : j.status === "running" ? "running" : j.status === "paused" ? "degraded" : j.status} /> <span className="text-xs">{j.status}</span>{j.last_error && <div className="text-xs text-red-600">{j.last_error}</div>}</td>
                <td className="px-3 py-2 w-48">
                  <div className="h-2 overflow-hidden rounded bg-slate-200"><div className="h-full bg-brand-600" style={{ width: `${total ? (done / total) * 100 : 0}%` }} /></div>
                  <div className="mt-1 text-xs text-slate-500">{s.success ?? 0} ok · {s.skipped ?? 0} übersprungen · {s.failed ?? 0} Fehler · {total - done} offen</div>
                </td>
                <td className="px-3 py-2 text-right" onClick={(e) => e.stopPropagation()}>
                  {can("technician") && j.status === "running" && <Button variant="ghost" onClick={() => void run(async () => { await api.post(`/firmware/jobs/${j.id}/pause`); await jobs.reload(); })}>Pause</Button>}
                  {can("technician") && j.status === "paused" && <Button variant="ghost" onClick={() => void run(async () => { await api.post(`/firmware/jobs/${j.id}/resume`); await jobs.reload(); })}>Fortsetzen</Button>}
                  {can("technician") && ["running", "paused"].includes(j.status) && <Button variant="ghost" onClick={() => confirm("Job abbrechen?") && void run(async () => { await api.post(`/firmware/jobs/${j.id}/cancel`); await jobs.reload(); })}>Abbrechen</Button>}
                </td>
              </tr>
            );
          })}
        </Table>
      </Card>
      {open && <JobModal ids={sel} onClose={() => setOpen(false)} onCreated={() => { setOpen(false); setSel([]); void jobs.reload(); }} />}
      {detail && <JobDetail id={detail} onClose={() => setDetail(null)} />}
    </>
  );
}

function JobModal({ ids, onClose, onCreated }: { ids: string[]; onClose: () => void; onCreated: () => void }) {
  const [f, setF] = useState({ name: `RouterOS-Update ${new Date().toLocaleDateString("de-DE")}`, channel: "stable", batch_size: 5, batch_interval_s: 300, max_failures: 1, upgrade_routerboard: true });
  const { busy, error, run } = useAction();
  return (
    <Modal open onClose={onClose} title={`${ids.length} Geräte aktualisieren`}>
      <ErrorBox error={error} />
      <div className="space-y-3">
        <Input label="Name" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />
        <Select label="Kanal" value={f.channel} onChange={(e) => setF({ ...f, channel: e.target.value })}>
          {["stable", "long-term", "testing"].map((c) => <option key={c}>{c}</option>)}
        </Select>
        <div className="grid grid-cols-3 gap-3">
          <Input label="Geräte pro Batch" type="number" min={1} value={f.batch_size} onChange={(e) => setF({ ...f, batch_size: Number(e.target.value) })} />
          <Input label="Pause zw. Batches (s)" type="number" min={0} value={f.batch_interval_s} onChange={(e) => setF({ ...f, batch_interval_s: Number(e.target.value) })} />
          <Input label="Pausieren ab Fehlern" type="number" min={0} value={f.max_failures} onChange={(e) => setF({ ...f, max_failures: Number(e.target.value) })} />
        </div>
        <Checkbox label="Danach RouterBOARD-Firmware aktualisieren (zusätzlicher Reboot)" checked={f.upgrade_routerboard} onChange={(v) => setF({ ...f, upgrade_routerboard: v })} />
        <p className="text-xs text-slate-500">Vor jedem Update wird ein Konfigurations-Backup erstellt. Geräte rebooten während des Updates.</p>
      </div>
      <div className="mt-4 flex justify-end"><Button disabled={busy} onClick={() => void run(async () => { await api.post("/firmware/jobs", { ...f, device_ids: ids }); onCreated(); })}>Job starten</Button></div>
    </Modal>
  );
}

function JobDetail({ id, onClose }: { id: string; onClose: () => void }) {
  const job = useFetch<Job>(`/firmware/jobs/${id}`);
  useLive(() => void job.reload(), ["firmware.item", "firmware.job"]);
  const j = job.data;
  return (
    <Modal open onClose={onClose} title={j?.name ?? "Job"} wide>
      {j && (
        <Table head={["Batch", "Gerät", "Status", "Version", "Fehler"]}>
          {j.items?.map((i) => (
            <tr key={i.device_id}>
              <td className="px-3 py-2">{i.batch_no + 1}{i.batch_no === j.current_batch && j.status === "running" && <span className="ml-1 text-xs text-sky-600">aktuell</span>}</td>
              <td className="px-3 py-2">{i.device}</td>
              <td className="px-3 py-2"><StatusBadge status={i.status === "rebooting" || i.status === "updating" ? "running" : i.status} /> <span className="text-xs">{i.status}</span></td>
              <td className="px-3 py-2 font-mono text-xs">{i.from_version ?? "?"} → {i.to_version ?? "?"}</td>
              <td className="px-3 py-2 text-xs text-red-600">{i.error}</td>
            </tr>
          ))}
        </Table>
      )}
    </Modal>
  );
}
