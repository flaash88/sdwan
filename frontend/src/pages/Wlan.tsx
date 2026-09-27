import { useState } from "react";
import { Link } from "react-router-dom";
import TargetsModal, { type Targets } from "../components/TargetsModal";
import { Button, Card, EmptyState, ErrorBox, IconButton, Input, Loading, Modal, Notice, PageHeader, Pill, Select, Textarea, ToggleField, useAction, type Tone } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtAgo } from "../lib/format";
import { useDevices, useSites } from "../lib/fleet";
import { useFetch } from "../lib/useFetch";

interface DevState { device_id: string; device: string; mode: string; status: string; error: string | null; applied_version: number | null; current: boolean; detail: { interfaces?: string[]; radios_disabled?: string[] } }
interface Assignment extends Targets { id: string; mode: "local" | "capsman" }
export interface WlanProfile {
  id: string; name: string; slug: string; description: string | null; ssid: string; security: string; has_passphrase: boolean;
  radius_server: string | null; radius_port: number; has_radius_secret: boolean; band: string; channel_width: string; country_code: string | null;
  vlan_id: number | null; bridge: string; client_isolation: boolean; hidden: boolean; schedule: { start: string; end: string } | null;
  is_guest: boolean; psk_rotate_days: number | null; psk_rotated_at: string | null; enabled: boolean; version: number; router_name: string;
  assignments: Assignment[]; devices: DevState[]; pending_removal: { device_id: string; device: string }[]; undeployed: string[];
}
interface Form extends Omit<WlanProfile, "id" | "has_passphrase" | "has_radius_secret" | "psk_rotated_at" | "version" | "router_name" | "assignments" | "devices" | "pending_removal" | "undeployed"> {
  id?: string; passphrase: string; radius_secret: string; use_schedule: boolean;
}

const SEC: Record<string, string> = { "wpa2-psk": "WPA2-PSK", "wpa2-wpa3-psk": "WPA2/WPA3-PSK", "wpa3-psk": "WPA3-PSK", "wpa2-eap": "WPA2-Enterprise (RADIUS)", "wpa3-eap": "WPA3-Enterprise (RADIUS)" };
const BAND: Record<string, string> = { both: "2,4 + 5 GHz", "2ghz": "2,4 GHz", "5ghz": "5 GHz" };
export const WLAN_STATUS: Record<string, [string, Tone]> = {
  ok: ["ausgerollt", "green"], pending: ["ausstehend", "gray"], error: ["Fehler", "red"], offline: ["offline", "gray"],
  unsupported_driver: ["Nicht unterstützt: alter wireless-Treiber (nur Anzeige)", "gray"], no_wlan: ["kein WLAN", "orange"], blocked: ["Sicherheitsmeldung", "red"],
};
const EMPTY: Form = { name: "", slug: "", description: null, ssid: "", security: "wpa2-wpa3-psk", passphrase: "", radius_server: null, radius_port: 1812, radius_secret: "",
  band: "both", channel_width: "auto", country_code: null, vlan_id: null, bridge: "bridge", client_isolation: false, hidden: false, schedule: null, use_schedule: false,
  is_guest: false, psk_rotate_days: null, enabled: true };

function toForm(p: WlanProfile): Form {
  return { ...p, passphrase: "", radius_secret: "", use_schedule: !!p.schedule };
}

