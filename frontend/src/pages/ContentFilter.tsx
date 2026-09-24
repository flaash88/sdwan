import { useState } from "react";
import { Badge, Button, Card, Checkbox, ErrorBox, Input, Modal, PageHeader, Select, StatusBadge, Table, Textarea, cls, useAction } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtDate } from "../lib/format";
import type { Site } from "../lib/types";
import { useFetch } from "../lib/useFetch";

interface Catalog { categories: Record<string, string>; services: string[]; security: Record<string, string>; blocklists: string[]; default_security: Record<string, boolean> }
interface Profile {
  id?: string; name: string; nextdns_profile_id?: string | null; categories: string[]; services: string[]; security: Record<string, boolean>;
  blocklists: string[]; denylist: string[]; allowlist: string[]; safe_search: boolean; youtube_restricted: boolean; block_bypass: boolean; force_dns: boolean;
  sync_status?: string; last_error?: string | null; synced_at?: string | null;
}
interface Assignment { default_profile_id: string | null; api_key_configured: boolean; sites: Record<string, string | null>; devices: { id: string; name: string; site_id: string | null; dns_filter: { profile: string; doh: string; forced: boolean; at: string } | null }[] }

export default function ContentFilter() {
  const { can, me } = useAuth();
  const tenant = me?.active_tenant_id;
  const catalog = useFetch<Catalog>("/content-filter/catalog");
  const profiles = useFetch<Profile[]>(tenant ? "/content-filter/profiles" : null);
  const assign = useFetch<Assignment>(tenant ? "/content-filter/assignment" : null);
  const sites = useFetch<Site[]>("/sites");
  const [edit, setEdit] = useState<Profile | null>(null);
  const [keyOpen, setKeyOpen] = useState(false);
  const { busy, error, run } = useAction();
  if (!tenant) return <><PageHeader title="Content-Filter" /><Card><p className="text-sm text-slate-500">Bitte links einen Mandanten wählen.</p></Card></>;
  const a = assign.data;
  const newProfile = (): Profile => ({ name: "", categories: [], services: [], security: { ...(catalog.data?.default_security ?? {}) }, blocklists: ["nextdns-recommended"], denylist: [], allowlist: [], safe_search: false, youtube_restricted: false, block_bypass: true, force_dns: true });
  const saveAssign = (patch: Partial<{ default_profile_id: string | null; sites: Record<string, string | null> }>) =>
    run(async () => { await api.put("/content-filter/assignment", { default_profile_id: a?.default_profile_id ?? null, sites: {}, ...patch, apply: true }); await assign.reload(); });

  return (
    <>
      <PageHeader title="Content-Filter" subtitle="DNS-Filterung über NextDNS – verschlüsselt per DNS-over-HTTPS direkt auf dem Router"
        actions={<>
          {can("admin") && <Button variant="secondary" onClick={() => setKeyOpen(true)}>NextDNS-API-Key {a?.api_key_configured ? "✓" : ""}</Button>}
          {can("admin") && <Button onClick={() => setEdit(newProfile())}>+ Filterprofil</Button>}
        </>} />
      <ErrorBox error={error} />
      <div className="grid gap-6 xl:grid-cols-2">
        <Card title="Filterprofile">
          <Table head={["Name", "NextDNS", "Blockiert", "Sync", ""]} empty={profiles.data?.length === 0}>
            {profiles.data?.map((p) => (
              <tr key={p.id}>
                <td className="px-3 py-2 font-medium">{p.name}</td>
                <td className="px-3 py-2 font-mono text-xs">{p.nextdns_profile_id ?? "–"}</td>
                <td className="px-3 py-2 text-xs text-slate-600">{p.categories.length} Kategorien · {p.services.length} Dienste · {p.denylist.length} Domains</td>
                <td className="px-3 py-2"><StatusBadge status={p.sync_status === "synced" ? "ok" : p.sync_status === "error" ? "error" : "pending"} />{p.last_error && <div className="max-w-48 truncate text-xs text-red-600" title={p.last_error}>{p.last_error}</div>}</td>
                <td className="px-3 py-2 text-right">
                  {can("admin") && <Button variant="ghost" onClick={() => setEdit(p)}>Bearbeiten</Button>}
                  {can("technician") && p.sync_status !== "synced" && <Button variant="ghost" onClick={() => void run(async () => { await api.post(`/content-filter/profiles/${p.id}/sync`); await profiles.reload(); })}>Sync</Button>}
                </td>
              </tr>
            ))}
          </Table>
        </Card>
        <Card title="Zuweisung" actions={can("technician") && <Button variant="secondary" disabled={busy} onClick={() => void run(async () => { await api.post("/content-filter/apply"); await assign.reload(); })}>Auf Router anwenden</Button>}>
          <div className="space-y-3">
            <Select label="Mandanten-Standard" value={a?.default_profile_id ?? ""} disabled={!can("admin")} onChange={(e) => void saveAssign({ default_profile_id: e.target.value || null })}>
              <option value="">– kein Filter –</option>
              {profiles.data?.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </Select>
            {sites.data?.map((s) => (
              <Select key={s.id} label={`Standort: ${s.name}`} value={a?.sites[s.id] ?? ""} disabled={!can("admin")} onChange={(e) => void saveAssign({ sites: { [s.id]: e.target.value || null } })}>
                <option value="">Mandanten-Standard verwenden</option>
                {profiles.data?.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              </Select>
            ))}
          </div>
        </Card>
      </div>
      <Card title="Status auf den Routern" className="mt-6">
        <Table head={["Gerät", "Profil", "DoH-Endpoint", "DNS erzwungen", "Angewendet"]} empty={a?.devices.length === 0}>
          {a?.devices.map((d) => (
            <tr key={d.id}>
              <td className="px-3 py-2 font-medium">{d.name}</td>
              <td className="px-3 py-2">{d.dns_filter ? <Badge color="green">{d.dns_filter.profile}</Badge> : <Badge>ungefiltert</Badge>}</td>
              <td className="px-3 py-2 font-mono text-xs">{d.dns_filter?.doh ?? "–"}</td>
              <td className="px-3 py-2">{d.dns_filter ? (d.dns_filter.forced ? "ja" : "nein") : "–"}</td>
              <td className="px-3 py-2 text-slate-500">{fmtDate(d.dns_filter?.at)}</td>
            </tr>
          ))}
        </Table>
      </Card>
      {edit && catalog.data && <ProfileModal profile={edit} catalog={catalog.data} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); void profiles.reload(); void assign.reload(); }} />}
      <ApiKeyModal open={keyOpen} onClose={() => { setKeyOpen(false); void assign.reload(); }} />
    </>
  );
}

