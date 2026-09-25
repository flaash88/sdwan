import { useEffect, useState } from "react";
import { Badge, Button, Card, Checkbox, ErrorBox, Input, Select, Table, useAction } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtAgo } from "../lib/format";
import { ifaceLabel, useInterfaces } from "../lib/interfaces";
import { useLive } from "../lib/live";
import { useMeta } from "../lib/meta";
import type { Device } from "../lib/types";
import { useFetch } from "../lib/useFetch";

interface VrrpInstance {
  id?: string;
  name: string;
  interface: string;
  vrid: number;
  priority: number;
  interval_ms: number;
  preemption: boolean;
  version: number;
  vip: string;
  local_address: string | null;
  linked_wan_slot: number | null;
  enabled: boolean;
  state?: string;
  last_change_at?: string | null;
}
interface VrrpConfig { instances: VrrpInstance[]; last_error: string | null }
interface WanSlot { slot: number; name: string; interface: string }

const blank = (i: number): VrrpInstance => ({ name: `vrrp${i + 1}`, interface: "", vrid: 1, priority: 100, interval_ms: 1000, preemption: true, version: 3, vip: "", local_address: null, linked_wan_slot: null, enabled: true });

function StateBadge({ state }: { state?: string }) {
  if (state === "master") return <Badge color="yellow">Master – Router übernimmt</Badge>;
  if (state === "backup") return <Badge color="green">Backup – Hauptsystem aktiv</Badge>;
  if (state === "disabled") return <Badge color="gray">deaktiviert</Badge>;
  return <Badge color="gray">unbekannt</Badge>;
}

