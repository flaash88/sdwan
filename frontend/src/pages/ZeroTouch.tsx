import { useState } from "react";
import { Link } from "react-router-dom";
import { Badge, Button, Card, Checkbox, ErrorBox, Input, Modal, PageHeader, Select, StatusBadge, Table, Textarea, useAction } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtDate } from "../lib/format";
import { useLive } from "../lib/live";
import type { Device, Site } from "../lib/types";
import { useFetch } from "../lib/useFetch";

interface TemplateContent {
  identity_pattern: string; timezone: string; ntp_servers: string[]; dns_servers: string[]; wan_interface: string;
  lan: { enabled: boolean; bridge_ports: string[]; cidr: string | null; dhcp: boolean };
  wan?: { mode: string; links: Record<string, unknown>[] };
  vrrp?: Record<string, unknown>[];
  policy_ids: string[];
}
interface Template { id: string; name: string; description: string | null; content: TemplateContent; updated_at: string }
interface Staged { device: Device; token: string; expires_at: string; command: string; bootstrap_script: string }

const DEFAULT: TemplateContent = {
  identity_pattern: "{tenant}-{site}-{name}", timezone: "Europe/Vienna", ntp_servers: ["pool.ntp.org"], dns_servers: ["9.9.9.9", "1.1.1.1"], wan_interface: "ether1",
  lan: { enabled: true, bridge_ports: ["ether2", "ether3", "ether4", "ether5"], cidr: null, dhcp: true },
  wan: { mode: "failover", links: [{ name: "WAN1", interface: "ether1", gateway: "dhcp", priority: 1, check_target: "1.1.1.1" }] },
  policy_ids: [],
};

function saveFile(name: string, text: string) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
  a.download = name;
  a.click();
}

export default function ZeroTouch() {
  const { can, me } = useAuth();
  const templates = useFetch<Template[]>(me?.active_tenant_id ? "/ztp/templates" : null);
  const devices = useFetch<Device[]>(me?.active_tenant_id ? "/ztp/devices" : null);
  const sites = useFetch<Site[]>("/sites");
  const policies = useFetch<{ id: string; name: string }[]>("/policies");
  const [edit, setEdit] = useState<Partial<Template> | null>(null);
  const [staging, setStaging] = useState(false);
  const [result, setResult] = useState<Staged[] | null>(null);
  useLive(() => void devices.reload(), ["device.ztp", "device.paired"]);
  if (!me?.active_tenant_id) return <><PageHeader title="Zero-Touch Provisioning" /><Card><p className="text-sm text-slate-500">Bitte links einen Mandanten wählen.</p></Card></>;
  const tplName = (id: string | null) => templates.data?.find((t) => t.id === id)?.name ?? "–";
  return (
    <>
      <PageHeader title="Zero-Touch Provisioning" subtitle="Router vorab registrieren, versenden – beim ersten Boot konfiguriert er sich selbst"
        actions={can("technician") && <Button onClick={() => setStaging(true)}>Geräte für Versand vorbereiten</Button>} />
      <div className="grid gap-6 xl:grid-cols-3">
        <Card title="Vorbereitete Geräte" className="xl:col-span-2">
          <Table head={["Gerät", "Serial", "Template", "Status", "Verlauf", ""]} empty={devices.data?.length === 0}>
            {devices.data?.map((d) => {
              const last = d.ztp_log[d.ztp_log.length - 1] as { at: string; msg: string } | undefined;
              return (
                <tr key={d.id}>
                  <td className="px-3 py-2 font-medium"><Link className="text-brand-700 hover:underline" to={`/devices/${d.id}`}>{d.name}</Link></td>
                  <td className="px-3 py-2 font-mono text-xs">{d.serial}</td>
                  <td className="px-3 py-2">{tplName(d.ztp_template_id)}</td>
                  <td className="px-3 py-2"><StatusBadge status={({ staged: "queued", paired: "running", provisioning: "running", provisioned: "success", failed: "failed" } as Record<string, string>)[d.ztp_state] ?? d.ztp_state} /> <span className="text-xs text-slate-500">{d.ztp_state}</span></td>
                  <td className="px-3 py-2 text-xs text-slate-500">{last ? `${fmtDate(last.at)} – ${last.msg}` : ""}</td>
                  <td className="px-3 py-2 text-right">
                    {can("technician") && d.pairing_status !== "paired" && (
                      <Button variant="ghost" onClick={() => confirm("Neues Bootstrap-Script erzeugen? Das bisherige wird ungültig.") && void api.post<string>(`/devices/${d.id}/ztp/bootstrap`).then((t) => saveFile(`sdwan-ztp-${d.serial}.rsc`, t))}>Bootstrap neu</Button>
                    )}
                  </td>
                </tr>
              );
            })}
          </Table>
        </Card>
        <Card title="Templates" actions={can("admin") && <Button variant="secondary" onClick={() => setEdit({ name: "", content: DEFAULT })}>+ Template</Button>}>
          <ul className="space-y-2 text-sm">
            {templates.data?.map((t) => (
              <li key={t.id} className="flex items-center justify-between">
                <div><div className="font-medium">{t.name}</div><div className="text-xs text-slate-500">{t.content.wan?.links.length ?? 0} WAN · {t.content.policy_ids.length} Policies · LAN {t.content.lan.enabled ? "an" : "aus"}</div></div>
                {can("admin") && <Button variant="ghost" onClick={() => setEdit(t)}>Bearbeiten</Button>}
              </li>
            ))}
            {templates.data?.length === 0 && <li className="text-slate-400">Noch keine Templates</li>}
          </ul>
          <div className="mt-4 rounded-lg bg-slate-50 p-3 text-xs text-slate-600">
            <b>Ablauf:</b> 1. Template anlegen · 2. Geräte mit Seriennummer vorbereiten · 3. Bootstrap-Script im Lager importieren (oder per Netinstall <code>-s</code>) · 4. Versenden – beim Kunden: Strom + Internet an ether1 genügt.
          </div>
        </Card>
      </div>
      {edit && <TemplateModal tpl={edit} policies={policies.data ?? []} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); void templates.reload(); }} />}
      {staging && <StageModal templates={templates.data ?? []} sites={sites.data ?? []} onClose={() => setStaging(false)} onDone={(r) => { setStaging(false); setResult(r); void devices.reload(); }} />}
      <Modal open={!!result} onClose={() => setResult(null)} title="Bootstrap-Scripts" wide>
        <p className="mb-3 text-sm">Die Tokens werden nur jetzt angezeigt. Scripts herunterladen und auf den Routern importieren (<code>/import sdwan-ztp.rsc</code>).</p>
        <ul className="space-y-2 text-sm">
          {result?.map((s) => (
            <li key={s.device.id} className="flex items-center justify-between border-b pb-2">
              <span><b>{s.device.name}</b> <span className="font-mono text-xs text-slate-500">{s.device.serial}</span></span>
              <Button variant="secondary" onClick={() => saveFile(`sdwan-ztp-${s.device.serial}.rsc`, s.bootstrap_script)}>.rsc herunterladen</Button>
            </li>
          ))}
        </ul>
        <div className="mt-4 flex justify-end">
          <Button onClick={() => result && saveFile("sdwan-ztp-tokens.csv", "name,serial,token,expires_at,command\n" + result.map((s) => [s.device.name, s.device.serial, s.token, s.expires_at, `"${s.command.replaceAll('"', '""')}"`].join(",")).join("\n"))}>Alle als CSV</Button>
        </div>
      </Modal>
    </>
  );
}

