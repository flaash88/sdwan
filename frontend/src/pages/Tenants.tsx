import { useState } from "react";
import { Badge, Button, Card, ErrorBox, Input, Modal, PageHeader, Select, Table, useAction } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtDate } from "../lib/format";
import type { Tenant } from "../lib/types";
import { useFetch } from "../lib/useFetch";

export default function Tenants() {
  const { reload, switchTenant } = useAuth();
  const tenants = useFetch<Tenant[]>("/tenants");
  const [open, setOpen] = useState(false);
  const [f, setF] = useState({ name: "", slug: "", contact_email: "", mesh_topology: "hub_spoke" });
  const { busy, error, run } = useAction();
  return (
    <>
      <PageHeader title="Mandanten" subtitle="Kunden des MSP – strikt voneinander isoliert" actions={<Button onClick={() => setOpen(true)}>+ Mandant</Button>} />
      <Card>
        <Table head={["Name", "Slug", "Kontakt", "Mesh", "Status", "Angelegt", ""]} empty={tenants.data?.length === 0}>
          {tenants.data?.map((t) => (
            <tr key={t.id} className="hover:bg-slate-50">
              <td className="px-3 py-2 font-medium">{t.name}</td>
              <td className="px-3 py-2 font-mono text-xs">{t.slug}</td>
              <td className="px-3 py-2">{t.contact_email ?? "–"}</td>
              <td className="px-3 py-2">{t.mesh_topology}</td>
              <td className="px-3 py-2">{t.is_active ? <Badge color="green">aktiv</Badge> : <Badge color="red">deaktiviert</Badge>}</td>
              <td className="px-3 py-2 text-slate-500">{fmtDate(t.created_at)}</td>
              <td className="px-3 py-2 text-right">
                <Button variant="ghost" onClick={() => switchTenant(t.id)}>Öffnen →</Button>
                <Button variant="ghost" onClick={() => void run(async () => { await api.patch(`/tenants/${t.id}`, { is_active: !t.is_active }); await tenants.reload(); })}>
                  {t.is_active ? "Deaktivieren" : "Aktivieren"}
                </Button>
              </td>
            </tr>
          ))}
        </Table>
      </Card>
      <Modal open={open} onClose={() => setOpen(false)} title="Neuer Mandant">
        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            void run(async () => {
              await api.post("/tenants", { ...f, contact_email: f.contact_email || null });
              setOpen(false);
              await tenants.reload();
              await reload();
            });
          }}
        >
          <ErrorBox error={error} />
          <Input label="Name" value={f.name} required onChange={(e) => setF({ ...f, name: e.target.value, slug: f.slug || e.target.value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") })} />
          <Input label="Slug" value={f.slug} required pattern="[a-z0-9][a-z0-9-]{1,62}" onChange={(e) => setF({ ...f, slug: e.target.value })} />
          <Input label="Kontakt-E-Mail" type="email" value={f.contact_email} onChange={(e) => setF({ ...f, contact_email: e.target.value })} />
          <Select label="VPN-Topologie" value={f.mesh_topology} onChange={(e) => setF({ ...f, mesh_topology: e.target.value })}>
            <option value="hub_spoke">Hub-and-Spoke (Standard)</option>
            <option value="full_mesh">Full-Mesh</option>
            <option value="none">Kein Site-to-Site-VPN</option>
          </Select>
          <div className="flex justify-end gap-2"><Button disabled={busy}>Anlegen</Button></div>
        </form>
      </Modal>
    </>
  );
}
