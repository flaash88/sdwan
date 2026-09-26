import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import PortalDesigner, { LOGIN_TYPES, type Portal } from "../components/hotspot/PortalDesigner";
import { Button, Card, EmptyState, ErrorBox, IconButton, Input, Loading, Modal, Notice, PageHeader, Pill, Segment, Select, Textarea, ToggleField, useAction, type Tone } from "../components/ui";
import { api, download } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtAgo, fmtFull } from "../lib/format";
import { useDevices } from "../lib/fleet";
import { ifaceLabel, useInterfaces } from "../lib/interfaces";
import { useFetch } from "../lib/useFetch";

interface Instance {
  id: string; name: string; slug: string; device_id: string; device: string; portal_id: string; portal: string; login_type: string | null;
  interface: string; hotspot_address: string | null; dns_name: string | null; walled_garden: string[]; session_timeout_min: number; idle_timeout_min: number;
  rate_limit: string | null; enabled: boolean; status: string; last_error: string | null; applied_at: string | null; undeployed: boolean; router_name: string;
  vouchers: Record<string, number>; registrations: number;
}
interface VProfile { id: string; name: string; slug: string; validity_min: number; data_limit_mb: number | null; rate_limit: string | null; shared_users: number }
interface Voucher { id: string; code: string; status: string; batch_id: string; profile_id: string; pushed: boolean; first_used_at: string | null; uptime_s: number; bytes_total: number }
interface Batch { id: string; count: number; note: string | null; profile_id: string; created_by: string | null; created_at: string }
type Tab = "hotspots" | "portals" | "vouchers" | "guests";

const V_STATUS: Record<string, [string, Tone]> = { new: ["neu", "gray"], active: ["aktiv", "green"], used: ["verbraucht", "neutral"], blocked: ["gesperrt", "red"] };
const mb = (b: number) => b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : `${(b / 1e6).toFixed(1)} MB`;
const dur = (m: number) => m % 1440 === 0 ? `${m / 1440} Tag(e)` : m % 60 === 0 ? `${m / 60} h` : `${m} min`;

/** Gäste-Portal: Hotspots auf Interfaces/VLANs, Portal-Designer, Voucher, Live-Gäste, Registrierungen. */
export default function Hotspot() {
  const [params, setParams] = useSearchParams();
  const tab = (params.get("tab") as Tab) || "hotspots";
  const instances = useFetch<Instance[]>("/hotspot/instances");
  const portals = useFetch<Portal[]>("/hotspot/portals");
  const [sel, setSel] = useState<string | null>(null);
  const current = instances.data?.find((i) => i.id === sel) ?? instances.data?.[0] ?? null;
  return (
    <>
      <PageHeader title="Gäste-Portal" subtitle="Hotspot auf dem MikroTik (/ip hotspot) auf einem Interface oder VLAN – unabhängig vom Hersteller der Access-Points dahinter." />
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <Segment value={tab} onChange={(t) => setParams({ tab: t }, { replace: true })} options={[
          { value: "hotspots", label: "Hotspots", count: instances.data?.length }, { value: "portals", label: "Portale" },
          { value: "vouchers", label: "Voucher" }, { value: "guests", label: "Gäste" }]} />
        {(tab === "vouchers" || tab === "guests") && (instances.data?.length ?? 0) > 0 && (
          <div className="w-64"><Select aria-label="Hotspot" value={current?.id ?? ""} onChange={(e) => setSel(e.target.value)}>
            {instances.data!.map((i) => <option key={i.id} value={i.id}>{i.name} ({i.device})</option>)}
          </Select></div>
        )}
      </div>
      {tab === "hotspots" && <Hotspots instances={instances} portals={portals.data ?? []} />}
      {tab === "portals" && <Portals portals={portals} />}
      {tab === "vouchers" && (current ? <Vouchers inst={current} reload={instances.reload} /> : <Card><EmptyState title="Noch kein Hotspot" /></Card>)}
      {tab === "guests" && (current ? <Guests inst={current} /> : <Card><EmptyState title="Noch kein Hotspot" /></Card>)}
    </>
  );
}