function StageModal({ templates, sites, onClose, onDone }: { templates: Template[]; sites: Site[]; onClose: () => void; onDone: (r: Staged[]) => void }) {
  const [tpl, setTpl] = useState(templates[0]?.id ?? "");
  const [site, setSite] = useState("");
  const [rows, setRows] = useState("");
  const [ttl, setTtl] = useState(180);
  const { busy, error, run } = useAction();
  return (
    <Modal open onClose={onClose} title="Geräte für Versand vorbereiten" wide>
      <ErrorBox error={error} />
      <div className="grid gap-3 md:grid-cols-3">
        <Select label="Template" value={tpl} onChange={(e) => setTpl(e.target.value)}>
          <option value="">– nur Onboarding, keine Basiskonfig –</option>
          {templates.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
        </Select>
        <Select label="Standort (für alle)" value={site} onChange={(e) => setSite(e.target.value)}>
          <option value="">– keiner –</option>
          {sites.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
        </Select>
        <Input label="Token gültig (Tage)" type="number" min={1} max={730} value={ttl} onChange={(e) => setTtl(Number(e.target.value))} />
      </div>
      <div className="mt-3">
        <Textarea label="Geräte – eine Zeile pro Gerät: Name, Seriennummer[, lokale VRRP-Adresse]" rows={8} placeholder={"filiale-graz, HGF0123ABC, 192.168.110.21/24\nfiliale-linz, HGF0456DEF, 192.168.110.22/24"} value={rows} onChange={(e) => setRows(e.target.value)} />
      </div>
      <div className="mt-4 flex justify-end">
        <Button disabled={busy} onClick={() => void run(async () => {
          const devices = rows.split("\n").map((l) => l.split(/[,;\t]/).map((x) => x.trim())).filter((x) => x[0] && x[1]).map(([name, serial, vrrp]) => ({ name, serial, vrrp_local_address: vrrp || null }));
          if (!devices.length) throw new Error("Keine Geräte angegeben");
          onDone(await api.post<Staged[]>("/ztp/stage", { template_id: tpl || null, site_id: site || null, ttl_days: ttl, devices }));
        })}>Vorbereiten</Button>
      </div>
    </Modal>
  );
}

function TemplateModal({ tpl, policies, onClose, onSaved }: { tpl: Partial<Template>; policies: { id: string; name: string }[]; onClose: () => void; onSaved: () => void }) {
  const [name, setName] = useState(tpl.name ?? "");
  const [c, setC] = useState<TemplateContent>(structuredClone(tpl.content ?? DEFAULT));
  const [wanJson, setWanJson] = useState(JSON.stringify(c.wan ?? { mode: "failover", links: [] }, null, 2));
  const [vrrpJson, setVrrpJson] = useState(JSON.stringify(c.vrrp ?? [], null, 2));
  const { busy, error, run } = useAction();
  const list = (v: string) => v.split(/[\s,]+/).filter(Boolean);
  return (
    <Modal open onClose={onClose} title={tpl.id ? "Template bearbeiten" : "Neues Template"} wide>
      <ErrorBox error={error} />
      <div className="grid gap-3 md:grid-cols-2">
        <Input label="Name" value={name} onChange={(e) => setName(e.target.value)} />
        <Input label="Identity-Muster ({tenant} {site} {name} {serial})" value={c.identity_pattern} onChange={(e) => setC({ ...c, identity_pattern: e.target.value })} />
        <Input label="Zeitzone" value={c.timezone} onChange={(e) => setC({ ...c, timezone: e.target.value })} />
        <Input label="WAN-Interface (DHCP beim ersten Boot)" value={c.wan_interface} onChange={(e) => setC({ ...c, wan_interface: e.target.value })} />
        <Input label="DNS-Server" value={c.dns_servers.join(", ")} onChange={(e) => setC({ ...c, dns_servers: list(e.target.value) })} />
        <Input label="NTP-Server" value={c.ntp_servers.join(", ")} onChange={(e) => setC({ ...c, ntp_servers: list(e.target.value) })} />
      </div>
      <fieldset className="mt-4 rounded-lg border p-3">
        <legend className="px-1 text-sm font-medium">LAN</legend>
        <div className="grid gap-3 md:grid-cols-3">
          <Checkbox label="LAN-Bridge + DHCP konfigurieren" checked={c.lan.enabled} onChange={(v) => setC({ ...c, lan: { ...c.lan, enabled: v } })} />
          <Input label="Bridge-Ports" value={c.lan.bridge_ports.join(", ")} onChange={(e) => setC({ ...c, lan: { ...c.lan, bridge_ports: list(e.target.value) } })} />
          <Input label="LAN-IP/Präfix (leer = aus Standort)" value={c.lan.cidr ?? ""} onChange={(e) => setC({ ...c, lan: { ...c.lan, cidr: e.target.value || null } })} />
        </div>
      </fieldset>
      <div className="mt-4 grid gap-3 md:grid-cols-2">
        <Textarea label="WAN-Vorlage (JSON, wie WAN-Tab)" rows={8} value={wanJson} onChange={(e) => setWanJson(e.target.value)} />
        <div>
          <span className="mb-1 block text-sm font-medium text-slate-700">Firewall-Policies</span>
          <div className="max-h-48 space-y-1 overflow-y-auto rounded-lg border p-2">
            {policies.map((p) => <Checkbox key={p.id} label={p.name} checked={c.policy_ids.includes(p.id)} onChange={(v) => setC({ ...c, policy_ids: v ? [...c.policy_ids, p.id] : c.policy_ids.filter((x) => x !== p.id) })} />)}
            {policies.length === 0 && <span className="text-xs text-slate-400">Keine Policies vorhanden</span>}
          </div>
        </div>
      </div>
      <div className="mt-4">
        <Textarea label="VRRP-Vorlage (JSON-Liste, wie VRRP-Tab; lokale Adresse pro Gerät beim Vorbereiten)" rows={6}
          placeholder={'[{"name": "vrrp-kassen", "interface": "ether2", "vrid": 110, "priority": 100, "vip": "192.168.110.1", "linked_wan_slot": 1}]'}
          value={vrrpJson} onChange={(e) => setVrrpJson(e.target.value)} />
      </div>
      <div className="mt-4 flex justify-between">
        {tpl.id ? <Button variant="danger" onClick={() => confirm("Template löschen?") && void run(async () => { await api.del(`/ztp/templates/${tpl.id}`); onSaved(); })}>Löschen</Button> : <span />}
        <Button disabled={busy} onClick={() => void run(async () => {
          const body = { name, content: { ...c, wan: JSON.parse(wanJson), vrrp: JSON.parse(vrrpJson || "[]") } };
          if (tpl.id) await api.put(`/ztp/templates/${tpl.id}`, body);
          else await api.post("/ztp/templates", body);
          onSaved();
        })}>Speichern</Button>
      </div>
      <p className="mt-2 text-xs text-slate-500"><Badge>Hinweis</Badge> Basiskonfiguration wird mit dem Pairing übertragen; WAN und Policies pusht die Cloud nach dem ersten erfolgreichen Kontakt.</p>
    </Modal>
  );
}
