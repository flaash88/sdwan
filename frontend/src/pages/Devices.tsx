import { useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Icon } from "../components/Icon";
import PairingBox from "../components/PairingBox";
import { CpuBar, DeviceStatusBadge, VrrpPill, WanPill } from "../components/fleet";
import { Button, ErrorBox, Input, Loading, Modal, Notice, PageHeader, RowCheck, Segment, Select, SelectionBar, cls, useAction } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { firmwareUpdate, useDevices, useFleetState, useSites } from "../lib/fleet";
import { fmtAgo, fmtUptime } from "../lib/format";
import type { Device, PairingInfo, Site } from "../lib/types";
import { useFetch } from "../lib/useFetch";

type StatusFilter = "all" | "online" | "offline" | "pending";
const COLS = "grid-cols-[24px_96px_minmax(140px,1fr)_96px_100px_100px_120px_96px_104px_76px_96px]";

function csv(rows: string[][]) {
  return rows.map((r) => r.map((c) => `"${String(c ?? "").replaceAll('"', '""')}"`).join(";")).join("\n");
}

export default function Devices() {
  const { can, me } = useAuth();
  const nav = useNavigate();
  const [params, setParams] = useSearchParams();
  const devices = useDevices();
  const sites = useSites();
  const fleet = useFleetState();
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [site, setSite] = useState("");
  const [tags, setTags] = useState<string[]>([]);
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [dialog, setDialog] = useState<null | "policy" | "firmware" | "backup">(null);
  const status = (params.get("status") as StatusFilter) || "all";
  const setStatus = (s: StatusFilter) => setParams(s === "all" ? {} : { status: s }, { replace: true });
  const needsTenant = me?.user.is_superuser && !me.active_tenant_id;
  const siteName = (id: string | null) => sites.data?.find((s) => s.id === id)?.name ?? "–";
  const state = fleet.data?.devices ?? {};

  const all = devices.data ?? [];
  const allTags = useMemo(() => [...new Set(all.flatMap((d) => d.tags))].sort(), [all]);
  const stOf = (d: Device): StatusFilter => (d.pairing_status !== "paired" ? "pending" : d.status === "online" ? "online" : "offline");
  const base = all.filter((d) =>
    (!site || d.site_id === site) &&
    (!tags.length || tags.some((t) => d.tags.includes(t))) &&
    (!q || [d.name, d.identity, d.tunnel_ip, d.mesh_ip, d.serial, d.model, siteName(d.site_id)].some((v) => v?.toLowerCase().includes(q.toLowerCase()))));
  const list = base.filter((d) => status === "all" || stOf(d) === status);
  const count = (s: StatusFilter) => base.filter((d) => stOf(d) === s).length;
  const updates = all.filter((d) => firmwareUpdate(d)?.update_available).length;
  const selected = all.filter((d) => sel.has(d.id));
  const allOn = list.length > 0 && list.every((d) => sel.has(d.id));
  const toggle = (id: string, on: boolean) => setSel((p) => { const n = new Set(p); if (on) n.add(id); else n.delete(id); return n; });

  const exportCsv = () => {
    const rows = [["Name", "Status", "Standort", "Modell", "Seriennummer", "RouterOS", "Tunnel-IP", "Aktiver WAN", "VRRP-Rolle", "CPU %", "Uptime", "Zuletzt gesehen", "Tags"],
      ...list.map((d) => [d.name, stOf(d), siteName(d.site_id), d.model ?? "", d.serial ?? "", d.routeros_version ?? "", d.tunnel_ip,
        state[d.id]?.active_wan?.name ?? "", state[d.id]?.vrrp_role ?? "", String(d.facts?.cpu_load ?? ""), d.uptime ?? "", d.last_seen_at ?? "", d.tags.join(", ")])];
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob(["﻿" + csv(rows)], { type: "text/csv" }));
    a.download = "geraete.csv";
    a.click();
  };

  return (
    <>
      <PageHeader
        title="Geräte"
        subtitle={`${all.length} Geräte${updates ? ` · ${updates} mit ausstehendem Firmware-Update` : ""}`}
        actions={<>
          <Button variant="secondary" icon="download" onClick={exportCsv} disabled={!list.length}>Exportieren</Button>
          {can("technician") && <Button icon="plus" onClick={() => setOpen(true)} disabled={needsTenant} title={needsTenant ? "Bitte zuerst einen Mandanten wählen" : undefined}>Gerät hinzufügen</Button>}
        </>}
      />
      <div className="mb-3 flex flex-wrap items-center gap-2.5">
        <div className="flex h-8 w-[260px] items-center gap-2 rounded-md border border-line-strong bg-panel px-2.5 text-fg3 focus-within:border-blue">
          <Icon name="search" className="text-[14px]" />
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Name, IP oder Seriennummer" aria-label="Geräte filtern" className="min-w-0 flex-1 bg-transparent text-fg outline-none placeholder:text-fg3" />
        </div>
        <label className="flex h-8 items-center gap-1.5 rounded-md border border-line-strong bg-panel pl-2.5">
          <span className="text-fg3">Standort:</span>
          <select value={site} onChange={(e) => setSite(e.target.value)} className="h-full cursor-pointer bg-transparent pr-2 font-medium outline-none focus-visible:outline-none">
            <option value="">Alle</option>
            {sites.data?.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
        </label>
        <Segment label="Status" value={status} onChange={setStatus} options={[
          { value: "all", label: "Alle", count: base.length }, { value: "online", label: "Online", count: count("online") },
          { value: "offline", label: "Offline", count: count("offline") },
          ...(count("pending") ? [{ value: "pending" as const, label: "Nicht verbunden", count: count("pending") }] : []),
        ]} />
        {allTags.length > 0 && <>
          <div className="h-5 w-px bg-line" />
          <span className="text-xs text-fg3">Tags</span>
          {allTags.map((t) => {
            const on = tags.includes(t);
            return (
              <button key={t} type="button" aria-pressed={on} onClick={() => setTags(on ? tags.filter((x) => x !== t) : [...tags, t])}
                className={cls("flex h-[26px] cursor-pointer items-center gap-1 rounded-full border px-2.5 text-xs font-medium", on ? "border-blue bg-blue-bg text-blue-text" : "border-line-strong bg-panel text-fg2 hover:bg-hover")}>
                {on && <Icon name="check" className="text-[12px]" />}{t}
              </button>
            );
          })}
        </>}
      </div>
      <div className="mb-3">
        <SelectionBar count={sel.size} onClear={() => setSel(new Set())}>
          {can("technician") && <>
            <Button variant="secondary" icon="shield" onClick={() => setDialog("policy")}>Policy pushen</Button>
            <Button variant="secondary" icon="upload" onClick={() => setDialog("firmware")}>Firmware-Update</Button>
            <Button variant="secondary" icon="archive" onClick={() => setDialog("backup")}>Backup erstellen</Button>
          </>}
        </SelectionBar>
      </div>
      <ErrorBox error={devices.error} />
      <section className="overflow-hidden rounded-lg border border-line bg-panel">
        {!devices.data ? <Loading rows={6} /> : (
          <div className="overflow-x-auto">
            <div role="table" aria-label="Geräte" className="min-w-[1150px]">
              <div role="row" className={cls("grid h-9 items-center gap-2 border-b border-line bg-panel2 px-3.5 text-xs font-medium text-fg3", COLS)}>
                <span role="columnheader"><RowCheck label="Alle auswählen" checked={allOn} indeterminate={!allOn && list.some((d) => sel.has(d.id))} onChange={(v) => setSel(v ? new Set([...sel, ...list.map((d) => d.id)]) : new Set([...sel].filter((id) => !list.some((d) => d.id === id))))} /></span>
                {["Status", "Name", "Standort", "Modell", "RouterOS", "Aktiver WAN", "VRRP-Rolle", "CPU", "Uptime", "Zuletzt gesehen"].map((h) => <span role="columnheader" key={h}>{h}</span>)}
              </div>
              {list.map((d) => {
                const on = sel.has(d.id);
                const up = firmwareUpdate(d);
                const off = d.status !== "online" || d.pairing_status !== "paired";
                return (
                  <div role="row" key={d.id} tabIndex={0} onClick={() => nav(`/devices/${d.id}`)} onKeyDown={(e) => e.key === "Enter" && nav(`/devices/${d.id}`)}
                    className={cls("grid h-12 cursor-pointer items-center gap-2 border-b border-line px-3.5 last:border-b-0 hover:bg-hover", COLS, on && "bg-blue-bg")}>
                    <span><RowCheck label={`${d.name} auswählen`} checked={on} onChange={(v) => toggle(d.id, v)} /></span>
                    <span><DeviceStatusBadge device={d} /></span>
                    <span className="flex min-w-0 flex-col leading-tight">
                      <span className="truncate font-medium">{d.name}</span>
                      <span className="truncate font-mono text-[11.5px] text-fg3">{d.tunnel_ip}</span>
                    </span>
                    <span className="truncate">{siteName(d.site_id)}</span>
                    <span className="truncate text-fg2" title={[d.model, d.architecture, d.serial && `SN ${d.serial}`].filter(Boolean).join(" · ") || undefined}>{d.model ?? "–"}</span>
                    <span className="flex min-w-0 flex-col leading-tight">
                      <span className="truncate font-mono text-xs">{d.routeros_version?.replace(/\s*\(.*\)/, "") ?? "–"}</span>
                      {up?.update_available && <span className="flex items-center gap-[3px] text-[11.5px] text-blue-text"><Icon name="upload" className="text-[11px]" />{up.latest} verfügbar</span>}
                    </span>
                    <span className="min-w-0">{d.pairing_status === "paired" ? <WanPill state={state[d.id]} offline={off} /> : <span className="text-fg3">–</span>}</span>
                    <span className="min-w-0"><VrrpPill state={state[d.id]} /></span>
                    <span>{off ? <span className="text-fg3">–</span> : <CpuBar value={d.facts?.cpu_load as number | undefined} />}</span>
                    <span className="truncate text-fg2">{off ? "–" : fmtUptime(d.uptime)}</span>
                    <span className={cls("whitespace-nowrap", d.status === "offline" ? "text-red-text" : "text-fg2")}>{d.last_seen_at ? fmtAgo(d.last_seen_at) : "nie"}</span>
                  </div>
                );
              })}
              {list.length === 0 && <div className="py-10 text-center text-fg3">{all.length ? "Keine Geräte für diesen Filter" : "Noch keine Geräte – über „Gerät hinzufügen“ oder Zero-Touch anlegen."}</div>}
            </div>
          </div>
        )}
        <div className="flex justify-between border-t border-line px-3.5 py-2.5 text-xs text-fg3">
          <span>{list.length} von {all.length} Geräten</span><span>Live-Aktualisierung bei jedem Polling</span>
        </div>
      </section>
      <AddDeviceModal open={open} onClose={() => setOpen(false)} sites={sites.data ?? []} onCreated={() => void devices.reload()} />
      {dialog && <BulkDialog kind={dialog} devices={selected} onClose={() => setDialog(null)} onDone={() => { setSel(new Set()); void devices.reload(); }} />}
    </>
  );
}

