import { useState } from "react";
import { Link } from "react-router-dom";
import { Button, Card, Checkbox, ErrorBox, Input, Loading, Modal, PageHeader, Pill, Select, Table, Textarea, useAction, type Tone } from "../components/ui";
import { api, download } from "../lib/api";
import { useAuth } from "../lib/auth";
import { useFetch } from "../lib/useFetch";

interface Eol { id: string; model: string; status: "end_of_sale" | "eol"; since: string | null; successor: string | null; note: string | null; link: string | null; enabled: boolean; builtin: boolean }
interface Row {
  device_id: string; device: string; serial: string | null; model: string | null; site: string | null; routeros_version: string | null; status: string;
  purchase_date: string | null; warranty_until: string | null; supplier: string | null; notes: string | null; eol: Eol | null; warnings: { type: string; tone: Tone; text: string }[];
}

const d = (v: string | null) => (v ? new Date(v + "T00:00:00").toLocaleDateString("de-DE") : "–");

/** Inventar je Mandant: Seriennummer/Modell aus dem Gerät, Kauf/Garantie/Lieferant gepflegt; EOL-Warnungen; CSV-Export. */
export default function Inventory() {
  const { can, me } = useAuth();
  const rows = useFetch<Row[]>("/inventory");
  const [edit, setEdit] = useState<Row | null>(null);
  const [q, setQ] = useState("");
  const [onlyWarn, setOnlyWarn] = useState(false);
  const list = (rows.data ?? []).filter((r) => (!onlyWarn || r.warnings.length) && (!q || [r.device, r.serial, r.model, r.supplier, r.site].join(" ").toLowerCase().includes(q.toLowerCase())));
  const warn = (rows.data ?? []).filter((r) => r.warnings.length).length;
  return (
    <>
      <PageHeader title="Inventar" subtitle="Seriennummern, Modelle, Kauf- und Garantiedaten der Router"
        actions={<Button variant="secondary" icon="download" onClick={() => void download("/inventory.csv", "inventar.csv")}>CSV exportieren</Button>} />
      <ErrorBox error={rows.error} />
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <Input aria-label="Suche" placeholder="Gerät, Seriennummer, Modell, Lieferant …" value={q} onChange={(e) => setQ(e.target.value)} className="w-80" />
        <Checkbox label={`Nur mit Hinweisen (${warn})`} checked={onlyWarn} onChange={setOnlyWarn} />
      </div>
      <Card flush>
        {!rows.data ? <Loading rows={5} /> : (
          <Table head={["Gerät", "Seriennummer", "Modell", "Standort", "Kaufdatum", "Garantie bis", "Lieferant", "Hinweise", ""]} empty={list.length === 0}>
            {list.map((r) => (
              <tr key={r.device_id}>
                <td className="px-3 py-2 font-medium"><Link to={`/devices/${r.device_id}`} className="text-blue-text hover:underline">{r.device}</Link></td>
                <td className="px-3 py-2 font-mono text-xs">{r.serial ?? "–"}</td>
                <td className="px-3 py-2">{r.model ?? "–"}</td>
                <td className="px-3 py-2">{r.site ?? "–"}</td>
                <td className="px-3 py-2">{d(r.purchase_date)}</td>
                <td className="px-3 py-2">{d(r.warranty_until)}</td>
                <td className="px-3 py-2">{r.supplier ?? "–"}{r.notes && <div className="max-w-[220px] truncate text-xs text-fg3" title={r.notes}>{r.notes}</div>}</td>
                <td className="px-3 py-2"><div className="flex flex-col items-start gap-1">{r.warnings.map((w) => <Pill key={w.type} tone={w.tone}>{w.text}</Pill>)}</div></td>
                <td className="px-3 py-2 text-right">{can("technician") && <Button size="sm" variant="ghost" icon="edit" onClick={() => setEdit(r)}>Bearbeiten</Button>}</td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
      <EolCard superuser={!!me?.user.is_superuser} onChanged={() => void rows.reload()} />
      {edit && <EditModal row={edit} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); void rows.reload(); }} />}
    </>
  );
}

function EditModal({ row, onClose, onSaved }: { row: Row; onClose: () => void; onSaved: () => void }) {
  const [f, setF] = useState({ purchase_date: row.purchase_date ?? "", warranty_until: row.warranty_until ?? "", supplier: row.supplier ?? "", notes: row.notes ?? "" });
  const { busy, error, run } = useAction();
  return (
    <Modal open onClose={onClose} title={`Inventar – ${row.device}`} subtitle={`${row.model ?? "?"} · SN ${row.serial ?? "?"}`}
      footer={<><Button variant="secondary" onClick={onClose}>Abbrechen</Button><Button disabled={busy} onClick={() => void run(async () => {
        await api.put(`/devices/${row.device_id}/inventory`, { purchase_date: f.purchase_date || null, warranty_until: f.warranty_until || null, supplier: f.supplier || null, notes: f.notes || null });
        onSaved();
      })}>Speichern</Button></>}>
      <ErrorBox error={error} />
      <div className="grid grid-cols-2 gap-3">
        <Input label="Kaufdatum" type="date" value={f.purchase_date} onChange={(e) => setF({ ...f, purchase_date: e.target.value })} />
        <Input label="Garantie bis" type="date" value={f.warranty_until} onChange={(e) => setF({ ...f, warranty_until: e.target.value })} />
        <div className="col-span-2"><Input label="Lieferant" value={f.supplier} onChange={(e) => setF({ ...f, supplier: e.target.value })} /></div>
        <div className="col-span-2"><Textarea label="Notizen" rows={3} value={f.notes} onChange={(e) => setF({ ...f, notes: e.target.value })} /></div>
      </div>
    </Modal>
  );
}

const EMPTY: Omit<Eol, "id" | "builtin"> = { model: "", status: "eol", since: null, successor: null, note: null, link: null, enabled: true };

function EolCard({ superuser, onChanged }: { superuser: boolean; onChanged: () => void }) {
  const q = useFetch<Eol[]>("/eol-models");
  const [edit, setEdit] = useState<(Partial<Eol> & typeof EMPTY) | null>(null);
  const { busy, error, run } = useAction();
  const save = () => run(async () => {
    if (!edit) return;
    const body = { model: edit.model, status: edit.status, since: edit.since || null, successor: edit.successor || null, note: edit.note || null, link: edit.link || null, enabled: edit.enabled };
    if (edit.id) await api.put(`/eol-models/${edit.id}`, body);
    else await api.post("/eol-models", body);
    setEdit(null); await q.reload(); onChanged();
  });
  return (
    <Card title="Abgekündigte Modelle (EOL-Liste)" subtitle="Vom MSP gepflegt – Treffer erscheinen als Hinweis im Inventar" className="mt-4" flush
      actions={superuser && <Button size="sm" variant="secondary" icon="plus" onClick={() => setEdit({ ...EMPTY })}>Modell</Button>}>
      <ErrorBox error={error ?? q.error} />
      <Table head={["Modell", "Status", "Seit", "Nachfolger", "Hinweis", "Aktiv", ""]} empty={q.data?.length === 0}>
        {q.data?.map((e) => (
          <tr key={e.id}>
            <td className="px-3 py-2 font-medium">{e.model}{e.builtin && <span className="ml-2 text-xs text-fg3">(Beispiel)</span>}</td>
            <td className="px-3 py-2"><Pill tone={e.status === "eol" ? "red" : "orange"}>{e.status === "eol" ? "End of Life" : "End of Sale"}</Pill></td>
            <td className="px-3 py-2">{d(e.since)}</td>
            <td className="px-3 py-2">{e.successor ?? "–"}</td>
            <td className="max-w-[300px] px-3 py-2 text-xs text-fg2">{e.link ? <a href={e.link} target="_blank" rel="noreferrer" className="text-blue-text hover:underline">{e.note ?? e.link}</a> : e.note ?? "–"}</td>
            <td className="px-3 py-2">{e.enabled ? "ja" : "nein"}</td>
            <td className="px-3 py-2 text-right">{superuser && !e.builtin && <>
              <Button size="sm" variant="ghost" onClick={() => setEdit({ ...e })}>Bearbeiten</Button>
              <Button size="sm" variant="ghost" icon="trash" disabled={busy} onClick={() => confirm(`„${e.model}“ löschen?`) && void run(async () => { await api.del(`/eol-models/${e.id}`); await q.reload(); onChanged(); })}>Löschen</Button>
            </>}</td>
          </tr>
        ))}
      </Table>
      {edit && (
        <Modal open onClose={() => setEdit(null)} title={edit.id ? "EOL-Eintrag bearbeiten" : "Abgekündigtes Modell"}
          footer={<><Button variant="secondary" onClick={() => setEdit(null)}>Abbrechen</Button><Button disabled={busy || !edit.model.trim()} onClick={() => void save()}>Speichern</Button></>}>
          <div className="grid grid-cols-2 gap-3">
            <div className="col-span-2"><Input label="Modell (wie im Gerät angezeigt)" value={edit.model} onChange={(e) => setEdit({ ...edit, model: e.target.value })} /></div>
            <Select label="Status" value={edit.status} onChange={(e) => setEdit({ ...edit, status: e.target.value as Eol["status"] })}><option value="end_of_sale">End of Sale</option><option value="eol">End of Life</option></Select>
            <Input label="Seit" type="date" value={edit.since ?? ""} onChange={(e) => setEdit({ ...edit, since: e.target.value || null })} />
            <Input label="Nachfolger" value={edit.successor ?? ""} onChange={(e) => setEdit({ ...edit, successor: e.target.value })} />
            <Input label="Link (Quelle)" value={edit.link ?? ""} onChange={(e) => setEdit({ ...edit, link: e.target.value })} placeholder="https://…" />
            <div className="col-span-2"><Textarea label="Hinweis" rows={2} value={edit.note ?? ""} onChange={(e) => setEdit({ ...edit, note: e.target.value })} /></div>
            <Checkbox label="Aktiv" checked={edit.enabled} onChange={(v) => setEdit({ ...edit, enabled: v })} />
          </div>
        </Modal>
      )}
    </Card>
  );
}
