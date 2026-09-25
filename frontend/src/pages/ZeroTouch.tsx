import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Icon } from "../components/Icon";
import { Button, Card, Checkbox, CodeBlock, EmptyState, ErrorBox, Input, Loading, Modal, PageHeader, Select, StatusBadge, Textarea, cls, statusInfo, useAction } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtAgo, fmtDate, fmtShort } from "../lib/format";
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

const splitCmd = (c: string) => c.split(/;\s+(?=[/:])/).join("\n");

function tplSummary(t: Template) {
  const c = t.content;
  return [t.description, `${c.wan?.links.length ?? 0} WAN`, c.vrrp?.length ? `VRRP (${c.vrrp.length})` : null, `${c.policy_ids.length} Policies`, `LAN ${c.lan.enabled ? "an" : "aus"}`].filter(Boolean).join(" · ");
}

export default function ZeroTouch() {
  const { can, me } = useAuth();
  const nav = useNavigate();
  const templates = useFetch<Template[]>(me?.active_tenant_id ? "/ztp/templates" : null);
  const devices = useFetch<Device[]>(me?.active_tenant_id ? "/ztp/devices" : null);
  const sites = useFetch<Site[]>("/sites");
  const policies = useFetch<{ id: string; name: string }[]>("/policies");
  const [edit, setEdit] = useState<Partial<Template> | null>(null);
  const [single, setSingle] = useState(false);
  const [staging, setStaging] = useState(false);
  const [result, setResult] = useState<Staged[] | null>(null);
  useLive(() => void devices.reload(), ["device.ztp", "device.paired", "device.status"]);
  if (!me?.active_tenant_id) return <><PageHeader title="Zero-Touch-Provisioning" /><Card><EmptyState icon="building" title="Bitte einen Mandanten wählen" text="Vorlagen und vorbereitete Router gehören zu einem Mandanten." /></Card></>;
  const tplName = (id: string | null) => templates.data?.find((t) => t.id === id)?.name ?? "–";
  const siteName = (id: string | null) => sites.data?.find((s) => s.id === id)?.name ?? "–";
  const list = devices.data ?? [];
  const n = (states: string[]) => list.filter((d) => states.includes(d.ztp_state)).length;
  const month = Date.now() - 30 * 86400e3;
  const doneRecent = list.filter((d) => d.ztp_state === "provisioned" && new Date(d.ztp_log[d.ztp_log.length - 1]?.at ?? d.created_at).getTime() >= month).length;
  const failed = n(["failed"]);
  const stats: { st: string; label: string; sub: string; n: number }[] = [
    { st: "staged", label: "Vorbereitet", sub: "Warten auf ersten Kontakt", n: n(["staged"]) },
    { st: "connected", label: "Verbunden", sub: "Konfiguration wird übertragen", n: n(["paired", "provisioning"]) },
    { st: "provisioned", label: "Provisioniert", sub: "In den letzten 30 Tagen", n: doneRecent },
    ...(failed ? [{ st: "failed", label: "Fehlgeschlagen", sub: "Details im Gerätedetail", n: failed }] : []),
  ];
  const stateKey = (s: string) => (s === "paired" || s === "provisioning" ? "connected" : s);
  return (
    <>
      <PageHeader title="Zero-Touch-Provisioning" subtitle="Neue Router per Bootstrap-Befehl automatisch mit einer Vorlage einrichten"
        actions={can("technician") && <>
          <Button variant="secondary" icon="list" onClick={() => setStaging(true)}>Mehrere vorbereiten</Button>
          <Button icon="plus" onClick={() => setSingle(true)}>Router vorbereiten</Button>
        </>} />
      <div className={cls("mb-4 grid gap-3", stats.length > 3 ? "md:grid-cols-4" : "md:grid-cols-3")}>
        {stats.map((k) => {
          const [, tone, icon] = statusInfo(k.st);
          return (
            <div key={k.st} className="flex items-center gap-3 rounded-lg border border-line bg-panel px-4 py-3">
              <span className={cls("flex h-[30px] w-[30px] items-center justify-center rounded-[7px] text-[15px]", { green: "bg-green-bg text-green-text", blue: "bg-blue-bg text-blue-text", red: "bg-red-bg text-red-text", gray: "bg-gray-bg text-gray-text", orange: "bg-orange-bg text-orange-text", neutral: "bg-sunken text-fg2" }[tone])}><Icon name={icon} /></span>
              <span className="flex flex-1 flex-col leading-tight"><span className="font-medium">{k.label}</span><span className="text-xs text-fg3">{k.sub}</span></span>
              <span className="text-[22px] font-semibold">{k.n}</span>
            </div>
          );
        })}
      </div>
      <section className="mb-4 overflow-hidden rounded-lg border border-line bg-panel">
        {!devices.data ? <Loading rows={3} /> : list.length === 0 ? (
          <EmptyState icon="package" title="Noch keine Router vorbereitet" text="Seriennummer registrieren, Vorlage wählen und den Bootstrap-Befehl auf dem Router ausführen – der Rest passiert automatisch." action={can("technician") && <Button icon="plus" onClick={() => setSingle(true)}>Router vorbereiten</Button>} />
        ) : (
          <div className="overflow-x-auto">
            <div className="min-w-[980px]">
              <div className="grid grid-cols-[140px_130px_minmax(0,1fr)_150px_130px_110px_120px_110px] gap-3 border-b border-line bg-panel2 px-4 py-2 text-xs font-medium text-fg3">
                <span>Seriennummer</span><span>Name</span><span>Vorlage</span><span>Ziel-Standort</span><span>Status</span><span>Erstellt</span><span>Letzter Kontakt</span><span />
              </div>
              {list.map((d) => (
                <div key={d.id} className="grid h-[46px] cursor-pointer grid-cols-[140px_130px_minmax(0,1fr)_150px_130px_110px_120px_110px] items-center gap-3 border-b border-line px-4 last:border-b-0 hover:bg-hover"
                  role="link" tabIndex={0} onClick={() => nav(`/devices/${d.id}`)} onKeyDown={(e) => e.key === "Enter" && nav(`/devices/${d.id}`)}>
                  <span className="truncate font-mono text-xs font-medium">{d.serial}</span>
                  <span className="truncate text-fg2" title={d.model ?? undefined}>{d.name}</span>
                  <span className="truncate">{tplName(d.ztp_template_id)}</span>
                  <span className="truncate">{siteName(d.site_id)}</span>
                  <span><StatusBadge status={stateKey(d.ztp_state)} /></span>
                  <span className="font-mono text-xs text-fg2">{fmtShort(d.created_at)}</span>
                  <span className="text-fg2">{d.last_seen_at ? fmtAgo(d.last_seen_at) : "—"}</span>
                  <span className="flex justify-end">
                    {can("technician") && d.pairing_status !== "paired" && (
                      <Button size="sm" variant="ghost" onClick={(e) => { e.stopPropagation(); if (confirm("Neues Bootstrap-Script erzeugen? Das bisherige wird ungültig.")) void api.post<string>(`/devices/${d.id}/ztp/bootstrap`).then((t) => saveFile(`ztp-${d.serial}.rsc`, t)); }}>Bootstrap neu</Button>
                    )}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
      </section>
      <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
        <Card title="Vorlagen" subtitle={`${templates.data?.length ?? 0}`} flush actions={can("admin") && <Button size="sm" variant="secondary" icon="plus" onClick={() => setEdit({ name: "", content: DEFAULT })}>Vorlage</Button>}>
          {templates.data?.length === 0 && <EmptyState compact title="Noch keine Vorlagen" />}
          {templates.data?.map((t) => (
            <div key={t.id} className="flex items-center gap-3 border-b border-line px-4 py-2.5 last:border-b-0">
              <span className="flex min-w-0 flex-1 flex-col leading-tight"><span className="font-medium">{t.name}</span><span className="truncate text-xs text-fg2">{tplSummary(t)}</span></span>
              {can("admin") && <Button size="sm" variant="ghost" icon="edit" onClick={() => setEdit(t)}>Bearbeiten</Button>}
            </div>
          ))}
        </Card>
        <Card title="Ablauf">
          <ol className="list-decimal space-y-1 pl-5 text-fg2">
            <li>Vorlage anlegen (Basis, WAN, VRRP, Policies).</li>
            <li>Router mit Seriennummer vorbereiten.</li>
            <li>Bootstrap-Befehl im Lager ausführen oder per Netinstall (<code>-s</code>) mitgeben.</li>
            <li>Versenden – vor Ort genügen Strom und Internet am WAN-Port der Vorlage.</li>
          </ol>
        </Card>
      </div>
      {edit && <TemplateModal tpl={edit} policies={policies.data ?? []} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); void templates.reload(); }} />}
      {single && <SingleStageModal templates={templates.data ?? []} sites={sites.data ?? []} onClose={() => setSingle(false)} onDone={() => void devices.reload()} />}
      {staging && <StageModal templates={templates.data ?? []} sites={sites.data ?? []} onClose={() => setStaging(false)} onDone={(r) => { setStaging(false); setResult(r); void devices.reload(); }} />}
      <Modal open={!!result} onClose={() => setResult(null)} title="Bootstrap-Scripts" subtitle="Die Tokens werden nur jetzt angezeigt." size="lg"
        footer={<>
          <Button variant="secondary" icon="download" onClick={() => result && saveFile("ztp-tokens.csv", "name,serial,token,expires_at,command\n" + result.map((s) => [s.device.name, s.device.serial, s.token, s.expires_at, `"${s.command.replaceAll('"', '""')}"`].join(",")).join("\n"))}>Alle als CSV</Button>
          <Button onClick={() => setResult(null)}>Fertig</Button>
        </>}>
        <p className="mb-3 text-fg2">Scripts herunterladen und auf den Routern importieren (<code>/import ztp.rsc</code>) oder den Befehl je Gerät ausführen. Token gültig bis {result?.[0] ? fmtDate(result[0].expires_at) : "–"}, an die Seriennummer gebunden.</p>
        {result?.map((s) => (
          <div key={s.device.id} className="flex items-center justify-between gap-2 border-b border-line py-2 last:border-b-0">
            <span><b>{s.device.name}</b> <span className="font-mono text-xs text-fg3">{s.device.serial}</span></span>
            <Button size="sm" variant="secondary" icon="download" onClick={() => saveFile(`ztp-${s.device.serial}.rsc`, s.bootstrap_script)}>.rsc</Button>
          </div>
        ))}
      </Modal>
    </>
  );
}

function SingleStageModal({ templates, sites, onClose, onDone }: { templates: Template[]; sites: Site[]; onClose: () => void; onDone: () => void }) {
  const [serial, setSerial] = useState("");
  const [name, setName] = useState("");
  const [site, setSite] = useState("");
  const [tpl, setTpl] = useState(templates[0]?.id ?? "");
  const [vrrpLocal, setVrrpLocal] = useState("");
  const [ttl, setTtl] = useState(180);
  const [staged, setStaged] = useState<Staged | null>(null);
  const { busy, error, run } = useAction();
  const t = templates.find((x) => x.id === tpl);
  const submit = () => void run(async () => {
    const [r] = await api.post<Staged[]>("/ztp/stage", { template_id: tpl || null, ttl_days: ttl, devices: [{ name: name || serial.toLowerCase(), serial, site_id: site || null, vrrp_local_address: vrrpLocal || null }] });
    setStaged(r);
    onDone();
  });
  return (
    <Modal open onClose={onClose} title="Router vorbereiten" subtitle="Seriennummer registrieren, Vorlage wählen, Befehl auf dem Router ausführen." size="lg"
      footer={staged ? <>
        <Button variant="secondary" icon="download" onClick={() => saveFile(`ztp-${staged.device.serial}.rsc`, staged.bootstrap_script)}>Script (.rsc)</Button>
        <Button onClick={onClose}>Fertig</Button>
      </> : <>
        <Button variant="secondary" onClick={onClose}>Abbrechen</Button>
        <Button disabled={busy || !serial} onClick={submit}>{busy ? "Bereite vor …" : "Vorbereiten"}</Button>
      </>}>
      <ErrorBox error={error} />
      {!staged ? (
        <div className="flex flex-col gap-4">
          <div className="grid gap-3 sm:grid-cols-3">
            <Input label="Seriennummer" value={serial} onChange={(e) => setSerial(e.target.value.toUpperCase())} className="font-mono" placeholder="HGK09C5D1X4" autoFocus />
            <Input label="Gerätename" hint="leer = Seriennummer" value={name} onChange={(e) => setName(e.target.value)} placeholder="fil-kassa-sued-gw1" />
            <Select label="Ziel-Standort" value={site} onChange={(e) => setSite(e.target.value)}>
              <option value="">– keiner –</option>
              {sites.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </Select>
          </div>
          <div className="flex flex-col gap-2" role="radiogroup" aria-label="Vorlage">
            <span className="font-medium">Vorlage</span>
            {[...templates.map((x) => ({ id: x.id, name: x.name, desc: tplSummary(x) })), { id: "", name: "Ohne Vorlage", desc: "Nur Onboarding, keine Basiskonfiguration" }].map((x) => {
              const on = x.id === tpl;
              return (
                <button key={x.id || "none"} type="button" role="radio" aria-checked={on} onClick={() => setTpl(x.id)}
                  className={cls("flex cursor-pointer items-center gap-3 rounded-[7px] border px-3 py-2.5 text-left", on ? "border-blue bg-blue-bg" : "border-line bg-panel hover:bg-hover")}>
                  <span className={cls("h-4 w-4 shrink-0 rounded-full", on ? "border-[5px] border-blue" : "border-[1.5px] border-line-strong")} />
                  <span className="flex flex-col leading-tight"><span className="font-medium">{x.name}</span><span className="text-xs text-fg2">{x.desc}</span></span>
                </button>
              );
            })}
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            {t?.content.vrrp?.length ? <Input label="Lokale VRRP-Adresse" hint="je Gerät verschieden, mit Präfix" value={vrrpLocal} onChange={(e) => setVrrpLocal(e.target.value)} placeholder="192.168.110.22/24" className="font-mono" /> : null}
            <Input label="Token gültig (Tage)" type="number" min={1} max={730} value={ttl} onChange={(e) => setTtl(Number(e.target.value))} />
          </div>
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <span className="font-medium">Bootstrap-Befehl</span>
            <span className="text-xs text-fg3">Im Terminal des Routers ausführen (Werkszustand, Internet am WAN-Port{t ? ` ${t.content.wan_interface}` : ""})</span>
          </div>
          <CodeBlock text={`# ${t ? `Vorlage: ${t.name}` : "Ohne Vorlage"} · SN ${staged.device.serial}${site ? ` · ${sites.find((s) => s.id === site)?.name}` : ""}\n${splitCmd(staged.command)}`} />
          <div className="flex items-center gap-1.5 text-xs text-fg3"><Icon name="clock" className="text-[13px]" />Token gültig bis {fmtDate(staged.expires_at)} · an Seriennummer {staged.device.serial} gebunden · wird nur jetzt angezeigt</div>
        </div>
      )}
    </Modal>
  );
}

function StageModal({ templates, sites, onClose, onDone }: { templates: Template[]; sites: Site[]; onClose: () => void; onDone: (r: Staged[]) => void }) {
  const [tpl, setTpl] = useState(templates[0]?.id ?? "");
  const [site, setSite] = useState("");
  const [rows, setRows] = useState("");
  const [ttl, setTtl] = useState(180);
  const { busy, error, run } = useAction();
  return (
    <Modal open onClose={onClose} title="Mehrere Router vorbereiten" subtitle="Seriennummern gesammelt registrieren – je Gerät entsteht ein Bootstrap-Script." size="lg"
      footer={<>
        <Button variant="secondary" onClick={onClose}>Abbrechen</Button>
        <Button disabled={busy} onClick={() => void run(async () => {
          const devices = rows.split("\n").map((l) => l.split(/[,;\t]/).map((x) => x.trim())).filter((x) => x[0] && x[1]).map(([name, serial, vrrp]) => ({ name, serial, vrrp_local_address: vrrp || null }));
          if (!devices.length) throw new Error("Keine Geräte angegeben");
          onDone(await api.post<Staged[]>("/ztp/stage", { template_id: tpl || null, site_id: site || null, ttl_days: ttl, devices }));
        })}>{busy ? "Bereite vor …" : "Vorbereiten"}</Button>
      </>}>
      <ErrorBox error={error} />
      <div className="grid gap-3 md:grid-cols-3">
        <Select label="Vorlage" value={tpl} onChange={(e) => setTpl(e.target.value)}>
          <option value="">– nur Onboarding, keine Basiskonfiguration –</option>
          {templates.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
        </Select>
        <Select label="Standort (für alle)" value={site} onChange={(e) => setSite(e.target.value)}>
          <option value="">– keiner –</option>
          {sites.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
        </Select>
        <Input label="Token gültig (Tage)" type="number" min={1} max={730} value={ttl} onChange={(e) => setTtl(Number(e.target.value))} />
      </div>
      <div className="mt-3">
        <Textarea label="Geräte" hint="Eine Zeile pro Gerät: Name, Seriennummer[, lokale VRRP-Adresse]" rows={8} placeholder={"filiale-graz, HGF0123ABC, 192.168.110.21/24\nfiliale-linz, HGF0456DEF, 192.168.110.22/24"} value={rows} onChange={(e) => setRows(e.target.value)} />
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
    <Modal open onClose={onClose} title={tpl.id ? "Vorlage bearbeiten" : "Neue Vorlage"} subtitle="Basiskonfiguration kommt mit dem Pairing; WAN, VRRP und Policies überträgt die Cloud nach dem ersten Kontakt." size="xl"
      footer={<>
        {tpl.id && <Button variant="danger-outline" icon="trash" className="mr-auto" onClick={() => confirm("Vorlage löschen?") && void run(async () => { await api.del(`/ztp/templates/${tpl.id}`); onSaved(); })}>Löschen</Button>}
        <Button variant="secondary" onClick={onClose}>Abbrechen</Button>
        <Button disabled={busy} onClick={() => void run(async () => {
          const body = { name, content: { ...c, wan: JSON.parse(wanJson), vrrp: JSON.parse(vrrpJson || "[]") } };
          if (tpl.id) await api.put(`/ztp/templates/${tpl.id}`, body);
          else await api.post("/ztp/templates", body);
          onSaved();
        })}>Speichern</Button>
      </>}>
      <ErrorBox error={error} />
      <div className="grid gap-3 md:grid-cols-2">
        <Input label="Name" value={name} onChange={(e) => setName(e.target.value)} />
        <Input label="Identity-Muster" hint="Platzhalter: {tenant} {site} {name} {serial}" value={c.identity_pattern} onChange={(e) => setC({ ...c, identity_pattern: e.target.value })} />
        <Input label="Zeitzone" value={c.timezone} onChange={(e) => setC({ ...c, timezone: e.target.value })} />
        <Input label="WAN-Interface (DHCP beim ersten Boot)" value={c.wan_interface} onChange={(e) => setC({ ...c, wan_interface: e.target.value })} />
        <Input label="DNS-Server" value={c.dns_servers.join(", ")} onChange={(e) => setC({ ...c, dns_servers: list(e.target.value) })} />
        <Input label="NTP-Server" value={c.ntp_servers.join(", ")} onChange={(e) => setC({ ...c, ntp_servers: list(e.target.value) })} />
      </div>
      <fieldset className="mt-4 rounded-lg border border-line p-3">
        <legend className="px-1 font-semibold">LAN</legend>
        <div className="grid gap-3 md:grid-cols-3">
          <Checkbox label="LAN-Bridge + DHCP konfigurieren" checked={c.lan.enabled} onChange={(v) => setC({ ...c, lan: { ...c.lan, enabled: v } })} />
          <Input label="Bridge-Ports" value={c.lan.bridge_ports.join(", ")} onChange={(e) => setC({ ...c, lan: { ...c.lan, bridge_ports: list(e.target.value) } })} />
          <Input label="LAN-IP/Präfix (leer = aus Standort)" value={c.lan.cidr ?? ""} onChange={(e) => setC({ ...c, lan: { ...c.lan, cidr: e.target.value || null } })} />
        </div>
      </fieldset>
      <div className="mt-4 grid gap-3 md:grid-cols-2">
        <Textarea label="WAN-Vorlage (JSON, wie WAN-Tab)" rows={8} value={wanJson} onChange={(e) => setWanJson(e.target.value)} />
        <div>
          <span className="mb-1.5 block font-medium">Firewall-Policies</span>
          <div className="max-h-48 space-y-1 overflow-y-auto rounded-md border border-line p-2">
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
    </Modal>
  );
}