// ----------------------------------------------------------------------------- Hotspots
function Hotspots({ instances, portals }: { instances: ReturnType<typeof useFetch<Instance[]>>; portals: Portal[] }) {
  const { can } = useAuth();
  const [edit, setEdit] = useState<Partial<Instance> | null>(null);
  const { busy, error, run } = useAction();
  return (
    <>
      <div className="mb-3 flex justify-end">{can("admin") && <Button icon="plus" onClick={() => setEdit({ session_timeout_min: 240, idle_timeout_min: 15, walled_garden: [], enabled: true })}>Hotspot</Button>}</div>
      <ErrorBox error={error ?? instances.error} />
      {!instances.data ? <Loading rows={3} /> : instances.data.length === 0 ? <Card><EmptyState title="Noch kein Hotspot" text="Ein Hotspot hängt an einem Interface/VLAN eines Geräts (mit IP-Adresse und DHCP im Gästenetz)." /></Card> : (
        <div className="flex flex-col gap-4">{instances.data.map((i) => (
          <Card key={i.id} title={<span className="flex items-center gap-2">{i.name}{!i.enabled && <Pill tone="gray" icon="pause">deaktiviert</Pill>}
            {i.status === "ok" ? <Pill tone="green">aktiv</Pill> : i.status === "error" ? <Pill tone="red" title={i.last_error ?? ""}>Fehler</Pill> : <Pill tone="gray">nicht ausgerollt</Pill>}</span>}
            subtitle={<><Link to={`/devices/${i.device_id}`} className="hover:underline">{i.device}</Link> · <span className="font-mono">{i.interface}</span> · {i.portal} ({LOGIN_TYPES[i.login_type ?? ""] ?? "?"})</>}
            actions={<div className="flex gap-2">
              {can("technician") && <Button size="sm" icon="upload" disabled={busy} onClick={() => void run(async () => { await api.post(`/hotspot/instances/${i.id}/apply`); await instances.reload(); })}>Ausrollen</Button>}
              {can("admin") && <IconButton icon="edit" label="Bearbeiten" onClick={() => setEdit(i)} />}
              {can("admin") && <IconButton icon="trash" label="Löschen" onClick={() => { if (confirm(`Hotspot „${i.name}“ vom Gerät entfernen und löschen? Voucher werden ungültig.`)) void run(async () => { await api.del(`/hotspot/instances/${i.id}`); await instances.reload(); }); }} />}
            </div>}>
            {i.undeployed && i.status !== "pending" && <div className="mb-3"><Notice tone="orange" icon="alert" title="Änderungen nicht ausgerollt">Hotspot oder Portal wurden seit dem letzten Ausrollen geändert.</Notice></div>}
            {i.last_error && i.status === "error" && <div className="mb-3"><Notice tone="red" icon="alert">{i.last_error}</Notice></div>}
            <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm text-fg2">
              <span>Adresse {i.hotspot_address ?? "vom Interface"}{i.dns_name && ` · ${i.dns_name}`}</span>
              <span>Sitzung {dur(i.session_timeout_min)} · Leerlauf {i.idle_timeout_min} min</span>
              <span>Bandbreite {i.rate_limit ?? "unbegrenzt"}</span>
              {i.walled_garden.length > 0 && <span>Walled Garden: {i.walled_garden.join(", ")}</span>}
              <span>Voucher: {Object.entries(i.vouchers).map(([k, v]) => `${v} ${V_STATUS[k]?.[0] ?? k}`).join(", ") || "keine"}</span>
              {i.login_type === "form" && <span>{i.registrations} Registrierungen</span>}
              {i.applied_at && <span>ausgerollt {fmtAgo(i.applied_at)}</span>}
              <span className="font-mono text-xs">{i.router_name}</span>
            </div>
          </Card>))}
        </div>
      )}
      {edit && <InstanceModal inst={edit} portals={portals} onClose={() => setEdit(null)} onSaved={instances.reload} />}
    </>
  );
}