function Chip({ on, children, onClick }: { on: boolean; children: string; onClick: () => void }) {
  return <button type="button" onClick={onClick} className={cls("rounded-full border px-3 py-1 text-xs", on ? "border-red-300 bg-red-50 text-red-700" : "border-slate-300 text-slate-600 hover:bg-slate-50")}>{on ? "⛔ " : ""}{children}</button>;
}

function ProfileModal({ profile, catalog, onClose, onSaved }: { profile: Profile; catalog: Catalog; onClose: () => void; onSaved: () => void }) {
  const [p, setP] = useState<Profile>(structuredClone(profile));
  const [deny, setDeny] = useState(p.denylist.join("\n"));
  const [allow, setAllow] = useState(p.allowlist.join("\n"));
  const { busy, error, run } = useAction();
  const toggle = (k: "categories" | "services" | "blocklists", v: string) => setP({ ...p, [k]: p[k].includes(v) ? p[k].filter((x) => x !== v) : [...p[k], v] });
  const lines = (s: string) => s.split(/\s+/).map((x) => x.trim()).filter(Boolean);
  return (
    <Modal open onClose={onClose} title={p.id ? `Profil ${p.name}` : "Neues Filterprofil"} wide>
      <ErrorBox error={error} />
      <div className="space-y-4">
        <Input label="Name" value={p.name} onChange={(e) => setP({ ...p, name: e.target.value })} />
        <div><div className="mb-1 text-sm font-medium">Kategorien blockieren</div><div className="flex flex-wrap gap-2">{Object.entries(catalog.categories).map(([k, l]) => <Chip key={k} on={p.categories.includes(k)} onClick={() => toggle("categories", k)}>{l}</Chip>)}</div></div>
        <div><div className="mb-1 text-sm font-medium">Dienste blockieren</div><div className="flex flex-wrap gap-2">{catalog.services.map((k) => <Chip key={k} on={p.services.includes(k)} onClick={() => toggle("services", k)}>{k}</Chip>)}</div></div>
        <div className="grid gap-4 md:grid-cols-2">
          <div><div className="mb-1 text-sm font-medium">Sicherheit</div><div className="space-y-1">{Object.entries(catalog.security).map(([k, l]) => <Checkbox key={k} label={l} checked={!!p.security[k]} onChange={(v) => setP({ ...p, security: { ...p.security, [k]: v } })} />)}</div></div>
          <div className="space-y-3">
            <div><div className="mb-1 text-sm font-medium">Werbe-/Tracker-Blocklisten</div><div className="space-y-1">{catalog.blocklists.map((b) => <Checkbox key={b} label={b} checked={p.blocklists.includes(b)} onChange={() => toggle("blocklists", b)} />)}</div></div>
            <div className="space-y-1">
              <Checkbox label="SafeSearch erzwingen" checked={p.safe_search} onChange={(v) => setP({ ...p, safe_search: v })} />
              <Checkbox label="YouTube eingeschränkter Modus" checked={p.youtube_restricted} onChange={(v) => setP({ ...p, youtube_restricted: v })} />
              <Checkbox label="Umgehung blockieren (VPN/Proxy/DoH)" checked={p.block_bypass} onChange={(v) => setP({ ...p, block_bypass: v })} />
              <Checkbox label="DNS im LAN erzwingen (Port-53-Redirect)" checked={p.force_dns} onChange={(v) => setP({ ...p, force_dns: v })} />
            </div>
          </div>
        </div>
        <div className="grid gap-4 md:grid-cols-2">
          <Textarea label="Denylist (eine Domain pro Zeile)" rows={4} value={deny} onChange={(e) => setDeny(e.target.value)} />
          <Textarea label="Allowlist" rows={4} value={allow} onChange={(e) => setAllow(e.target.value)} />
        </div>
      </div>
      <div className="mt-4 flex justify-between">
        {p.id ? <Button variant="danger" onClick={() => confirm("Profil löschen (auch bei NextDNS)?") && void run(async () => { await api.del(`/content-filter/profiles/${p.id}`); onSaved(); })}>Löschen</Button> : <span />}
        <Button disabled={busy} onClick={() => void run(async () => {
          const body = { ...p, denylist: lines(deny), allowlist: lines(allow) };
          if (p.id) await api.put(`/content-filter/profiles/${p.id}`, body);
          else await api.post("/content-filter/profiles", body);
          onSaved();
        })}>Speichern & zu NextDNS synchronisieren</Button>
      </div>
    </Modal>
  );
}

function ApiKeyModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [key, setKey] = useState("");
  const { busy, error, run } = useAction();
  return (
    <Modal open={open} onClose={onClose} title="NextDNS-API-Key des Mandanten">
      <ErrorBox error={error} />
      <p className="mb-3 text-sm text-slate-600">Optional: eigener NextDNS-Account des Kunden. Ohne Key wird der globale MSP-Key (<code>NEXTDNS_API_KEY</code>) verwendet. Der Key wird verschlüsselt gespeichert.</p>
      <Input label="API-Key (my.nextdns.io → Account)" type="password" value={key} onChange={(e) => setKey(e.target.value)} />
      <div className="mt-4 flex justify-end"><Button disabled={busy} onClick={() => void run(async () => { await api.put("/content-filter/api-key", { api_key: key }); onClose(); })}>Prüfen & speichern</Button></div>
    </Modal>
  );
}
