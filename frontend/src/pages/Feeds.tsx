import { useState } from "react";
import { Link } from "react-router-dom";
import TargetsModal from "../components/TargetsModal";
import { Button, Card, EmptyState, ErrorBox, IconButton, Input, Loading, Modal, Notice, PageHeader, Pill, Select, ToggleField, useAction, type Tone } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtAgo, fmtFull } from "../lib/format";
import { useFetch } from "../lib/useFetch";

interface Assignment { device_id: string; device: string; status: string; synced_count: number; last_sync_at: string | null; last_error: string | null; current: boolean }
interface Feed {
  id: string; name: string; slug: string; url: string; fmt: "lines" | "jsonl"; json_field: string; comment_chars: string; interval_min: number; max_entries: number;
  enabled: boolean; description: string | null; builtin: boolean; scope: "global" | "tenant"; list_name: string; count: number; rejected: number;
  last_fetch_at: string | null; last_ok_at: string | null; last_error: string | null; stale: boolean; assignments: Assignment[];
}

const A_STATUS: Record<string, [string, Tone]> = { ok: ["aktuell", "green"], pending: ["ausstehend", "gray"], skipped_memory: ["zu wenig RAM", "orange"], error: ["Fehler", "red"] };

function FeedState({ f }: { f: Feed }) {
  if (!f.enabled) return <Pill tone="gray" icon="pause">deaktiviert</Pill>;
  if (!f.assignments.length) return <Pill tone="gray">nicht zugewiesen</Pill>;
  if (f.stale) return <Pill tone="red" icon="alert">veraltet</Pill>;
  if (f.last_error) return <Pill tone="orange" icon="alert" title={f.last_error}>Fehler, alte Liste aktiv</Pill>;
  if (!f.last_ok_at) return <Pill tone="gray">noch nicht geladen</Pill>;
  return <Pill tone="green" icon="checkCircle">aktuell</Pill>;
}