function InstanceModal({ inst, portals, onClose, onSaved }: { inst: Partial<Instance>; portals: Portal[]; onClose: () => void; onSaved: () => Promise<void> }) {
  const devices = useDevices();
  const [f, setF] = useState<Partial<Instance>>(inst);
  const [wg, setWg] = useState((inst.walled_garden ?? []).join("\n"));
  const ifaces = useInterfaces(f.device_id ?? null);
  const { busy, error, run } = useAction();
  const portal = portals.find((p) => p.id === f.portal_id);
  return (
    <Modal open wide onClose={onClose} title={inst.id ? `Hotspot „${inst.name}“` : "Neuer Hotspot"}
      footer={<><Button variant="secondary" onClick={onClose}>Abbrechen</Button><Button disabled={busy || !f.name || !f.slug || !f.device_id || !f.portal_id || !f.interface} onClick={() => void run(async () => {
        const body = { name: f.name, slug: f.slug, device_id: f.device_id, portal_id: f.portal_id, interface: f.interface, hotspot_address: f.hotspot_address || null,
          dns_name: f.dns_name || null, walled_garden: wg.split(/\s+/).filter(Boolean), session_timeout_min: f.session_timeout_min, idle_timeout_min: f.idle_timeout_min,
          rate_limit: f.rate_limit || null, enabled: f.enabled };
        if (inst.id) await api.put(`/hotspot/instances/${inst.id}`, body); else await api.post("/hotspot/instances", body);
        await onSaved(); onClose();
      })}>Speichern</Button></>}>
      <ErrorBox error={error} />
      <div className="grid gap-3 sm:grid-cols-2">
        <Input label="Name" value={f.name ?? ""} onChange={(e) => setF({ ...f, name: e.target.value })} />
        <Input label="Kürzel (Router-Objekte sdwan-hs-…)" value={f.slug ?? ""} disabled={!!inst.id} maxLength={20} onChange={(e) => setF({ ...f, slug: e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "") })} />
        <Select label="Gerät" value={f.device_id ?? ""} disabled={!!inst.id} onChange={(e) => setF({ ...f, device_id: e.target.value, interface: "" })}>
          <option value="">– wählen –</option>{devices.data?.filter((d) => d.pairing_status === "paired").map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
        </Select>
        <Select label="Interface / VLAN (Gästenetz)" value={f.interface ?? ""} onChange={(e) => setF({ ...f, interface: e.target.value })}>
          <option value="">– wählen –</option>{ifaces.data?.map((i) => <option key={i.name} value={i.name}>{ifaceLabel(i.name, i)}</option>)}
          {f.interface && !ifaces.data?.some((i) => i.name === f.interface) && <option value={f.interface}>{f.interface}</option>}
        </Select>
        <Select label="Portal" value={f.portal_id ?? ""} onChange={(e) => setF({ ...f, portal_id: e.target.value })}>
          <option value="">– wählen –</option>{portals.map((p) => <option key={p.id} value={p.id}>{p.name}{p.builtin ? " (Vorlage)" : ""} – {LOGIN_TYPES[p.login_type]}</option>)}
        </Select>
        <Input label="Hotspot-Adresse (leer = IP des Interfaces)" value={f.hotspot_address ?? ""} onChange={(e) => setF({ ...f, hotspot_address: e.target.value })} />
        <Input label="DNS-Name (optional, z. B. für Voucher-QR)" value={f.dns_name ?? ""} onChange={(e) => setF({ ...f, dns_name: e.target.value })} />
        <Input label="Bandbreite je Gast (Upload/Download, z. B. 5M/20M)" value={f.rate_limit ?? ""} onChange={(e) => setF({ ...f, rate_limit: e.target.value })} />
        <Input label="Sitzungsdauer Klick/Formular (min)" type="number" min={5} value={f.session_timeout_min ?? 240} onChange={(e) => setF({ ...f, session_timeout_min: Number(e.target.value) })} />
        <Input label="Leerlauf-Timeout (min)" type="number" min={1} value={f.idle_timeout_min ?? 15} onChange={(e) => setF({ ...f, idle_timeout_min: Number(e.target.value) })} />
      </div>
      <div className="mt-3"><Textarea label="Walled Garden – ohne Anmeldung erreichbare Hosts (je Zeile, z. B. example.com, *.example.com)" rows={3} value={wg} onChange={(e) => setWg(e.target.value)} /></div>
      <div className="mt-3"><ToggleField label="Aktiviert" checked={f.enabled ?? true} onChange={(v) => setF({ ...f, enabled: v })} /></div>
      {portal?.login_type === "form" && <div className="mt-3"><Notice tone="blue" icon="info">Formular-Anmeldung: Die Plattform-Adresse wird automatisch in den Walled Garden aufgenommen. Bei HTTPS kann der Walled Garden nur nach Host freigeben – Gäste erreichen damit auch die (anmeldegeschützte) Plattform-Oberfläche.</Notice></div>}
    </Modal>
  );
}

