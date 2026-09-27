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
  const sug = useFetch<{ suggestions: { list: string; zone_id: string; zone_name: string; interfaces: string[]; applicable: boolean; defconf: boolean; note: string | null }[] }>(
    can("technician") && device.status === "online" ? `/devices/${device.id}/zones/suggestions` : null);
  const defconf = useFetch<{ disabled: { rule_id: string; chain: string | null; action: string | null; comment: string }[]; active: unknown[] | null }>(`/devices/${device.id}/firewall/defconf`);
  const open = (sug.data?.suggestions ?? []).filter((x) => x.applicable && x.interfaces.some((i) => map[i] !== x.zone_id));
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
      <div className="flex flex-col gap-2 px-4 pb-3 pt-3"><ErrorBox error={error ?? cur.data?.last_error} />{msg && <Notice tone="green">{msg}</Notice>}
        {editable && open.length > 0 && (
          <Notice tone="blue" icon="info" title="Vorschlag aus der Werkskonfiguration">
            {open.map((x) => <div key={x.list}>Interface-List „{x.list}“{x.defconf && " (defconf)"}: {x.interfaces.join(", ")} → Zone {x.zone_name}</div>)}
            {(sug.data?.suggestions ?? []).filter((x) => !x.applicable).map((x) => <div key={x.list} className="text-fg3">„{x.list}“: {x.interfaces.join(", ")} – {x.note}</div>)}
            <div className="mt-2"><Button size="sm" variant="secondary" onClick={() => setMap({ ...map, ...Object.fromEntries(open.flatMap((x) => x.interfaces.map((i) => [i, x.zone_id]))) })}>Vorschlag übernehmen</Button>
              <span className="ml-2 text-xs text-fg3">danach „Speichern & anwenden“</span></div>
          </Notice>
        )}
        {(defconf.data?.disabled.length ?? 0) > 0 && (
          <Notice tone="orange" icon="shield" title={`Werks-Firewall (defconf): ${defconf.data!.disabled.length} Regeln von der Plattform deaktiviert`}>
            Durch die Grundregeln der Plattform abgedeckt. Beim Entfernen der letzten Policy mit Default-Drop werden sie automatisch wieder aktiviert.
            <ul className="mt-1 font-mono text-[11.5px] text-fg2">{defconf.data!.disabled.map((r) => <li key={r.rule_id}>{r.chain} {r.action} # {r.comment}</li>)}</ul>
            {editable && <div className="mt-2"><Button size="sm" variant="secondary" disabled={busy} onClick={() => confirm("Die von der Plattform deaktivierten defconf-Regeln wieder aktivieren? Sie liegen dann hinter dem Default-Drop.") && void run(async () => {
              const r = await api.post<{ enabled: string[]; missing: string[] }>(`/devices/${device.id}/firewall/defconf/restore`);
              setMsg(`${r.enabled.length} defconf-Regeln wieder aktiviert${r.missing.length ? `, ${r.missing.length} nicht mehr vorhanden/verändert (nicht angefasst)` : ""}`);
              await defconf.reload();
            })}>defconf-Regeln wieder aktivieren</Button></div>}
          </Notice>
        )}
      </div>
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