interface Policy { id: string; name: string; scope?: string; version?: number; current_version?: number }

function BulkDialog({ kind, devices, onClose, onDone }: { kind: "policy" | "firmware" | "backup"; devices: Device[]; onClose: () => void; onDone: () => void }) {
  const policies = useFetch<Policy[]>(kind === "policy" ? "/policies" : null);
  const [policy, setPolicy] = useState("");
  const [channel, setChannel] = useState("stable");
  const [batch, setBatch] = useState(5);
  const [result, setResult] = useState<string | null>(null);
  const { busy, error, run } = useAction();
  const paired = devices.filter((d) => d.pairing_status === "paired");
  const ids = paired.map((d) => d.id);
  const title = { policy: "Policy pushen", firmware: "Firmware-Update", backup: "Backup erstellen" }[kind];
  const go = () => void run(async () => {
    if (kind === "policy") {
      await api.post(`/policies/${policy}/assign`, { device_ids: ids });
      await api.post(`/policies/${policy}/deploy`, { device_ids: ids });
      setResult("Policy zugewiesen, Übertragung gestartet – Fortschritt unter Firewall-Policies.");
    } else if (kind === "firmware") {
      const j = await api.post<{ id: string }>("/firmware/jobs", { name: `Update ${ids.length} Geräte`, device_ids: ids, channel, batch_size: batch });
      setResult(`Update-Auftrag angelegt (${j.id.slice(0, 8)}) – Fortschritt unter Firmware.`);
    } else {
      const res = await Promise.allSettled(ids.map((id) => api.post(`/devices/${id}/backups`)));
      const ok = res.filter((r) => r.status === "fulfilled").length;
      setResult(`${ok} von ${ids.length} Backups erstellt${ok < ids.length ? " – nicht erreichbare Geräte übersprungen" : ""}.`);
    }
    onDone();
  });
  return (
    <Modal open onClose={onClose} title={title} subtitle={`${paired.length} ${paired.length === 1 ? "Gerät" : "Geräte"}: ${paired.map((d) => d.name).slice(0, 4).join(", ")}${paired.length > 4 ? " …" : ""}`}
      footer={result ? <Button onClick={onClose}>Schließen</Button> : <>
        <Button variant="secondary" onClick={onClose}>Abbrechen</Button>
        <Button disabled={busy || !ids.length || (kind === "policy" && !policy)} onClick={go}>{busy ? "Läuft …" : title}</Button>
      </>}>
      <ErrorBox error={error} />
      {devices.length > paired.length && <div className="mb-3"><Notice tone="orange">{devices.length - paired.length} nicht verbundene Geräte werden übersprungen.</Notice></div>}
      {result ? <Notice tone="green">{result}</Notice> : (
        <div className="space-y-3">
          {kind === "policy" && (
            <Select label="Policy" value={policy} onChange={(e) => setPolicy(e.target.value)}>
              <option value="">– wählen –</option>
              {policies.data?.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </Select>
          )}
          {kind === "firmware" && <div className="grid grid-cols-2 gap-3">
            <Select label="Kanal" value={channel} onChange={(e) => setChannel(e.target.value)}>
              {["stable", "long-term", "testing"].map((c) => <option key={c}>{c}</option>)}
            </Select>
            <Input label="Geräte pro Welle" type="number" min={1} max={100} value={batch} onChange={(e) => setBatch(Number(e.target.value))} />
            <p className="col-span-2 text-xs text-fg3">Vor jedem Update wird automatisch ein Backup erstellt. Bei Fehlern wird der Auftrag pausiert.</p>
          </div>}
          {kind === "backup" && <p className="text-fg2">Erstellt sofort einen Konfigurations-Export je Gerät (ohne Passwörter/Schlüssel).</p>}
        </div>
      )}
    </Modal>
  );
}

function AddDeviceModal({ open, onClose, sites, onCreated }: { open: boolean; onClose: () => void; sites: Site[]; onCreated: () => void }) {
  const [name, setName] = useState("");
  const [siteId, setSiteId] = useState("");
  const [serial, setSerial] = useState("");
  const [tags, setTags] = useState("");
  const [pairing, setPairing] = useState<PairingInfo | null>(null);
  const { busy, error, run } = useAction();
  const close = () => { setPairing(null); setName(""); setSerial(""); setTags(""); onClose(); };
  const submit = () => void run(async () => {
    const r = await api.post<{ pairing: PairingInfo }>("/devices", {
      name, site_id: siteId || null, serial: serial || null, tags: tags.split(",").map((t) => t.trim()).filter(Boolean),
    });
    setPairing(r.pairing);
    onCreated();
  });
  return (
    <Modal open={open} onClose={close} title={pairing ? "Onboarding-Befehl" : "Gerät hinzufügen"} subtitle={pairing ? undefined : "Router anlegen und per Befehl mit der Cloud verbinden. Für viele Router: Zero-Touch."} wide={!!pairing}
      footer={pairing ? <Button onClick={close}>Fertig</Button> : <>
        <Button type="button" variant="secondary" onClick={close}>Abbrechen</Button>
        <Button disabled={busy || !name} onClick={submit}>Anlegen</Button>
      </>}>
      {pairing ? <PairingBox pairing={pairing} /> : (
        <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); submit(); }}>
          <ErrorBox error={error} />
          <Input label="Name" value={name} onChange={(e) => setName(e.target.value)} required placeholder="z. B. fil-wien-rtr01" />
          <Select label="Standort" value={siteId} onChange={(e) => setSiteId(e.target.value)}>
            <option value="">– kein Standort –</option>
            {sites.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </Select>
          <Input label="Seriennummer" hint="Optional – bindet den Token an die Hardware." value={serial} onChange={(e) => setSerial(e.target.value)} className="font-mono" />
          <Input label="Tags" hint="Kommagetrennt, z. B. Kasse, Kern" value={tags} onChange={(e) => setTags(e.target.value)} />
          <button type="submit" hidden />
        </form>
      )}
    </Modal>
  );
}
