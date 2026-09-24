import { useState } from "react";
import { Link } from "react-router-dom";
import PairingBox from "../components/PairingBox";
import { Button, Card, ErrorBox, Input, Modal, PageHeader, Select, StatusBadge, StatusDot, Table, useAction } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtAgo } from "../lib/format";
import { useLive } from "../lib/live";
import type { Device, PairingInfo, Site } from "../lib/types";
import { useFetch } from "../lib/useFetch";

export default function Devices() {
  const { can, me } = useAuth();
  const devices = useFetch<Device[]>("/devices");
  const sites = useFetch<Site[]>("/sites");
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState("");
  useLive(() => void devices.reload(), ["device.status", "device.paired"]);
  const siteName = (id: string | null) => sites.data?.find((s) => s.id === id)?.name ?? "–";
  const list = devices.data?.filter((d) => !filter || `${d.name} ${d.serial} ${d.tunnel_ip} ${d.tags.join(" ")}`.toLowerCase().includes(filter.toLowerCase()));
  const needsTenant = me?.user.is_superuser && !me.active_tenant_id;

  return (
    <>
      <PageHeader
        title="Geräte"
        subtitle="MikroTik-Router aller Standorte"
        actions={can("technician") && <Button onClick={() => setOpen(true)} disabled={needsTenant} title={needsTenant ? "Bitte zuerst Mandant wählen" : ""}>+ Gerät hinzufügen</Button>}
      />
      <Card>
        <div className="mb-4 max-w-xs">
          <Input placeholder="Suchen (Name, Serial, IP, Tag) …" value={filter} onChange={(e) => setFilter(e.target.value)} />
        </div>
        <ErrorBox error={devices.error} />
        <Table head={["", "Name", "Standort", "Modell", "Serial", "Tunnel-IP", "Pairing", "Zuletzt gesehen"]} empty={list?.length === 0}>
          {list?.map((d) => (
            <tr key={d.id} className="hover:bg-slate-50">
              <td className="px-3 py-2"><StatusDot status={d.pairing_status === "paired" ? d.status : "unknown"} /></td>
              <td className="px-3 py-2 font-medium">
                <Link className="text-brand-700 hover:underline" to={`/devices/${d.id}`}>{d.name}</Link>
                {d.tags.map((t) => <span key={t} className="ml-1 rounded bg-slate-100 px-1.5 text-xs text-slate-600">{t}</span>)}
              </td>
              <td className="px-3 py-2">{siteName(d.site_id)}</td>
              <td className="px-3 py-2">{d.model ?? "–"}</td>
              <td className="px-3 py-2 font-mono text-xs">{d.serial ?? "–"}</td>
              <td className="px-3 py-2 font-mono text-xs">{d.tunnel_ip}</td>
              <td className="px-3 py-2"><StatusBadge status={d.pairing_status} /></td>
              <td className="px-3 py-2 text-slate-500">{fmtAgo(d.last_seen_at)}</td>
            </tr>
          ))}
        </Table>
      </Card>
      <AddDeviceModal open={open} onClose={() => setOpen(false)} sites={sites.data ?? []} onCreated={() => void devices.reload()} />
    </>
  );
}

function AddDeviceModal({ open, onClose, sites, onCreated }: { open: boolean; onClose: () => void; sites: Site[]; onCreated: () => void }) {
  const [name, setName] = useState("");
  const [siteId, setSiteId] = useState("");
  const [serial, setSerial] = useState("");
  const [tags, setTags] = useState("");
  const [pairing, setPairing] = useState<PairingInfo | null>(null);
  const { busy, error, run } = useAction();
  const close = () => {
    setPairing(null);
    setName("");
    setSerial("");
    onClose();
  };
  return (
    <Modal open={open} onClose={close} title={pairing ? "Onboarding-Befehl" : "Gerät hinzufügen"} wide={!!pairing}>
      {pairing ? (
        <>
          <PairingBox pairing={pairing} />
          <div className="mt-4 text-right"><Button onClick={close}>Fertig</Button></div>
        </>
      ) : (
        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            void run(async () => {
              const r = await api.post<{ pairing: PairingInfo }>("/devices", {
                name,
                site_id: siteId || null,
                serial: serial || null,
                tags: tags.split(",").map((t) => t.trim()).filter(Boolean),
              });
              setPairing(r.pairing);
              onCreated();
            });
          }}
        >
          <ErrorBox error={error} />
          <Input label="Name" value={name} onChange={(e) => setName(e.target.value)} required placeholder="z. B. fil-wien-rtr01" />
          <Select label="Standort" value={siteId} onChange={(e) => setSiteId(e.target.value)}>
            <option value="">– kein Standort –</option>
            {sites.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </Select>
          <Input label="Seriennummer (optional, bindet den Token an die Hardware)" value={serial} onChange={(e) => setSerial(e.target.value)} />
          <Input label="Tags (kommagetrennt)" value={tags} onChange={(e) => setTags(e.target.value)} />
          <div className="flex justify-end gap-2 pt-2">
            <Button type="button" variant="secondary" onClick={close}>Abbrechen</Button>
            <Button disabled={busy}>Anlegen</Button>
          </div>
        </form>
      )}
    </Modal>
  );
}
