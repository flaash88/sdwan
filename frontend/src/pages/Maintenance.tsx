import { useState } from "react";
import { Button, Card, EmptyState, ErrorBox, IconButton, Input, Loading, Modal, PageHeader, Pill, Select, Textarea, ToggleField, useAction } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtFull } from "../lib/format";
import type { Device, Site } from "../lib/types";
import { useFetch } from "../lib/useFetch";

interface Win {
  id?: string; name: string; site_id: string | null; device_id: string | null; kind: "once" | "weekly"; start_at: string | null; weekdays: number[];
  start_time: string | null; duration_min: number; suppress_alerts: boolean; firmware_allowed: boolean; enabled: boolean; note: string | null; active?: boolean;
}
const DAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
const EMPTY: Win = { name: "", site_id: null, device_id: null, kind: "weekly", start_at: null, weekdays: [], start_time: "22:00", duration_min: 120, suppress_alerts: true, firmware_allowed: true, enabled: true, note: null };

function localInput(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

/** Wartungsfenster je Mandant/Standort/Gerät: Alarme unterdrücken, Firmware-Rollouts optional nur im Fenster. */
export default function Maintenance() {
  const { can } = useAuth();
  const list = useFetch<Win[]>("/maintenance-windows");
  const sites = useFetch<Site[]>("/sites");
  const devices = useFetch<Device[]>("/devices");
  const [edit, setEdit] = useState<Win | null>(null);
  const { busy, error, run } = useAction();
  const scope = (w: Win) => w.device_id ? `Gerät ${devices.data?.find((d) => d.id === w.device_id)?.name ?? "?"}` : w.site_id ? `Standort ${sites.data?.find((s) => s.id === w.site_id)?.name ?? "?"}` : "ganzer Mandant";
  const when = (w: Win) => w.kind === "once" ? `einmalig ab ${w.start_at ? fmtFull(w.start_at) : "?"}` : `${w.weekdays.map((d) => DAYS[d]).join(", ")} ab ${w.start_time}`;
  const save = () => edit && run(async () => {
    const body = { ...edit, start_at: edit.kind === "once" && edit.start_at ? new Date(edit.start_at).toISOString() : null };
    if (edit.id) await api.put(`/maintenance-windows/${edit.id}`, body); else await api.post("/maintenance-windows", body);
    setEdit(null); await list.reload();
  });
  return (
    <>
      <PageHeader title="Wartungsfenster" subtitle="Zeiten in der Zeitzone des Mandanten. Während eines Fensters werden Alarme unterdrückt (sichtbar markiert); Firmware-Rollouts können auf Fenster beschränkt werden."
        actions={can("technician") && <Button icon="plus" onClick={() => setEdit({ ...EMPTY })}>Wartungsfenster</Button>} />
      <ErrorBox error={error ?? list.error} />
      <Card flush>
        {!list.data ? <Loading rows={3} /> : list.data.length === 0 ? <EmptyState title="Keine Wartungsfenster" text="Ohne Fenster laufen Alarme und Firmware-Rollouts wie bisher." /> : (
          <div className="overflow-x-auto"><table className="w-full text-sm">
            <thead><tr className="border-b border-line bg-panel2 text-left text-xs text-fg3"><th className="px-4 py-2 font-medium">Name</th><th className="px-2 py-2 font-medium">Gilt für</th><th className="px-2 py-2 font-medium">Zeit</th><th className="px-2 py-2 font-medium">Dauer</th><th className="px-2 py-2 font-medium">Wirkung</th><th className="px-2 py-2 font-medium">Status</th><th /></tr></thead>
            <tbody>{list.data.map((w) => (
              <tr key={w.id} className="border-b border-line last:border-b-0">
                <td className="px-4 py-2 font-medium">{w.name}{w.note && <div className="text-xs font-normal text-fg3">{w.note}</div>}</td>
                <td className="px-2 py-2">{scope(w)}</td><td className="px-2 py-2">{when(w)}</td><td className="px-2 py-2">{w.duration_min} min</td>
                <td className="px-2 py-2"><div className="flex flex-wrap gap-1">{w.suppress_alerts && <Pill tone="blue">Alarme aus</Pill>}{w.firmware_allowed && <Pill tone="gray">Firmware erlaubt</Pill>}</div></td>
                <td className="px-2 py-2">{!w.enabled ? <Pill tone="gray" icon="pause">deaktiviert</Pill> : w.active ? <Pill tone="orange" icon="clock">aktiv</Pill> : <Pill tone="gray">wartet</Pill>}</td>
                <td className="px-2 py-2 text-right">{can("technician") && <div className="flex justify-end gap-1">
                  <IconButton icon="edit" label="Bearbeiten" onClick={() => setEdit({ ...w, start_at: localInput(w.start_at) })} />
                  <IconButton icon="trash" label="Löschen" onClick={() => { if (confirm(`Wartungsfenster „${w.name}“ löschen?`)) void run(async () => { await api.del(`/maintenance-windows/${w.id}`); await list.reload(); }); }} />
                </div>}</td>
              </tr>))}</tbody>
          </table></div>
        )}
      </Card>
      {edit && (
        <Modal open onClose={() => setEdit(null)} title={edit.id ? "Wartungsfenster bearbeiten" : "Neues Wartungsfenster"}
          footer={<><Button variant="secondary" onClick={() => setEdit(null)}>Abbrechen</Button><Button disabled={busy || !edit.name} onClick={() => void save()}>Speichern</Button></>}>
          <div className="flex flex-col gap-3">
            <Input label="Name" value={edit.name} onChange={(e) => setEdit({ ...edit, name: e.target.value })} />
            <div className="grid grid-cols-2 gap-3">
              <Select label="Standort" value={edit.site_id ?? ""} disabled={!!edit.device_id} onChange={(e) => setEdit({ ...edit, site_id: e.target.value || null })}>
                <option value="">– alle –</option>{sites.data?.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
              </Select>
              <Select label="Gerät" value={edit.device_id ?? ""} disabled={!!edit.site_id} onChange={(e) => setEdit({ ...edit, device_id: e.target.value || null })}>
                <option value="">– alle –</option>{devices.data?.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
              </Select>
            </div>
            <Select label="Art" value={edit.kind} onChange={(e) => setEdit({ ...edit, kind: e.target.value as Win["kind"] })}>
              <option value="weekly">wöchentlich</option><option value="once">einmalig</option>
            </Select>
            {edit.kind === "once" ? (
              <Input label="Beginn" type="datetime-local" value={edit.start_at ?? ""} onChange={(e) => setEdit({ ...edit, start_at: e.target.value })} />
            ) : (
              <div className="flex flex-wrap items-end gap-3">
                <div className="flex gap-1">{DAYS.map((d, i) => (
                  <button key={d} type="button" onClick={() => setEdit({ ...edit, weekdays: edit.weekdays.includes(i) ? edit.weekdays.filter((x) => x !== i) : [...edit.weekdays, i].sort() })}
                    className={`h-8 w-9 rounded-md border text-xs ${edit.weekdays.includes(i) ? "border-blue bg-blue-bg text-blue-text" : "border-line text-fg2"}`}>{d}</button>
                ))}</div>
                <div className="w-28"><Input label="Beginn" type="time" value={edit.start_time ?? ""} onChange={(e) => setEdit({ ...edit, start_time: e.target.value })} /></div>
              </div>
            )}
            <Input label="Dauer (Minuten)" type="number" min={1} value={edit.duration_min} onChange={(e) => setEdit({ ...edit, duration_min: Number(e.target.value) })} />
            <ToggleField label="Alarme während des Fensters unterdrücken" checked={edit.suppress_alerts} onChange={(v) => setEdit({ ...edit, suppress_alerts: v })} />
            <ToggleField label="Firmware-Rollouts („nur im Fenster“) erlaubt" checked={edit.firmware_allowed} onChange={(v) => setEdit({ ...edit, firmware_allowed: v })} />
            <ToggleField label="Aktiviert" checked={edit.enabled} onChange={(v) => setEdit({ ...edit, enabled: v })} />
            <Textarea label="Notiz" rows={2} value={edit.note ?? ""} onChange={(e) => setEdit({ ...edit, note: e.target.value || null })} />
          </div>
        </Modal>
      )}
    </>
  );
}
