import { Button, Card, EmptyState, ErrorBox, Loading, Notice, PageHeader, Pill, useAction, type Tone } from "../components/ui";
import { api } from "../lib/api";
import { fmtAgo, fmtBytes, fmtFull } from "../lib/format";
import { useFetch } from "../lib/useFetch";

interface Backup {
  id: string; status: string; trigger: string; filename: string | null; size: number; sha256: string | null; targets: Record<string, unknown>;
  contents: { parts?: Record<string, { size: number }>; warnings?: string[]; revision?: string | null }; error: string | null; started_by: string | null;
  created_at: string; finished_at: string | null;
}
interface Data { config: { configured: boolean; hour_utc: number; directory: string; keep_days: number; remote: string | null; influx: boolean }; last_ok: Backup | null; backups: Backup[] }
interface PAlert { id: string; label: string; status: string; message: string; fired_at: string; resolved_at: string | null }

const ST: Record<string, [string, Tone]> = { ok: ["erfolgreich", "green"], failed: ["fehlgeschlagen", "red"], running: ["läuft", "blue"], queued: ["angefordert", "blue"], not_configured: ["nicht konfiguriert", "orange"] };

/** Sicherung der Plattform selbst (nur MSP-Admin): Status, Ziele, Verlauf, Plattform-Alarme. */
export default function PlatformBackup() {
  const data = useFetch<Data>("/platform/backups");
  const alerts = useFetch<PAlert[]>("/platform/alerts");
  const { busy, error, run } = useAction();
  const d = data.data;
  const firing = (alerts.data ?? []).filter((a) => a.status === "firing");
  return (
    <>
      <PageHeader title="Plattform-Sicherung" subtitle="Datenbank, Schlüssel (.env) und WireGuard-Hub – verschlüsselt mit age. Anleitung: docs/DISASTER-RECOVERY.md"
        actions={<Button icon="archive" disabled={busy || !d?.config.configured} onClick={() => void run(async () => { await api.post("/platform/backups"); await data.reload(); })}>Jetzt sichern</Button>} />
      <ErrorBox error={error ?? data.error} />
      {firing.map((a) => <div key={a.id} className="mb-3"><Notice tone="red" icon="alert" title={`${a.label} (seit ${fmtFull(a.fired_at)})`}>{a.message}</Notice></div>)}
      {!d ? <Loading rows={4} /> : <>
        {!d.config.configured && <div className="mb-4"><Notice tone="orange" icon="alert" title="Keine Sicherung konfiguriert">PLATFORM_BACKUP_AGE_RECIPIENT (age-Public-Key) in .env setzen. Der Private Key gehört nicht auf diesen Server.</Notice></div>}
        <div className="mb-4 grid gap-4 md:grid-cols-3">
          <Card title="Letzte erfolgreiche Sicherung">{d.last_ok ? <><div className="text-2xl font-semibold">{fmtAgo(d.last_ok.created_at)}</div><div className="text-sm text-fg2">{fmtFull(d.last_ok.created_at)} · {fmtBytes(d.last_ok.size)}</div></> : <span className="text-fg3">noch keine</span>}</Card>
          <Card title="Ziele"><div className="text-sm">Lokal: <span className="font-mono">{d.config.directory}</span> ({d.config.keep_days} Tage)</div><div className="text-sm">Extern: {d.config.remote ? <span className="font-mono">{d.config.remote}</span> : <span className="text-fg3">nicht konfiguriert</span>}</div><div className="text-sm">InfluxDB: {d.config.influx ? "ja" : "nein"}</div></Card>
          <Card title="Zeitplan"><div className="text-sm">täglich {String(d.config.hour_utc).padStart(2, "0")}:10 UTC</div><div className="mt-1 text-xs text-fg3">Testwiederherstellung regelmäßig durchführen (DISASTER-RECOVERY.md)</div></Card>
        </div>
        <Card flush title="Verlauf">
          {d.backups.length === 0 ? <EmptyState compact title="Noch keine Sicherungen" /> : (
            <table className="w-full text-sm">
              <thead><tr className="border-b border-line bg-panel2 text-left text-xs text-fg3"><th className="px-4 py-2 font-medium">Zeitpunkt</th><th className="px-2 font-medium">Status</th><th className="px-2 font-medium">Datei</th><th className="px-2 font-medium">Größe</th><th className="px-2 font-medium">Ziele</th><th className="px-2 font-medium">Hinweise</th></tr></thead>
              <tbody>{d.backups.map((b) => { const [l, t] = ST[b.status] ?? [b.status, "gray"]; return (
                <tr key={b.id} className="border-b border-line align-top last:border-b-0">
                  <td className="px-4 py-2 text-xs">{fmtFull(b.created_at)}<div className="text-fg3">{b.trigger}{b.started_by ? ` · ${b.started_by}` : ""}</div></td>
                  <td className="px-2 py-2"><Pill tone={t}>{l}</Pill></td>
                  <td className="px-2 py-2 font-mono text-xs">{b.filename ?? "–"}</td>
                  <td className="px-2 py-2 text-xs">{b.size ? fmtBytes(b.size) : "–"}</td>
                  <td className="px-2 py-2 text-xs">{Object.entries(b.targets).filter(([k]) => k !== "pruned").map(([k, v]) => `${k}: ${v}`).join(" · ") || "–"}</td>
                  <td className="px-2 py-2 text-xs text-fg2">{b.error ?? (b.contents.warnings ?? []).join("; ")}</td>
                </tr>); })}</tbody>
            </table>
          )}
        </Card>
      </>}
    </>
  );
}
