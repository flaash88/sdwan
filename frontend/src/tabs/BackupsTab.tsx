import { useState } from "react";
import DiffView from "../components/DiffView";
import { Badge, Button, Card, ErrorBox, Select, Table, cls, useAction } from "../components/ui";
import { api, download } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtBytes, fmtDate } from "../lib/format";
import type { Device } from "../lib/types";
import { useFetch } from "../lib/useFetch";

interface Backup { id: string; trigger: string; routeros_version: string | null; size: number; pinned: boolean; note: string | null; created_at: string; added: number; removed: number; previous_id: string | null }

export default function BackupsTab({ device }: { device: Device }) {
  const { can } = useAuth();
  const list = useFetch<Backup[]>(`/devices/${device.id}/backups`);
  const [sel, setSel] = useState<string | null>(null);
  const [against, setAgainst] = useState<string>("");
  const [view, setView] = useState<"diff" | "content">("diff");
  const cur = sel ?? list.data?.[0]?.id ?? null;
  const diff = useFetch<{ lines: string[]; added: number; removed: number }>(cur ? `/backups/${cur}/diff${against ? `?against=${against}` : ""}` : null);
  const full = useFetch<{ content: string }>(cur && view === "content" ? `/backups/${cur}` : null);
  const { busy, error, run } = useAction();
  const b = list.data?.find((x) => x.id === cur);
  return (
    <div className="grid gap-6 xl:grid-cols-3">
      <Card title="Konfigurations-Backups" actions={can("technician") && <Button disabled={busy} onClick={() => void run(async () => { await api.post(`/devices/${device.id}/backups`); await list.reload(); setSel(null); })}>Backup jetzt</Button>}>
        <ErrorBox error={error} />
        <p className="mb-3 text-xs text-slate-500">Tägliches Backup (nur bei Änderungen gespeichert). Exporte ohne Passwörter/Schlüssel.</p>
        <Table head={["Zeitpunkt", "Änderung", ""]} empty={list.data?.length === 0}>
          {list.data?.map((x) => (
            <tr key={x.id} onClick={() => { setSel(x.id); setAgainst(""); }} className={cls("cursor-pointer", x.id === cur ? "bg-brand-50" : "hover:bg-slate-50")}>
              <td className="px-3 py-2">{fmtDate(x.created_at)}<div className="text-xs text-slate-500">{x.routeros_version} · {fmtBytes(x.size)}</div></td>
              <td className="px-3 py-2 text-xs"><span className="text-emerald-700">+{x.added}</span> / <span className="text-red-700">−{x.removed}</span></td>
              <td className="px-3 py-2">{x.trigger !== "scheduled" && <Badge color={x.trigger === "manual" ? "blue" : "yellow"}>{x.trigger}</Badge>}</td>
            </tr>
          ))}
        </Table>
      </Card>
      <Card className="xl:col-span-2" title={b ? `Backup vom ${fmtDate(b.created_at)}` : "Backup"} actions={b && <>
        <Select value={view} onChange={(e) => setView(e.target.value as "diff" | "content")}><option value="diff">Diff</option><option value="content">Vollständig</option></Select>
        {view === "diff" && <Select value={against} onChange={(e) => setAgainst(e.target.value)}>
          <option value="">gegen vorheriges</option>
          {list.data?.filter((x) => x.id !== cur).map((x) => <option key={x.id} value={x.id}>gegen {fmtDate(x.created_at)}</option>)}
        </Select>}
        <Button variant="secondary" onClick={() => void download(`/backups/${b.id}/download`, `${device.name}-${b.created_at.slice(0, 10)}.rsc`)}>Download</Button>
      </>}>
        {b?.note && <p className="mb-2 text-sm text-slate-600">{b.note}</p>}
        {view === "diff" ? (diff.data ? <><p className="mb-2 text-xs text-slate-500"><span className="text-emerald-700">+{diff.data.added}</span> / <span className="text-red-700">−{diff.data.removed}</span> Zeilen</p><DiffView lines={diff.data.lines} /></> : null)
          : <pre className="max-h-[32rem] overflow-auto rounded-lg bg-slate-900 p-3 font-mono text-xs text-slate-100">{full.data?.content}</pre>}
      </Card>
    </div>
  );
}