/** WLAN-Profile: SSID, Sicherheit, Funk, VLAN – Zuweisung an Geräte/Standorte/Tags, lokal oder über CAPsMAN. */
export default function Wlan() {
  const { can } = useAuth();
  const list = useFetch<WlanProfile[]>("/wlan/profiles");
  const countries = useFetch<{ countries: Record<string, string>; tenant_default: string }>("/wlan/countries");
  const devices = useDevices();
  const sites = useSites();
  const [edit, setEdit] = useState<Form | null>(null);
  const [assign, setAssign] = useState<{ p: WlanProfile; mode: "local" | "capsman" } | null>(null);
  const [rotated, setRotated] = useState<{ p: WlanProfile; psk: string } | null>(null);
  const { busy, error, run } = useAction();
  const devName = (id: string) => devices.data?.find((d) => d.id === id)?.name ?? "?";
  const siteName = (id: string) => sites.data?.find((s) => s.id === id)?.name ?? "?";
  const describe = (a: Assignment) => [...a.device_ids.map(devName), ...a.site_ids.map((s) => `Standort ${siteName(s)}`), ...a.tags.map((t) => `#${t}`)].join(", ");
  const save = () => edit && run(async () => {
    const { use_schedule, id, ...rest } = edit;
    const body = { ...rest, passphrase: edit.passphrase || null, radius_secret: edit.radius_secret || null, schedule: use_schedule ? (edit.schedule ?? { start: "07:00", end: "22:00" }) : null,
      country_code: edit.country_code || null, vlan_id: edit.vlan_id || null, psk_rotate_days: edit.is_guest ? edit.psk_rotate_days || null : null };
    if (id) await api.put(`/wlan/profiles/${id}`, body); else await api.post("/wlan/profiles", body);
    setEdit(null); await list.reload();
  });
  const setAssignments = (p: WlanProfile, add: Assignment | null, removeId?: string) => run(async () => {
    const keep = p.assignments.filter((a) => a.id !== removeId).map(({ id: _id, ...a }) => a);
    await api.put(`/wlan/profiles/${p.id}/assignments`, add ? [...keep, add] : keep);
    await list.reload();
  });
  const psk = edit?.security.endsWith("-psk");
  return (
    <>
      <PageHeader title="WLAN" subtitle="Profile für den wifi-Treiber (RouterOS 7). Die Plattform legt virtuelle APs an – vorhandene WLANs und Radios bleiben unverändert."
        actions={can("admin") && <Button icon="plus" onClick={() => setEdit({ ...EMPTY })}>WLAN-Profil</Button>} />
      <ErrorBox error={error ?? list.error} />
      {!list.data ? <Loading rows={3} /> : list.data.length === 0 ? <Card><EmptyState title="Noch keine WLAN-Profile" text="Ein Profil beschreibt ein WLAN (SSID, Sicherheit, VLAN). Danach Geräten, Standorten oder Tags zuweisen und ausrollen." /></Card> : (
        <div className="flex flex-col gap-4">
          {list.data.map((p) => (
            <Card key={p.id} title={<span className="flex items-center gap-2">{p.name}{p.is_guest && <Pill tone="blue">Gäste</Pill>}{!p.enabled && <Pill tone="gray" icon="pause">deaktiviert</Pill>}</span>}
              subtitle={<span className="font-mono">{p.ssid}</span>}
              actions={<div className="flex flex-wrap gap-2">
                {p.security.endsWith("-psk") && can("technician") && <>
                  <Link to={`/print/wlan/${p.id}`} target="_blank"><Button size="sm" variant="secondary" icon="file">Aushang / QR</Button></Link>
                  {p.is_guest && <Button size="sm" variant="secondary" icon="rotate" disabled={busy} onClick={() => { if (confirm(`Neues PSK für „${p.ssid}“ erzeugen und ausrollen? Verbundene Gäste müssen sich neu anmelden.`)) void run(async () => { const r = await api.post<{ passphrase: string }>(`/wlan/profiles/${p.id}/rotate-psk`); setRotated({ p, psk: r.passphrase }); await list.reload(); }); }}>PSK rotieren</Button>}
                </>}
                {can("technician") && <Button size="sm" icon="upload" disabled={busy || (!p.devices.length && !p.pending_removal.length)} onClick={() => void run(async () => { await api.post(`/wlan/profiles/${p.id}/apply`); setTimeout(() => void list.reload(), 1500); })}>Ausrollen</Button>}
                {can("admin") && <IconButton icon="edit" label="Bearbeiten" onClick={() => setEdit(toForm(p))} />}
                {can("admin") && <IconButton icon="trash" label="Löschen" onClick={() => { if (confirm(`WLAN-Profil „${p.name}“ löschen? Es wird von allen Geräten entfernt.`)) void run(async () => { await api.del(`/wlan/profiles/${p.id}`); await list.reload(); }); }} />}
              </div>}>
              {p.undeployed.length > 0 && <div className="mb-3"><Notice tone="orange" icon="alert" title="Änderungen nicht ausgerollt">Betroffen: {p.undeployed.join(", ")}</Notice></div>}
              <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm text-fg2">
                <span>{SEC[p.security]}</span><span>{BAND[p.band]}{p.channel_width !== "auto" && ` · ${p.channel_width} MHz`}</span>
                <span>Land {p.country_code ?? `${countries.data?.tenant_default ?? "AT"} (Mandant)`}</span>
                <span>{p.vlan_id ? `VLAN ${p.vlan_id}` : "ohne VLAN"} · Bridge {p.bridge}</span>
                {p.client_isolation && <span>Client-Isolation</span>}{p.hidden && <span>versteckt</span>}
                {p.schedule && <span>aktiv {p.schedule.start}–{p.schedule.end}</span>}
                {p.is_guest && p.psk_rotate_days && <span>PSK-Rotation alle {p.psk_rotate_days} Tage</span>}
                {p.psk_rotated_at && <span>PSK erneuert {fmtAgo(p.psk_rotated_at)}</span>}
                <span className="font-mono text-xs">{p.router_name}</span>
              </div>
              <div className="mt-4 flex flex-col gap-2">
                <div className="text-xs font-medium text-fg3">Zuweisungen</div>
                {p.assignments.length === 0 && <div className="text-sm text-fg3">Noch keinem Gerät zugewiesen.</div>}
                {p.assignments.map((a) => (
                  <div key={a.id} className="flex items-center gap-2 text-sm">
                    <Pill tone={a.mode === "capsman" ? "blue" : "gray"}>{a.mode === "capsman" ? "CAPsMAN-Controller" : "lokal"}</Pill><span>{describe(a) || "–"}</span>
                    {can("technician") && <IconButton icon="x" label="Zuweisung entfernen" onClick={() => void setAssignments(p, null, a.id)} />}
                  </div>
                ))}
                {can("technician") && <div className="flex gap-2">
                  <Button size="sm" variant="ghost" icon="plus" onClick={() => setAssign({ p, mode: "local" })}>Lokal zuweisen</Button>
                  <Button size="sm" variant="ghost" icon="plus" onClick={() => setAssign({ p, mode: "capsman" })}>An CAPsMAN-Controller</Button>
                </div>}
              </div>
              {(p.devices.length > 0 || p.pending_removal.length > 0) && (
                <div className="mt-4 overflow-x-auto"><table className="w-full text-sm">
                  <thead><tr className="border-y border-line text-left text-xs text-fg3"><th className="py-1.5 pr-2 font-medium">Gerät</th><th className="px-2 font-medium">Modus</th><th className="px-2 font-medium">Status</th><th className="px-2 font-medium">Version</th><th className="px-2 font-medium">Interfaces / Hinweis</th></tr></thead>
                  <tbody>
                    {p.devices.map((d) => { const [l, t] = WLAN_STATUS[d.status] ?? [d.status, "gray"]; return (
                      <tr key={d.device_id} className="border-b border-line last:border-b-0">
                        <td className="py-1.5 pr-2"><Link to={`/devices/${d.device_id}?tab=wlan`} className="hover:underline">{d.device}</Link></td>
                        <td className="px-2">{d.mode === "capsman" ? "CAPsMAN" : "lokal"}</td>
                        <td className="px-2"><Pill tone={t} title={d.error ?? ""}>{l}</Pill></td>
                        <td className="px-2 text-xs">{d.applied_version ? `v${d.applied_version}` : "–"}{!d.current && d.status === "ok" && " (veraltet)"}</td>
                        <td className="px-2 text-xs text-fg2">{d.error ?? [...(d.detail.interfaces ?? []), ...((d.detail.radios_disabled ?? []).length ? [`Radio deaktiviert: ${d.detail.radios_disabled!.join(", ")}`] : [])].join(", ")}</td>
                      </tr>); })}
                    {p.pending_removal.map((d) => <tr key={d.device_id} className="border-b border-line"><td className="py-1.5 pr-2">{d.device}</td><td /><td className="px-2"><Pill tone="orange">wird entfernt</Pill></td><td /><td className="px-2 text-xs text-fg2">beim nächsten Ausrollen</td></tr>)}
                  </tbody>
                </table></div>
              )}
            </Card>
          ))}
        </div>
      )}
      {assign && <TargetsModal title={assign.mode === "capsman" ? `„${assign.p.name}“ an CAPsMAN-Controller` : `„${assign.p.name}“ lokal zuweisen`}
        onClose={() => setAssign(null)} onSubmit={async (t) => { await setAssignments(assign.p, { id: "", mode: assign.mode, ...t }); }} />}
      {rotated && (
        <Modal open onClose={() => setRotated(null)} title="Neues Gäste-PSK" footer={<><Link to={`/print/wlan/${rotated.p.id}`} target="_blank"><Button variant="secondary" icon="file">Aushang drucken</Button></Link><Button onClick={() => setRotated(null)}>Schließen</Button></>}>
          <p>SSID <b className="font-mono">{rotated.p.ssid}</b> – neues PSK:</p>
          <p className="mt-2 rounded-md bg-panel2 p-3 text-center font-mono text-xl">{rotated.psk}</p>
          <p className="mt-2 text-sm text-fg3">Wird auf alle zugewiesenen Geräte ausgerollt.</p>
        </Modal>
      )}
      {edit && (
        <Modal open wide onClose={() => setEdit(null)} title={edit.id ? `WLAN-Profil „${edit.name}“` : "Neues WLAN-Profil"}
          footer={<><Button variant="secondary" onClick={() => setEdit(null)}>Abbrechen</Button><Button disabled={busy || !edit.name || !edit.ssid || !edit.slug} onClick={() => void save()}>Speichern</Button></>}>
          <ErrorBox error={error} />
          <div className="grid gap-3 sm:grid-cols-2">
            <Input label="Name" value={edit.name} onChange={(e) => setEdit({ ...edit, name: e.target.value })} />
            <Input label="Kürzel (Router-Objekte sdwan-wifi-…)" value={edit.slug} disabled={!!edit.id} maxLength={20} onChange={(e) => setEdit({ ...edit, slug: e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "") })} />
            <Input label="SSID" value={edit.ssid} maxLength={32} onChange={(e) => setEdit({ ...edit, ssid: e.target.value })} />
            <Select label="Sicherheit" value={edit.security} onChange={(e) => setEdit({ ...edit, security: e.target.value })}>
              {Object.entries(SEC).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </Select>
            {psk ? (
              <Input label={edit.id ? "PSK (leer = unverändert)" : edit.is_guest ? "PSK (leer = automatisch erzeugen)" : "PSK (8–63 Zeichen)"} type="password" autoComplete="new-password" value={edit.passphrase} onChange={(e) => setEdit({ ...edit, passphrase: e.target.value })} />
            ) : <>
              <Input label="RADIUS-Server (IP)" value={edit.radius_server ?? ""} onChange={(e) => setEdit({ ...edit, radius_server: e.target.value || null })} />
              <Input label="RADIUS-Port" type="number" value={edit.radius_port} onChange={(e) => setEdit({ ...edit, radius_port: Number(e.target.value) })} />
              <Input label={edit.id ? "RADIUS-Secret (leer = unverändert)" : "RADIUS-Secret"} type="password" autoComplete="new-password" value={edit.radius_secret} onChange={(e) => setEdit({ ...edit, radius_secret: e.target.value })} />
            </>}
            <Select label="Band" value={edit.band} onChange={(e) => setEdit({ ...edit, band: e.target.value })}>{Object.entries(BAND).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</Select>
            <Select label="Kanalbreite" value={edit.channel_width} onChange={(e) => setEdit({ ...edit, channel_width: e.target.value })}>
              <option value="auto">automatisch</option>{["20", "40", "80", "160"].map((w) => <option key={w} value={w}>bis {w} MHz</option>)}
            </Select>
            <Select label="Land (Funkvorschriften)" value={edit.country_code ?? ""} onChange={(e) => setEdit({ ...edit, country_code: e.target.value || null })}>
              <option value="">wie Mandant ({countries.data?.tenant_default ?? "AT"})</option>
              {Object.entries(countries.data?.countries ?? {}).map(([k, v]) => <option key={k} value={k}>{k} – {v}</option>)}
            </Select>
            <Input label="VLAN-ID (leer = ohne)" type="number" min={1} max={4094} value={edit.vlan_id ?? ""} onChange={(e) => setEdit({ ...edit, vlan_id: e.target.value ? Number(e.target.value) : null })} />
            <Input label="Bridge" value={edit.bridge} onChange={(e) => setEdit({ ...edit, bridge: e.target.value })} />
          </div>
          <div className="mt-3 grid gap-2 sm:grid-cols-2">
            <ToggleField label="Client-Isolation (Clients sehen sich nicht)" checked={edit.client_isolation} onChange={(v) => setEdit({ ...edit, client_isolation: v })} />
            <ToggleField label="SSID verstecken" checked={edit.hidden} onChange={(v) => setEdit({ ...edit, hidden: v })} />
            <ToggleField label="Gäste-WLAN (PSK rotierbar, QR-Aushang)" checked={edit.is_guest} onChange={(v) => setEdit({ ...edit, is_guest: v })} />
            <ToggleField label="Aktiviert" checked={edit.enabled} onChange={(v) => setEdit({ ...edit, enabled: v })} />
            <ToggleField label="Zeitplan (täglich)" checked={edit.use_schedule} onChange={(v) => setEdit({ ...edit, use_schedule: v, schedule: v ? edit.schedule ?? { start: "07:00", end: "22:00" } : null })} />
          </div>
          {edit.use_schedule && edit.schedule && <div className="mt-2 grid grid-cols-2 gap-3 sm:w-1/2">
            <Input label="an ab" type="time" value={edit.schedule.start} onChange={(e) => setEdit({ ...edit, schedule: { ...edit.schedule!, start: e.target.value } })} />
            <Input label="aus ab" type="time" value={edit.schedule.end} onChange={(e) => setEdit({ ...edit, schedule: { ...edit.schedule!, end: e.target.value } })} />
          </div>}
          {edit.is_guest && psk && <div className="mt-2 sm:w-1/2"><Input label="PSK automatisch rotieren alle … Tage (leer = nur manuell)" type="number" min={1} value={edit.psk_rotate_days ?? ""} onChange={(e) => setEdit({ ...edit, psk_rotate_days: e.target.value ? Number(e.target.value) : null })} /></div>}
          <div className="mt-3"><Textarea label="Beschreibung" rows={2} value={edit.description ?? ""} onChange={(e) => setEdit({ ...edit, description: e.target.value || null })} /></div>
          {edit.is_guest && !edit.client_isolation && <div className="mt-3"><Notice tone="blue" icon="info">Für Gäste-WLANs empfohlen: Client-Isolation und ein eigenes VLAN; im Firewall-Editor den Baustein „Gäste vom LAN isolieren“ verwenden.</Notice></div>}
        </Modal>
      )}
    </>
  );
}
