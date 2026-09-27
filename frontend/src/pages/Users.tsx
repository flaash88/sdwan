import { useState } from "react";
import { Badge, Button, Card, ErrorBox, Input, Modal, PageHeader, Select, Table, ToggleField, useAction } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtAgo } from "../lib/format";
import type { User } from "../lib/types";
import { useFetch } from "../lib/useFetch";

export default function Users() {
  const { me } = useAuth();
  const users = useFetch<User[]>("/users");
  const [open, setOpen] = useState(false);
  const [f, setF] = useState({ email: "", full_name: "", password: "", role: "technician", is_superuser: false });
  const { busy, error, run } = useAction();
  const security = useFetch<{ require_2fa: boolean }>(me?.active_tenant_id ? "/tenants/current/security" : null);
  const tenantName = (id: string | null) => (id ? me?.tenants.find((t) => t.id === id)?.name ?? id.slice(0, 8) : "MSP");
  return (
    <>
      <PageHeader title="Benutzer" subtitle="Rollen: Admin · Techniker · Read-Only" actions={<Button onClick={() => setOpen(true)} icon="plus">Benutzer</Button>} />
      {security.data && (
        <div className="mb-3 rounded-md border border-line bg-panel px-4 py-3">
          <ToggleField label="Zwei-Faktor-Anmeldung für alle Benutzer dieses Mandanten verpflichtend (greift bei der nächsten Anmeldung)" checked={security.data.require_2fa}
            onChange={(v) => void run(async () => { await api.put("/tenants/current/security", { require_2fa: v }); await security.reload(); })} />
        </div>
      )}
      <Card>
        <ErrorBox error={error ?? users.error} />
        <Table head={["E-Mail", "Name", "Mandant", "Rolle", "Status", "2FA", "Letzter Login", ""]} empty={users.data?.length === 0}>
          {users.data?.map((u) => (
            <tr key={u.id}>
              <td className="px-3 py-2 font-medium">{u.email}</td>
              <td className="px-3 py-2">{u.full_name ?? "–"}</td>
              <td className="px-3 py-2">{tenantName(u.tenant_id)}</td>
              <td className="px-3 py-2">{u.is_superuser ? <Badge color="blue">MSP-Admin</Badge> : <Badge>{u.role}</Badge>}</td>
              <td className="px-3 py-2">{!u.is_active ? <Badge color="red">gesperrt</Badge> : u.locked_until && new Date(u.locked_until) > new Date() ? <Badge color="orange">Fehlversuche</Badge> : <Badge color="green">aktiv</Badge>}</td>
              <td className="px-3 py-2">{u.totp_enabled ? <Badge color="green">aktiv</Badge> : <Badge>aus</Badge>}</td>
              <td className="px-3 py-2 text-slate-500">{fmtAgo(u.last_login_at)}</td>
              <td className="px-3 py-2 text-right">
                {u.id !== me?.user.id && (
                  <>
                    {u.locked_until && new Date(u.locked_until) > new Date() && <Button variant="ghost" onClick={() => void run(async () => { await api.post(`/users/${u.id}/unlock`); await users.reload(); })}>Sperre aufheben</Button>}
                    {me?.user.is_superuser && u.totp_enabled && <Button variant="ghost" onClick={() => confirm(`2FA von ${u.email} zurücksetzen? Der Benutzer muss sie bei der nächsten Anmeldung neu einrichten.`) && void run(async () => { await api.post(`/users/${u.id}/2fa/reset`); await users.reload(); })}>2FA zurücksetzen</Button>}
                    <Button variant="ghost" onClick={() => void run(async () => { await api.patch(`/users/${u.id}`, { is_active: !u.is_active }); await users.reload(); })}>{u.is_active ? "Sperren" : "Entsperren"}</Button>
                    <Button variant="ghost" onClick={() => confirm(`${u.email} löschen?`) && void run(async () => { await api.del(`/users/${u.id}`); await users.reload(); })}>Löschen</Button>
                  </>
                )}
              </td>
            </tr>
          ))}
        </Table>
      </Card>
      <Modal open={open} onClose={() => setOpen(false)} title="Neuer Benutzer"
        footer={<><Button variant="secondary" onClick={() => setOpen(false)}>Abbrechen</Button><Button form="user-form" disabled={busy}>Anlegen</Button></>}>
        <form
          id="user-form"
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            void run(async () => {
              await api.post("/users", { ...f, full_name: f.full_name || null, tenant_id: me?.active_tenant_id ?? null });
              setOpen(false);
              await users.reload();
            });
          }}
        >
          <ErrorBox error={error} />
          <Input label="E-Mail" type="email" required value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} />
          <Input label="Name" value={f.full_name} onChange={(e) => setF({ ...f, full_name: e.target.value })} />
          <Input label="Initiales Passwort" hint="Mindestens 8 Zeichen" type="password" required minLength={8} value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} />
          <Select label="Rolle" value={f.role} onChange={(e) => setF({ ...f, role: e.target.value })}>
            <option value="admin">Admin</option>
            <option value="technician">Techniker</option>
            <option value="readonly">Read-Only</option>
          </Select>
          {me?.user.is_superuser && (
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={f.is_superuser} onChange={(e) => setF({ ...f, is_superuser: e.target.checked })} /> MSP-Admin (Zugriff auf alle Mandanten)
            </label>
          )}
          {me?.user.is_superuser && !f.is_superuser && !me.active_tenant_id && <p className="text-xs text-amber-600">Bitte oben zuerst einen Mandanten wählen – der Benutzer wird diesem zugeordnet.</p>}
        </form>
      </Modal>
    </>
  );
}
