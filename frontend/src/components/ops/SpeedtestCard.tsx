import { useState } from "react";
import { api } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { fmtFull } from "../../lib/format";
import type { Device } from "../../lib/types";
import { useFetch } from "../../lib/useFetch";
import { Button, Card, EmptyState, ErrorBox, Loading, Modal, Notice, Pill, ToggleField, useAction } from "../ui";

interface Result { id: string; wan_link_id: string | null; slot: number | null; status: string; down_mbps: number | null; up_mbps: number | null; duration_s: number; bytes_estimated: number | null; error: string | null; triggered_by: string | null; created_at: string }
interface Estimate { enabled: boolean; bytes_estimated: number; duration_s: number; volume_warning: string | null; based_on_last: boolean }

/** Speedtest je WAN mit Verlauf; Volumenwarnung vor dem Start. */
export default function SpeedtestCard({ device, links }: { device: Device; links: { id?: string; slot?: number; name: string }[] }) {
  const { can } = useAuth();
  const data = useFetch<{ enabled: boolean; results: Result[]; weekly: Record<string, boolean> }>(`/devices/${device.id}/speedtests`);
  const [confirm, setConfirm] = useState<{ link: { id: string; name: string }; est: Estimate } | null>(null);
  const { busy, error, run } = useAction();
  const name = (id: string | null) => links.find((l) => l.id === id)?.name ?? "?";
  const start = (link: { id: string; name: string }) => run(async () => {
    const est = await api.get<Estimate>(`/devices/${device.id}/wan/${link.id}/speedtest/estimate`);
    setConfirm({ link, est });
  });
  const d = data.data;
  return (
    <Card flush title="Speedtest" subtitle={d && !d.enabled ? "deaktiviert – kein btest-Server konfiguriert (SPEEDTEST_SERVER)" : "je WAN über eine vorübergehende Route zum Testserver"}>
      <div className="px-4 pt-3"><ErrorBox error={error ?? data.error} /></div>
      {!d ? <Loading rows={2} /> : <>
        <div className="flex flex-wrap gap-3 px-4 py-3">
          {links.filter((l) => l.id).map((l) => (
            <div key={l.id} className="flex items-center gap-3 rounded-md border border-line px-3 py-2">
              <span className="font-medium">{l.name}</span>
              {can("technician") && <Button size="sm" variant="secondary" icon={busy ? "loader" : "activity"} disabled={!d.enabled || busy || device.status !== "online"}
                onClick={() => void start({ id: l.id!, name: l.name })}>Testen</Button>}
              {can("technician") && <ToggleField label="wöchentlich" checked={!!d.weekly[l.id!]} disabled={!d.enabled}
                onChange={(v) => void run(async () => { await api.put(`/devices/${device.id}/wan/${l.id}/speedtest/schedule`, { weekly: v }); await data.reload(); })} />}
            </div>
          ))}
        </div>
        {d.results.length === 0 ? <EmptyState compact title="Noch keine Messungen" /> : (
          <div className="overflow-x-auto"><table className="w-full text-sm">
            <thead><tr className="border-y border-line bg-panel2 text-left text-xs text-fg3"><th className="px-4 py-2 font-medium">Zeitpunkt</th><th className="px-2 py-2 font-medium">WAN</th><th className="px-2 py-2 font-medium">Download</th><th className="px-2 py-2 font-medium">Upload</th><th className="px-2 py-2 font-medium">Status</th><th className="px-2 py-2 font-medium">von</th></tr></thead>
            <tbody>{d.results.map((r) => (
              <tr key={r.id} className="border-b border-line last:border-b-0">
                <td className="px-4 py-2 font-mono text-xs">{fmtFull(r.created_at)}</td><td className="px-2 py-2">{name(r.wan_link_id)}</td>
                <td className="px-2 py-2">{r.down_mbps != null ? `${r.down_mbps.toLocaleString("de-DE")} Mbit/s` : "–"}</td>
                <td className="px-2 py-2">{r.up_mbps != null ? `${r.up_mbps.toLocaleString("de-DE")} Mbit/s` : "–"}</td>
                <td className="px-2 py-2">{r.status === "ok" ? <Pill tone="green">ok</Pill> : r.status === "running" ? <Pill tone="blue" icon="loader">läuft</Pill> : <Pill tone="red" title={r.error ?? ""}>Fehler</Pill>}</td>
                <td className="px-2 py-2 text-xs text-fg2">{r.triggered_by}</td>
              </tr>))}</tbody>
          </table></div>
        )}
      </>}
      {confirm && (
        <Modal open onClose={() => setConfirm(null)} title={`Speedtest über ${confirm.link.name}`}
          footer={<><Button variant="secondary" onClick={() => setConfirm(null)}>Abbrechen</Button><Button icon="play" disabled={busy} onClick={() => void run(async () => {
            await api.post(`/devices/${device.id}/wan/${confirm.link.id}/speedtest`, { confirm_volume: !!confirm.est.volume_warning });
            setConfirm(null); await data.reload();
          })}>Starten</Button></>}>
          <p>Dauer je Richtung {confirm.est.duration_s} s. Geschätzter Datenverbrauch: <b>ca. {Math.round(confirm.est.bytes_estimated / 1e6)} MB</b>{confirm.est.based_on_last ? " (anhand der letzten Messung)" : " (Annahme 100 Mbit/s)"}.</p>
          {confirm.est.volume_warning && <div className="mt-3"><Notice tone="orange" icon="alert" title="WAN mit Volumenlimit">{confirm.est.volume_warning}</Notice></div>}
        </Modal>
      )}
    </Card>
  );
}
