import { useState } from "react";
import { Badge, Button, Card, Checkbox, ErrorBox, Input, Modal, PageHeader, Table, Textarea, useAction } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import type { Device, Site } from "../lib/types";
import { useFetch } from "../lib/useFetch";

export default function Sites() {
  const { can, me } = useAuth();
  const sites = useFetch<Site[]>("/sites");
  const devices = useFetch<Device[]>("/devices");
  const [edit, setEdit] = useState<Partial<Site> | null>(null);
  const count = (id: string) => devices.data?.filter((d) => d.site_id === id).length ?? 0;
  const needsTenant = me?.user.is_superuser && !me.active_tenant_id;
  return (
    <>
      <PageHeader
        title="Standorte"
        subtitle="Sites mit LAN-Netzen – Basis für das VPN-Mesh"
        actions={can("technician") && <Button disabled={needsTenant} title={needsTenant ? "Bitte zuerst Mandant wählen" : ""} onClick={() => setEdit({ lan_subnets: [], is_mesh_hub: false })}>+ Standort</Button>}
      />
      <Card>
        <ErrorBox error={sites.error} />
        <Table head={["Name", "Adresse", "LAN-Netze", "Mesh-Rolle", "Geräte", ""]} empty={sites.data?.length === 0}>
          {sites.data?.map((s) => (
            <tr key={s.id} className="hover:bg-slate-50">
              <td className="px-3 py-2 font-medium">{s.name}</td>
              <td className="px-3 py-2 text-slate-600">{s.address ?? "–"}</td>
              <td className="px-3 py-2 font-mono text-xs">{s.lan_subnets.join(", ") || "–"}</td>
              <td className="px-3 py-2">{s.is_mesh_hub ? <Badge color="blue">Hub</Badge> : <Badge>Spoke</Badge>}</td>
              <td className="px-3 py-2">{count(s.id)}</td>
              <td className="px-3 py-2 text-right">
                {can("technician") && <Button variant="ghost" onClick={() => setEdit(s)}>Bearbeiten</Button>}
              </td>
            </tr>
          ))}
        </Table>
      </Card>
      {edit && <SiteModal site={edit} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); void sites.reload(); }} />}
    </>
  );
}

function SiteModal({ site, onClose, onSaved }: { site: Partial<Site>; onClose: () => void; onSaved: () => void }) {
  const { can } = useAuth();
  const [f, setF] = useState({ ...site, subnets: (site.lan_subnets ?? []).join("\n") });
  const { busy, error, run } = useAction();
  const save = () =>
    run(async () => {
      const body = { name: f.name, address: f.address || null, description: f.description || null, is_mesh_hub: !!f.is_mesh_hub, lan_subnets: f.subnets.split(/[\s,]+/).filter(Boolean) };
      if (site.id) await api.patch(`/sites/${site.id}`, body);
      else await api.post("/sites", body);
      onSaved();
    });
  return (
    <Modal open onClose={onClose} title={site.id ? "Standort bearbeiten" : "Neuer Standort"}>
      <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); void save(); }}>
        <ErrorBox error={error} />
        <Input label="Name" value={f.name ?? ""} onChange={(e) => setF({ ...f, name: e.target.value })} required />
        <Input label="Adresse" value={f.address ?? ""} onChange={(e) => setF({ ...f, address: e.target.value })} />
        <Textarea label="LAN-Netze (eines pro Zeile, z. B. 192.168.10.0/24)" rows={3} value={f.subnets} onChange={(e) => setF({ ...f, subnets: e.target.value })} />
        <Checkbox label="Hub-Standort im Hub-and-Spoke-Mesh (z. B. Zentrale/Rechenzentrum)" checked={!!f.is_mesh_hub} onChange={(v) => setF({ ...f, is_mesh_hub: v })} />
        <Input label="Beschreibung" value={f.description ?? ""} onChange={(e) => setF({ ...f, description: e.target.value })} />
        <div className="flex justify-between pt-2">
          {site.id && can("admin") ? (
            <Button type="button" variant="danger" onClick={() => confirm("Standort löschen?") && void run(async () => { await api.del(`/sites/${site.id}`); onSaved(); })}>Löschen</Button>
          ) : <span />}
          <div className="flex gap-2">
            <Button type="button" variant="secondary" onClick={onClose}>Abbrechen</Button>
            <Button disabled={busy}>Speichern</Button>
          </div>
        </div>
      </form>
    </Modal>
  );
}