// ----------------------------------------------------------------------------- Portale
function Portals({ portals }: { portals: ReturnType<typeof useFetch<Portal[]>> }) {
  const { can } = useAuth();
  const [edit, setEdit] = useState<Portal | null>(null);
  const { error, run } = useAction();
  return (
    <>
      <ErrorBox error={error ?? portals.error} />
      {!portals.data ? <Loading rows={3} /> : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">{portals.data.map((p) => (
          <Card key={p.id} title={<span className="flex items-center gap-2">{p.name}{p.builtin ? <Pill tone="gray">Vorlage</Pill> : p.scope === "global" ? <Pill tone="blue">global</Pill> : null}</span>}
            subtitle={LOGIN_TYPES[p.login_type]}
            actions={<div className="flex gap-1">
              {can("admin") && <IconButton icon="copy" label="Kopieren" onClick={() => void run(async () => { const c = await api.post<Portal>(`/hotspot/portals/${p.id}/copy`); await portals.reload(); setEdit(c); })} />}
              {can("admin") && !p.builtin && <IconButton icon="edit" label="Bearbeiten" onClick={() => setEdit(p)} />}
              {can("admin") && !p.builtin && <IconButton icon="trash" label="Löschen" onClick={() => { if (confirm(`Portal „${p.name}“ löschen?`)) void run(async () => { await api.del(`/hotspot/portals/${p.id}`); await portals.reload(); }); }} />}
            </div>}>
            <div className="flex items-center gap-3">
              <div className="flex h-20 w-28 shrink-0 flex-col items-center justify-center rounded border border-line px-1 text-center text-[9px] leading-tight" style={{ background: p.design.background, color: p.design.text }}>
                {p.design.logo ? <img src={p.design.logo} alt="" className="max-h-6 max-w-16" /> : <span className="font-semibold">{p.texts.de?.title}</span>}
                <span className="mt-1 rounded px-2 py-0.5 text-white" style={{ background: p.design.primary }}>{p.texts.de?.button}</span>
              </div>
              <p className="text-sm text-fg2">{p.description ?? p.texts.de?.welcome}{p.form_fields.length > 0 && <span className="block text-xs text-fg3">Felder: {p.form_fields.map((f) => f.label_de).join(", ")}</span>}</p>
            </div>
          </Card>))}
        </div>
      )}
      {edit && <PortalDesigner portal={edit} onClose={() => setEdit(null)} onSaved={portals.reload} />}
    </>
  );
}