/** Threat-Feeds: Blocklisten laden und als Address-List sdwan-feed-<kürzel> an Geräte verteilen. */
export default function Feeds() {
  const { can } = useAuth();
  const list = useFetch<Feed[]>("/feeds");
  const [edit, setEdit] = useState<Partial<Feed> | null>(null);
  const [assign, setAssign] = useState<Feed | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const { busy, error, run } = useAction();
  return (
    <>
      <PageHeader title="Threat-Feeds" subtitle={<>Blocklisten als Address-Lists auf den Geräten – im <Link to="/policies" className="hover:underline">Firewall-Editor</Link> als Objekt nutzbar</>}
        actions={can("admin") && <Button icon="plus" onClick={() => setEdit({ fmt: "lines", comment_chars: "#;", json_field: "cidr", interval_min: 360, max_entries: 20000, enabled: true })}>Feed</Button>} />
      <ErrorBox error={error ?? list.error} />
      <Card flush>
        {!list.data ? <Loading rows={3} /> : list.data.length === 0 ? <EmptyState compact title="Keine Feeds" /> : list.data.map((f) => (
          <div key={f.id} className="border-b border-line last:border-b-0">
            <div className="flex flex-wrap items-center gap-3 px-4 py-3">
              <button className="flex min-w-0 flex-1 flex-col text-left" onClick={() => setOpen(open === f.id ? null : f.id)} aria-expanded={open === f.id}>
                <span className="flex flex-wrap items-center gap-2 font-medium">{f.name}{f.builtin ? <Pill tone="gray">vordefiniert</Pill> : f.scope === "global" ? <Pill tone="blue">global</Pill> : <Pill>Mandant</Pill>}</span>
                <span className="truncate text-xs text-fg2"><span className="font-mono">{f.list_name}</span> · {f.count.toLocaleString("de-DE")} Einträge{f.rejected ? ` (${f.rejected} verworfen)` : ""} · alle {f.interval_min} min · {f.last_ok_at ? `aktualisiert ${fmtAgo(f.last_ok_at)}` : "noch nie geladen"}</span>
              </button>
              <FeedState f={f} />
              <span className="text-xs text-fg2">{f.assignments.length} Geräte</span>
              {can("technician") && <>
                <Button size="sm" variant="secondary" icon="rotate" disabled={busy || !f.assignments.length} title={!f.assignments.length ? "Erst Geräten zuweisen" : ""}
                  onClick={() => void run(async () => { await api.post(`/feeds/${f.id}/refresh`); await list.reload(); })}>Jetzt laden</Button>
                <Button size="sm" variant="secondary" icon="plus" onClick={() => setAssign(f)}>Zuweisen</Button>
              </>}
              {can("admin") && <IconButton icon="edit" label="Bearbeiten" onClick={() => setEdit(f)} />}
              {can("admin") && !f.builtin && <IconButton icon="trash" label="Löschen" onClick={() => confirm(`${f.name} löschen?`) && void run(async () => { await api.del(`/feeds/${f.id}`); await list.reload(); })} />}
            </div>
            {open === f.id && (
              <div className="flex flex-col gap-3 border-t border-line bg-panel2 px-4 py-3">
                {f.description && <p className="text-fg2">{f.description}</p>}
                <p className="break-all font-mono text-xs text-fg2">{f.url}</p>
                {f.last_error && <Notice tone="orange" icon="alert" title="Letzter Ladefehler – die letzte gültige Liste bleibt aktiv">{f.last_error}</Notice>}
                {f.assignments.length === 0 ? <p className="text-fg3">Keinem Gerät zugewiesen – der Feed wird nicht geladen.</p> : (
                  <div className="rounded-md border border-line bg-panel">
                    {f.assignments.map((a) => {
                      const [label, tone] = A_STATUS[a.status] ?? [a.status, "gray" as Tone];
                      return (
                        <div key={a.device_id} className="flex flex-wrap items-center gap-3 border-b border-line px-3 py-2 last:border-b-0">
                          <Link to={`/devices/${a.device_id}`} className="font-medium hover:underline">{a.device}</Link>
                          <Pill tone={tone}>{label}</Pill>
                          {a.status === "ok" && !a.current && <Pill tone="orange">Update ausstehend</Pill>}
                          <span className="text-xs text-fg2">{a.synced_count.toLocaleString("de-DE")} Einträge{a.last_sync_at && ` · ${fmtFull(a.last_sync_at)}`}</span>
                          {a.last_error && <span className="text-xs text-orange-text">{a.last_error}</span>}
                          <span className="flex-1" />
                          {can("technician") && <IconButton icon="x" label={`Von ${a.device} entfernen`} onClick={() => confirm(`Feed von ${a.device} entfernen? Die Liste wird auf dem Router gelöscht.`) && void run(async () => { await api.del(`/feeds/${f.id}/assign/${a.device_id}`); await list.reload(); })} />}
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            )}
          </div>
        ))}
      </Card>
      {assign && <TargetsModal title={`${assign.name} zuweisen`} onClose={() => setAssign(null)} onSubmit={async (t) => { await api.post(`/feeds/${assign.id}/assign`, t); await list.reload(); }} />}
      {edit && <FeedDialog feed={edit} onClose={() => setEdit(null)} onSaved={async () => { setEdit(null); await list.reload(); }} />}
    </>
  );
}

function FeedDialog({ feed, onClose, onSaved }: { feed: Partial<Feed>; onClose: () => void; onSaved: () => Promise<void> }) {
  const [f, setF] = useState(feed);
  const { busy, error, run } = useAction();
  const ro = !!feed.builtin;
  return (
    <Modal open onClose={onClose} title={feed.id ? `Feed ${feed.name}` : "Neuer Threat-Feed"} size="lg"
      footer={<><Button variant="secondary" onClick={onClose}>Abbrechen</Button><Button disabled={busy || !f.name || !f.url} onClick={() => void run(async () => {
        const body = { name: f.name, url: f.url, fmt: f.fmt, json_field: f.json_field, comment_chars: f.comment_chars, interval_min: Number(f.interval_min), max_entries: Number(f.max_entries), enabled: f.enabled, description: f.description || null };
        if (feed.id) await api.patch(`/feeds/${feed.id}`, body); else await api.post("/feeds", body);
        await onSaved();
      })}>Speichern</Button></>}>
      <ErrorBox error={error} />
      {ro && <Notice tone="blue">Vordefinierter Feed: nur Intervall, Obergrenze und Aktivierung änderbar.</Notice>}
      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <Input label="Name" disabled={ro} value={f.name ?? ""} onChange={(e) => setF({ ...f, name: e.target.value })} />
        <Select label="Format" disabled={ro} value={f.fmt} onChange={(e) => setF({ ...f, fmt: e.target.value as Feed["fmt"] })}>
          <option value="lines">IP/CIDR je Zeile</option><option value="jsonl">JSON je Zeile (Feld wählbar)</option>
        </Select>
        <div className="sm:col-span-2"><Input label="URL" disabled={ro} className="font-mono" value={f.url ?? ""} onChange={(e) => setF({ ...f, url: e.target.value })} /></div>
        {f.fmt === "jsonl" ? <Input label="JSON-Feld" disabled={ro} value={f.json_field ?? ""} onChange={(e) => setF({ ...f, json_field: e.target.value })} />
          : <Input label="Kommentarzeichen" disabled={ro} value={f.comment_chars ?? ""} onChange={(e) => setF({ ...f, comment_chars: e.target.value })} />}
        <Input label="Intervall (Minuten)" type="number" min={15} value={f.interval_min ?? 360} onChange={(e) => setF({ ...f, interval_min: Number(e.target.value) })} />
        <Input label="Obergrenze Einträge" hint="größere Listen werden abgelehnt (RAM der Router)" type="number" min={1} value={f.max_entries ?? 20000} onChange={(e) => setF({ ...f, max_entries: Number(e.target.value) })} />
        <div className="self-end pb-2"><ToggleField label="Aktiv" checked={f.enabled ?? true} onChange={(v) => setF({ ...f, enabled: v })} /></div>
        <div className="sm:col-span-2"><Input label="Beschreibung" disabled={ro} value={f.description ?? ""} onChange={(e) => setF({ ...f, description: e.target.value })} /></div>
      </div>
      <p className="mt-3 text-xs text-fg3">Nur öffentlich routbare IPv4/IPv6-Netze werden übernommen (keine privaten oder reservierten Netze, keine Präfixe breiter als /8 bzw. /16).</p>
    </Modal>
  );
}
