import { useEffect, useState } from "react";
import { api } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { useFwCatalog } from "../../lib/fw";
import { ifaceLabel, useInterfaces } from "../../lib/interfaces";
import type { Device } from "../../lib/types";
import { useFetch } from "../../lib/useFetch";
import { Button, Card, EmptyState, ErrorBox, Loading, Notice, Select, useAction } from "../ui";

/** Zuordnung Interface → Firewall-Zone (Interface-Lists sdwan-zone-*) und Zähler-Reset. */
export default function DeviceZones({ device }: { device: Device }) {
  const { can } = useAuth();
  const cat = useFwCatalog();
  const ifaces = useInterfaces(device.id);
  const cur = useFetch<{ members: { interface: string; zone_id: string }[]; last_error: string | null }>(`/devices/${device.id}/zones`);
  const [map, setMap] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState<string | null>(null);
  const { busy, error, run } = useAction();
  useEffect(() => { if (cur.data) setMap(Object.fromEntries(cur.data.members.map((m) => [m.interface, m.zone_id]))); }, [cur.data]);
  const zones = cat.data?.zones.filter((z) => z.source !== "wan") ?? [];
  const dirty = JSON.stringify(Object.fromEntries(Object.entries(map).filter(([, v]) => v))) !== JSON.stringify(Object.fromEntries((cur.data?.members ?? []).map((m) => [m.interface, m.zone_id])));
  const editable = can("technician");
  const list = (ifaces.data ?? []).filter((i) => i.type !== "wg" || i.name !== "sdwan-mgmt");
  return (
    <Card flush title="Firewall-Zonen" subtitle="Interface → Zone; die Zone „WAN“ folgt automatisch der WAN-Konfiguration"
      actions={editable && <>
        <Button size="sm" variant="secondary" icon="rotate" disabled={busy || device.status !== "online"} title="Trefferzähler der verwalteten Regeln auf 0 setzen"
          onClick={() => confirm("Trefferzähler der verwalteten Firewall-Regeln zurücksetzen?") && void run(async () => { const r = await api.post<{ reset: number }>(`/devices/${device.id}/firewall/reset-counters`); setMsg(`${r.reset} Zähler zurückgesetzt`); })}>Zähler zurücksetzen</Button>
        <Button size="sm" disabled={busy || !dirty} onClick={() => void run(async () => {
          const r = await api.put<{ apply: { ok: boolean; error?: string } | null }>(`/devices/${device.id}/zones`, { members: Object.entries(map).filter(([, z]) => z).map(([interface_, zone_id]) => ({ interface: interface_, zone_id })) });
          setMsg(r.apply?.ok ? "Zonen gespeichert und auf den Router übertragen" : `Gespeichert, Übertragung fehlgeschlagen: ${r.apply?.error}`);
          await cur.reload();
        })}>Speichern & anwenden</Button>
      </>}>
      <div className="px-4 pt-3"><ErrorBox error={error ?? cur.data?.last_error} />{msg && <Notice tone="green">{msg}</Notice>}</div>
      {!cat.data || !ifaces.data ? <Loading rows={3} /> : list.length === 0 ? <EmptyState compact title="Keine Interfaces gemeldet" /> : (
        <div className="grid gap-x-6 px-4 pb-3 sm:grid-cols-2 xl:grid-cols-3">
          {list.map((i) => (
            <Select key={i.name} label={ifaceLabel(i.name, i)} disabled={!editable} value={map[i.name] ?? ""} onChange={(e) => setMap({ ...map, [i.name]: e.target.value })}>
              <option value="">– keine Zone –</option>{zones.map((z) => <option key={z.id} value={z.id}>{z.name}</option>)}
            </Select>
          ))}
        </div>
      )}
    </Card>
  );
}