// ----------------------------------------------------------------------------- Voucher
function Vouchers({ inst, reload }: { inst: Instance; reload: () => Promise<void> }) {
  const { can } = useAuth();
  const profiles = useFetch<VProfile[]>("/hotspot/voucher-profiles");
  const data = useFetch<{ vouchers: Voucher[]; batches: Batch[] }>(`/hotspot/instances/${inst.id}/vouchers`);
  const [prof, setProf] = useState<Partial<VProfile> | null>(null);
  const [batch, setBatch] = useState<{ profile_id: string; count: number; note: string } | null>(null);
  const [filter, setFilter] = useState("");
  const { busy, error, run } = useAction();
  const pname = (id: string) => profiles.data?.find((p) => p.id === id)?.name ?? "?";
  const vs = (data.data?.vouchers ?? []).filter((v) => !filter || v.batch_id === filter);
  return (
    <div className="flex flex-col gap-4">
      <ErrorBox error={error ?? data.error} />
      {inst.login_type !== "voucher" && <Notice tone="orange" icon="info">Das Portal dieses Hotspots nutzt keine Voucher-Anmeldung – Voucher funktionieren trotzdem über die Login-URL mit Code.</Notice>}
      <Card title="Voucher-Profile" actions={can("admin") && <Button size="sm" variant="secondary" icon="plus" onClick={() => setProf({ validity_min: 1440, shared_users: 1 })}>Profil</Button>}>
        {!profiles.data?.length ? <p className="text-sm text-fg3">Ein Profil legt Gültigkeit (Online-Zeit), Datenvolumen, Bandbreite und Geräte je Voucher fest.</p> : (
          <div className="flex flex-wrap gap-2">{profiles.data.map((p) => (
            <button key={p.id} type="button" disabled={!can("admin")} onClick={() => setProf(p)} className="rounded-md border border-line px-3 py-2 text-left text-sm hover:bg-panel2">
              <div className="font-medium">{p.name}</div><div className="text-xs text-fg3">{dur(p.validity_min)} · {p.data_limit_mb ? `${p.data_limit_mb} MB` : "ohne Datenlimit"} · {p.rate_limit ?? "Bandbreite wie Hotspot"} · {p.shared_users} Gerät(e)</div>
            </button>))}</div>
        )}
      </Card>
      <Card flush title="Stapel" actions={can("technician") && <Button size="sm" icon="plus" disabled={!profiles.data?.length} onClick={() => setBatch({ profile_id: profiles.data![0].id, count: 20, note: "" })}>Voucher erzeugen</Button>}>
        {!data.data ? <Loading rows={2} /> : data.data.batches.length === 0 ? <EmptyState compact title="Noch keine Voucher" /> : (
          <table className="w-full text-sm"><tbody>{data.data.batches.map((b) => (
            <tr key={b.id} className="border-b border-line last:border-b-0">
              <td className="px-4 py-2">{fmtFull(b.created_at)}</td><td className="px-2">{b.count} × {pname(b.profile_id)}</td><td className="px-2 text-fg2">{b.note}</td><td className="px-2 text-xs text-fg3">{b.created_by}</td>
              <td className="px-2 py-1.5 text-right"><div className="flex justify-end gap-2">
                <Button size="sm" variant="ghost" onClick={() => setFilter(filter === b.id ? "" : b.id)}>{filter === b.id ? "alle zeigen" : "anzeigen"}</Button>
                {can("technician") && <Link to={`/print/vouchers/${b.id}`} target="_blank"><Button size="sm" variant="secondary" icon="file">Drucken (A4)</Button></Link>}
                {can("technician") && <Button size="sm" variant="secondary" icon="download" onClick={() => void download(`/hotspot/batches/${b.id}/csv`, `voucher-${inst.slug}.csv`)}>CSV</Button>}
              </div></td>
            </tr>))}</tbody></table>
        )}
      </Card>
      <Card flush title="Voucher" subtitle={`${vs.length} angezeigt · Status wird alle 5 Minuten vom Router übernommen`}>
        {vs.length === 0 ? <EmptyState compact title="Keine Voucher" /> : (
          <div className="max-h-[50vh] overflow-y-auto"><table className="w-full text-sm">
            <thead><tr className="border-b border-line bg-panel2 text-left text-xs text-fg3"><th className="px-4 py-2 font-medium">Code</th><th className="px-2 font-medium">Profil</th><th className="px-2 font-medium">Status</th><th className="px-2 font-medium">Online</th><th className="px-2 font-medium">Volumen</th><th className="px-2 font-medium">Erstmals</th><th /></tr></thead>
            <tbody>{vs.map((v) => { const [l, t] = V_STATUS[v.status] ?? [v.status, "gray"]; return (
              <tr key={v.id} className="border-b border-line last:border-b-0">
                <td className="px-4 py-1.5 font-mono">{v.code}{!v.pushed && <Pill tone="orange" title="Noch nicht auf dem Router">ausstehend</Pill>}</td><td className="px-2">{pname(v.profile_id)}</td>
                <td className="px-2"><Pill tone={t}>{l}</Pill></td><td className="px-2 text-xs">{v.uptime_s ? dur(Math.round(v.uptime_s / 60)) : "–"}</td><td className="px-2 text-xs">{v.bytes_total ? mb(v.bytes_total) : "–"}</td>
                <td className="px-2 text-xs">{v.first_used_at ? fmtFull(v.first_used_at) : "–"}</td>
                <td className="px-2 text-right">{can("technician") && v.status !== "blocked" && <Button size="sm" variant="ghost" disabled={busy} onClick={() => void run(async () => { await api.post(`/hotspot/vouchers/${v.id}/block`); await data.reload(); })}>Sperren</Button>}</td>
              </tr>); })}</tbody>
          </table></div>
        )}
      </Card>
      {prof && (
        <Modal open onClose={() => setProf(null)} title={prof.id ? `Voucher-Profil „${prof.name}“` : "Neues Voucher-Profil"}
          footer={<><Button variant="secondary" onClick={() => setProf(null)}>Abbrechen</Button><Button disabled={busy || !prof.name || !prof.slug} onClick={() => void run(async () => {
            const body = { name: prof.name, slug: prof.slug, validity_min: prof.validity_min, data_limit_mb: prof.data_limit_mb || null, rate_limit: prof.rate_limit || null, shared_users: prof.shared_users };
            if (prof.id) await api.put(`/hotspot/voucher-profiles/${prof.id}`, body); else await api.post("/hotspot/voucher-profiles", body);
            setProf(null); await profiles.reload();
          })}>Speichern</Button></>}>
          <div className="grid gap-3 sm:grid-cols-2">
            <Input label="Name" value={prof.name ?? ""} onChange={(e) => setProf({ ...prof, name: e.target.value })} />
            <Input label="Kürzel" value={prof.slug ?? ""} disabled={!!prof.id} maxLength={20} onChange={(e) => setProf({ ...prof, slug: e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "") })} />
            <Input label="Online-Zeit (Minuten)" type="number" min={5} value={prof.validity_min ?? 1440} onChange={(e) => setProf({ ...prof, validity_min: Number(e.target.value) })} />
            <Input label="Datenlimit (MB, leer = ohne)" type="number" min={1} value={prof.data_limit_mb ?? ""} onChange={(e) => setProf({ ...prof, data_limit_mb: e.target.value ? Number(e.target.value) : null })} />
            <Input label="Bandbreite (Upload/Download, leer = wie Hotspot)" value={prof.rate_limit ?? ""} onChange={(e) => setProf({ ...prof, rate_limit: e.target.value })} />
            <Input label="Geräte je Voucher" type="number" min={1} max={20} value={prof.shared_users ?? 1} onChange={(e) => setProf({ ...prof, shared_users: Number(e.target.value) })} />
          </div>
        </Modal>
      )}
      {batch && (
        <Modal open onClose={() => setBatch(null)} title="Voucher erzeugen"
          footer={<><Button variant="secondary" onClick={() => setBatch(null)}>Abbrechen</Button><Button disabled={busy} onClick={() => void run(async () => {
            const r = await api.post<{ pushed: boolean; error: string | null }>(`/hotspot/instances/${inst.id}/batches`, { ...batch, note: batch.note || null });
            setBatch(null); await data.reload(); await reload();
            if (!r.pushed) alert(`Voucher angelegt, aber noch nicht auf dem Router (${r.error}). Sie werden automatisch übertragen.`);
          })}>Erzeugen</Button></>}>
          <div className="grid gap-3">
            <Select label="Profil" value={batch.profile_id} onChange={(e) => setBatch({ ...batch, profile_id: e.target.value })}>{profiles.data?.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</Select>
            <Input label="Anzahl (1–500)" type="number" min={1} max={500} value={batch.count} onChange={(e) => setBatch({ ...batch, count: Number(e.target.value) })} />
            <Input label="Notiz (optional)" value={batch.note} onChange={(e) => setBatch({ ...batch, note: e.target.value })} />
          </div>
        </Modal>
      )}
    </div>
  );
}

