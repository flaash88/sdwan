import { Button, Card, EmptyState, ErrorBox, Loading, PageHeader, Pill } from "../components/ui";
import { download } from "../lib/api";
import { fmtFull } from "../lib/format";
import { useFetch } from "../lib/useFetch";

interface Archive { id: string; device_name: string; serial: string | null; model: string | null; routeros_version: string | null; mode: string; has_backup: boolean;
  backup_created_at: string | null; created_by: string | null; created_at: string; expires_at: string; steps: { step: number; label: string; ok: boolean }[] }

/** Backups entfernter Geräte (90 Tage), mit Offboarding-Protokoll. */
export default function OffboardingArchive() {
  const list = useFetch<Archive[]>("/offboarding-archives");
  return (
    <>
      <PageHeader title="Offboarding-Archiv" subtitle="Backups und Protokolle entfernter Geräte – automatisch gelöscht nach 90 Tagen" />
      <ErrorBox error={list.error} />
      <Card flush>
        {!list.data ? <Loading rows={3} /> : list.data.length === 0 ? <EmptyState title="Keine entfernten Geräte" /> : (
          <table className="w-full text-sm">
            <thead><tr className="border-b border-line bg-panel2 text-left text-xs text-fg3"><th className="px-4 py-2 font-medium">Gerät</th><th className="px-2 font-medium">Art</th><th className="px-2 font-medium">Entfernt</th><th className="px-2 font-medium">Protokoll</th><th className="px-2 font-medium">Löschung</th><th /></tr></thead>
            <tbody>{list.data.map((a) => (
              <tr key={a.id} className="border-b border-line last:border-b-0 align-top">
                <td className="px-4 py-2"><div className="font-medium">{a.device_name}</div><div className="text-xs text-fg3">{[a.model, a.serial, a.routeros_version].filter(Boolean).join(" · ")}</div></td>
                <td className="px-2 py-2">{a.mode === "clean" ? <Pill tone="green">bereinigt</Pill> : <Pill tone="orange">nur Plattform</Pill>}</td>
                <td className="px-2 py-2 text-xs">{fmtFull(a.created_at)}<div className="text-fg3">{a.created_by}</div></td>
                <td className="px-2 py-2 text-xs">{a.steps.map((s) => <div key={`${s.step}-${s.label}`}>{s.ok ? "✓" : "✗"} {s.label}</div>)}</td>
                <td className="px-2 py-2 text-xs">{fmtFull(a.expires_at)}</td>
                <td className="px-2 py-2 text-right">{a.has_backup && <Button size="sm" variant="secondary" icon="download" onClick={() => void download(`/offboarding-archives/${a.id}/download`, `offboarding-${a.device_name}.rsc`)}>Backup</Button>}</td>
              </tr>))}</tbody>
          </table>
        )}
      </Card>
    </>
  );
}