export default function VrrpTab({ device }: { device: Device }) {
  const { can } = useAuth();
  const meta = useMeta();
  const vrrp = useFetch<VrrpConfig>(`/devices/${device.id}/vrrp`);
  const wan = useFetch<{ links: WanSlot[] }>(`/devices/${device.id}/wan`);
  const ifaces = useInterfaces(device.id);
  const [draft, setDraft] = useState<VrrpInstance[] | null>(null);
  const { busy, error, run } = useAction();
  useEffect(() => { if (vrrp.data && !draft) setDraft(structuredClone(vrrp.data.instances)); }, [vrrp.data, draft]);
  useLive((e) => { if ((e.data as { device_id?: string }).device_id === device.id) void vrrp.reload(); }, ["vrrp.state", "wan.link"]);
  if (!draft || !vrrp.data) return <ErrorBox error={vrrp.error} />;
  const set = (i: number, patch: Partial<VrrpInstance>) => setDraft(draft.map((x, j) => (j === i ? { ...x, ...patch } : x)));
  const editable = can("technician");
  const slots = wan.data?.links ?? [];

  return (
    <div className="space-y-6">
      <Card title="VRRP-Status" actions={editable && <Button variant="secondary" disabled={busy} onClick={() => void run(async () => { await api.post(`/devices/${device.id}/vrrp/apply`); await vrrp.reload(); })}>Erneut anwenden</Button>}>
        <ErrorBox error={vrrp.data.last_error} />
        <Table head={["Instanz", "Interface", "VRID", "Priorität", "VIP", "Gekoppeltes WAN", "Zustand", "Letzter Wechsel", ""]} empty={vrrp.data.instances.length === 0}>
          {vrrp.data.instances.map((v) => (
            <tr key={v.id}>
              <td className="px-3 py-2 font-medium">{v.name}</td>
              <td className="px-3 py-2 font-mono text-xs">{v.interface}</td>
              <td className="px-3 py-2">{v.vrid}</td>
              <td className="px-3 py-2">{v.priority}</td>
              <td className="px-3 py-2 font-mono text-xs">{v.vip}</td>
              <td className="px-3 py-2">{v.linked_wan_slot ? `WAN${v.linked_wan_slot}` : "–"}</td>
              <td className="px-3 py-2"><StateBadge state={v.state} /></td>
              <td className="px-3 py-2 text-slate-500">{fmtAgo(v.last_change_at)}</td>
              <td className="whitespace-nowrap px-3 py-2 text-right">
                {editable && meta?.simulator && (
                  <Button variant="ghost" onClick={() => void run(async () => { await api.post(`/devices/${device.id}/vrrp/${v.id}/simulate?master=${v.state !== "master"}`); })}>
                    {v.state === "master" ? "Backup werden" : "Master werden"}
                  </Button>
                )}
              </td>
            </tr>
          ))}
        </Table>
        <p className="mt-3 text-xs text-slate-500">
          Der Router läuft als VRRP-Backup hinter dem Hauptsystem (z. B. FortiGate, Priorität 255). Wird er Master, deaktiviert das on-master-Skript sofort die Default-Route des gekoppelten WAN
          und leert dessen Verbindungen – der Verkehr läuft über das Backup-WAN. Zurückgeschaltet wird erst, wenn die Netwatch des WAN wieder „up“ meldet.
        </p>
      </Card>

      {editable && (
        <Card title="Konfiguration">
          <ErrorBox error={error} />
          <div className="space-y-4">
            {draft.map((v, i) => (
              <div key={i} className="rounded-lg border border-slate-200 p-4">
                <div className="mb-3 flex items-center justify-between">
                  <span className="font-medium">VRRP-Instanz {i + 1}</span>
                  <div className="flex items-center gap-3">
                    <Checkbox label="aktiv" checked={v.enabled} onChange={(c) => set(i, { enabled: c })} />
                    <Button variant="ghost" onClick={() => setDraft(draft.filter((_, j) => j !== i))}>Entfernen</Button>
                  </div>
                </div>
                <div className="grid gap-3 md:grid-cols-4">
                  <Input label="Name (VRRP-Interface)" value={v.name} onChange={(e) => set(i, { name: e.target.value })} />
                  <Select label="Interface" value={v.interface} onChange={(e) => set(i, { interface: e.target.value })}>
                    {!ifaces.data?.some((x) => x.name === v.interface) && <option value={v.interface}>{v.interface || "– wählen –"}</option>}
                    {ifaces.data?.filter((x) => x.type !== "vrrp").map((x) => <option key={x.name} value={x.name}>{ifaceLabel(x.name, x)}</option>)}
                  </Select>
                  <Input label="VRID (1–255, wie Hauptsystem)" type="number" min={1} max={255} value={v.vrid} onChange={(e) => set(i, { vrid: Number(e.target.value) })} />
                  <Input label="Priorität (1–254, Backup < Master)" type="number" min={1} max={254} value={v.priority} onChange={(e) => set(i, { priority: Number(e.target.value) })} />
                  <Input label="Virtuelle IP (VIP, /32)" placeholder="192.168.110.1" value={v.vip} onChange={(e) => set(i, { vip: e.target.value })} />
                  <Input label="Lokale Adresse (optional, mit Präfix)" placeholder="192.168.110.21/24" value={v.local_address ?? ""} onChange={(e) => set(i, { local_address: e.target.value || null })} />
                  <Select label="Gekoppeltes WAN (bei Master abschalten)" value={v.linked_wan_slot ?? ""} onChange={(e) => set(i, { linked_wan_slot: e.target.value ? Number(e.target.value) : null })}>
                    <option value="">– keines –</option>
                    {slots.map((l) => <option key={l.slot} value={l.slot}>WAN{l.slot} – {l.name} ({l.interface})</option>)}
                  </Select>
                  <Input label="Intervall (ms)" type="number" min={10} value={v.interval_ms} onChange={(e) => set(i, { interval_ms: Number(e.target.value) })} />
                  <Select label="VRRP-Version" value={v.version} onChange={(e) => set(i, { version: Number(e.target.value) })}>
                    <option value={3}>3 (Standard RouterOS 7)</option>
                    <option value={2}>2</option>
                  </Select>
                  <div className="pt-6"><Checkbox label="Preemption" checked={v.preemption} onChange={(c) => set(i, { preemption: c })} /></div>
                </div>
              </div>
            ))}
          </div>
          <div className="mt-4 flex justify-between">
            <Button variant="secondary" onClick={() => setDraft([...draft, blank(draft.length)])}>+ VRRP-Instanz</Button>
            <Button disabled={busy} onClick={() => void run(async () => {
              const r = await api.put<{ instances: VrrpInstance[]; push: { ok: boolean; error?: string } | null }>(`/devices/${device.id}/vrrp`, { instances: draft, push: true });
              setDraft(structuredClone(r.instances));
              await vrrp.reload();
              if (r.push && !r.push.ok) throw new Error(`Gespeichert, aber Push fehlgeschlagen: ${r.push.error}`);
            })}>Speichern & auf Router anwenden</Button>
          </div>
        </Card>
      )}
    </div>
  );
}