// ----------------------------------------------------------------------------- Gäste
interface Active { id: string; user: string; address: string; mac: string; uptime: string; bytes_in: number; bytes_out: number; trial: boolean }

function Guests({ inst }: { inst: Instance }) {
  const { can } = useAuth();
  const live = useFetch<{ active: Active[]; blocked: { id: string; mac: string }[] }>(`/hotspot/instances/${inst.id}/active`);
  const regs = useFetch<{ retention_days: number; registrations: { id: string; data: Record<string, unknown>; terms_accepted: boolean; lang: string | null; created_at: string }[] }>(
    can("admin") && inst.login_type === "form" ? `/hotspot/instances/${inst.id}/registrations` : null);
  const settings = useFetch<{ retention_days: number }>("/hotspot/settings");
  const { busy, error, run } = useAction();
  const act = (path: string, g: Active) => run(async () => { await api.post(`/hotspot/instances/${inst.id}/${path}`, { active_id: g.id, user: g.user, mac: g.mac }); await live.reload(); });
  return (
    <div className="flex flex-col gap-4">
      <ErrorBox error={error ?? live.error} />
      <Card flush title="Aktive Gäste" subtitle="live vom Router, wird nicht gespeichert" actions={<Button size="sm" variant="ghost" icon="rotate" onClick={() => void live.reload()}>Aktualisieren</Button>}>
        {!live.data ? <Loading rows={2} /> : live.data.active.length === 0 ? <EmptyState compact title="Keine Gäste online" /> : (
          <table className="w-full text-sm">
            <thead><tr className="border-b border-line bg-panel2 text-left text-xs text-fg3"><th className="px-4 py-2 font-medium">Anmeldung</th><th className="px-2 font-medium">IP</th><th className="px-2 font-medium">MAC</th><th className="px-2 font-medium">Online</th><th className="px-2 font-medium">Volumen ↓/↑</th><th /></tr></thead>
            <tbody>{live.data.active.map((g) => (
              <tr key={g.id} className="border-b border-line last:border-b-0">
                <td className="px-4 py-1.5">{g.trial ? <Pill tone="gray">{inst.login_type === "form" ? "Formular" : "Klick"}</Pill> : <span className="font-mono">{g.user}</span>}</td>
                <td className="px-2 font-mono text-xs">{g.address}</td><td className="px-2 font-mono text-xs">{g.mac}</td><td className="px-2 text-xs">{g.uptime}</td>
                <td className="px-2 text-xs">{mb(g.bytes_out)} / {mb(g.bytes_in)}</td>
                <td className="px-2 text-right">{can("technician") && <div className="flex justify-end gap-1">
                  <Button size="sm" variant="ghost" disabled={busy} onClick={() => void act("disconnect", g)}>Trennen</Button>
                  <Button size="sm" variant="danger-outline" disabled={busy} onClick={() => { if (confirm(g.trial ? `Gerät ${g.mac} sperren?` : `Voucher ${g.user} sperren?`)) void act("block", g); }}>Sperren</Button>
                </div>}</td>
              </tr>))}</tbody>
          </table>
        )}
      </Card>
      {(live.data?.blocked.length ?? 0) > 0 && (
        <Card flush title="Gesperrte Geräte">
          <table className="w-full text-sm"><tbody>{live.data!.blocked.map((b) => (
            <tr key={b.id} className="border-b border-line last:border-b-0"><td className="px-4 py-2 font-mono text-xs">{b.mac}</td>
              <td className="px-2 text-right">{can("technician") && <Button size="sm" variant="ghost" onClick={() => void run(async () => { await api.del(`/hotspot/instances/${inst.id}/blocked/${b.id}`); await live.reload(); })}>Entsperren</Button>}</td></tr>))}</tbody></table>
        </Card>
      )}
      {inst.login_type === "form" && can("admin") && (
        <Card flush title="Registrierungen" subtitle={`nur die Formularfelder · automatisch gelöscht nach ${regs.data?.retention_days ?? settings.data?.retention_days ?? 30} Tagen`}
          actions={<RetentionEdit days={settings.data?.retention_days ?? 30} onSaved={async () => { await settings.reload(); await regs.reload(); }} />}>
          {!regs.data ? <Loading rows={2} /> : regs.data.registrations.length === 0 ? <EmptyState compact title="Keine Registrierungen" /> : (
            <table className="w-full text-sm"><tbody>{regs.data.registrations.map((r) => (
              <tr key={r.id} className="border-b border-line last:border-b-0"><td className="px-4 py-2 text-xs">{fmtFull(r.created_at)}</td>
                <td className="px-2">{Object.entries(r.data).map(([k, v]) => `${k}: ${typeof v === "boolean" ? (v ? "ja" : "nein") : v || "–"}`).join(" · ")}</td>
                <td className="px-2 text-xs text-fg3">{r.lang?.toUpperCase()}</td></tr>))}</tbody></table>
          )}
        </Card>
      )}
      <Notice tone="blue" icon="info" title="Datenschutz">Gespeichert werden nur die Felder des Portal-Formulars (keine Browser- oder Gerätedaten); aktive Sitzungen werden nur live angezeigt. Nutzungsbedingungen und Datenschutzhinweis verantwortet der Betreiber.</Notice>
    </div>
  );
}

function RetentionEdit({ days, onSaved }: { days: number; onSaved: () => Promise<void> }) {
  const [v, setV] = useState<number | null>(null);
  const { busy, run } = useAction();
  if (v === null) return <Button size="sm" variant="ghost" icon="settings" onClick={() => setV(days)}>Aufbewahrung</Button>;
  return (
    <div className="flex items-center gap-2">
      <div className="w-24"><Input aria-label="Tage" type="number" min={1} value={v} onChange={(e) => setV(Number(e.target.value))} /></div>
      <Button size="sm" disabled={busy} onClick={() => void run(async () => { await api.put("/hotspot/retention", { days: v }); setV(null); await onSaved(); })}>Speichern</Button>
    </div>
  );
}
