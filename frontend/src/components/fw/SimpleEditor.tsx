import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../lib/api";
import { fmtAgo, fmtBytes } from "../../lib/format";
import {
  ACTION_LABEL, KIND_LABEL, ROUTER, emptySpec, newId, serviceText, useFwCatalog,
  type FwCatalog, type Hit, type LintIssue, type Preview, type Spec, type SpecNat, type SpecRule,
} from "../../lib/fw";
import { useFetch } from "../../lib/useFetch";
import { Icon } from "../Icon";
import { Button, Card, Checkbox, CodeBlock, EmptyState, ErrorBox, IconButton, Input, Loading, Modal, Notice, Pill, Select, ToggleField, cls, useAction, type Tone } from "../ui";

interface PolicyLike { id: string; version: number; spec: Spec | null; undeployed: { device_id: string; name: string; deployed_version: number | null }[]; assignments?: { device_id: string }[] }

const LINT_TONE: Record<LintIssue["level"], Tone> = { error: "red", warn: "orange", info: "blue" };
const ACTION_TONE: Record<SpecRule["action"], Tone> = { accept: "green", drop: "red", reject: "orange" };

function blankRule(): SpecRule {
  return { id: newId(), enabled: true, src_zone: null, src: [], dst_zone: null, dst: [], services: [], action: "accept", log: false, comment: "" };
}

/** Einfache Policy bearbeiten. Speichern erzeugt eine neue Version (kompiliert auf dem Server). */
export default function SimpleEditor({ policy, readOnly, onSaved }: { policy: PolicyLike; readOnly: boolean; onSaved: () => Promise<void> | void }) {
  const cat = useFwCatalog();
  const hits = useFetch<{ rules: Record<string, Hit> }>(`/policies/${policy.id}/hits`);
  const [draft, setDraft] = useState<Spec>(() => structuredClone(policy.spec ?? emptySpec()));
  const [edit, setEdit] = useState<SpecRule | null>(null);
  const [natEdit, setNatEdit] = useState<SpecNat | null>(null);
  const [blocksOpen, setBlocksOpen] = useState(false);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [deployOpen, setDeployOpen] = useState(false);
  const [q, setQ] = useState("");
  const [note, setNote] = useState("");
  const [lint, setLint] = useState<LintIssue[]>([]);
  const { busy, error, run } = useAction();
  const dirty = JSON.stringify(draft) !== JSON.stringify(policy.spec ?? emptySpec());
  useEffect(() => { setDraft(structuredClone(policy.spec ?? emptySpec())); }, [policy.version]); // eslint-disable-line react-hooks/exhaustive-deps
  // Prüfung bei jeder Änderung (verzögert)
  useEffect(() => {
    const t = setTimeout(() => { api.post<Preview>(`/policies/${policy.id}/preview`, { spec: draft }).then((p) => setLint(p.lint)).catch((e: Error) => setLint([{ level: "error", code: "invalid", message: e.message, rule_id: null }])); }, 500);
    return () => clearTimeout(t);
  }, [draft, policy.id]);
  if (!cat.data) return cat.error ? <ErrorBox error={cat.error} /> : <Loading rows={4} />;
  const c = cat.data;
  const set = (patch: Partial<Spec>) => setDraft({ ...draft, ...patch });
  const setRules = (rules: SpecRule[]) => set({ rules });
  const save = () => run(async () => { await api.patch(`/policies/${policy.id}`, { spec: draft, note: note || null }); setNote(""); await onSaved(); });
  const errors = lint.filter((i) => i.level === "error");
  return (
    <div className="flex flex-col gap-4">
      {policy.undeployed.length > 0 && !dirty && (
        <Notice tone="orange" icon="alert" title="Änderungen nicht ausgerollt">
          Version {policy.version} läuft noch nicht auf: {policy.undeployed.map((d) => `${d.name}${d.deployed_version ? ` (v${d.deployed_version})` : " (noch nie)"}`).join(", ")}.
        </Notice>
      )}
      <ErrorBox error={error} />
      <Card title="Optionen" subtitle="Verwaltete Regeln werden oben eingefügt – vor der ersten manuellen Regel des Routers.">
        <div className="flex flex-wrap gap-6">
          <ToggleField label="Grundregeln (bestehende Verbindungen, ungültige verwerfen, Management nur aus Zone Management/Tunnel)" checked={draft.options.baseline} disabled={readOnly}
            onChange={(v) => set({ options: { ...draft.options, baseline: v } })} />
          <ToggleField label="Default-Drop am Ende (input/forward)" checked={draft.options.default_drop} disabled={readOnly}
            onChange={(v) => (v || confirm("Default-Drop abschalten? Dann ist aller nicht ausdrücklich verbotene Verkehr erlaubt.")) && set({ options: { ...draft.options, default_drop: v } })} />
        </div>
        {!draft.options.default_drop && <div className="mt-3"><Notice tone="orange" icon="alert" title="Default-Drop aus">Nicht ausdrücklich verbotener Verkehr ist erlaubt.</Notice></div>}
      </Card>

      <LintBar lint={lint} />

      <Card flush title="Regeln" subtitle={`${draft.rules.length} Regeln · Reihenfolge per Ziehen oder mit Alt+Pfeil`}
        actions={<>
          <input aria-label="Regeln filtern" placeholder="Suchen …" className="h-8 w-44 rounded-md border border-line bg-panel px-2 text-sm" value={q} onChange={(e) => setQ(e.target.value)} />
          {!readOnly && <Button size="sm" variant="secondary" icon="layers" onClick={() => setBlocksOpen(true)}>Baustein</Button>}
          {!readOnly && <Button size="sm" icon="plus" onClick={() => setEdit(blankRule())}>Regel</Button>}
        </>}>
        <BaseRows spec={draft} cat={c} where="top" />
        <RuleTable spec={draft} cat={c} hits={hits.data?.rules ?? {}} q={q} readOnly={readOnly} lint={lint}
          onChange={setRules} onEdit={(r) => setEdit(structuredClone(r))} />
        {draft.raw.filter.length > 0 && <RawRows rows={draft.raw.filter} />}
        <BaseRows spec={draft} cat={c} where="bottom" />
      </Card>

      <NatCard spec={draft} cat={c} readOnly={readOnly} onChange={(nat) => set({ nat })} onEdit={(n) => setNatEdit(structuredClone(n))} />

      {!readOnly && (
        <div className="flex flex-wrap items-end gap-2">
          <div className="min-w-60 flex-1"><Input label="Änderungsnotiz" value={note} onChange={(e) => setNote(e.target.value)} /></div>
          <Button variant="secondary" icon="terminal" onClick={() => setPreviewOpen(true)}>Vorschau</Button>
          <Button variant="secondary" disabled={!dirty} onClick={() => setDraft(structuredClone(policy.spec ?? emptySpec()))}>Verwerfen</Button>
          <Button disabled={!dirty || busy} onClick={() => void save()}>{busy ? "Speichere …" : `Als v${policy.version + 1} speichern`}</Button>
          <Button variant="secondary" icon="upload" disabled={dirty || !policy.assignments?.length} title={dirty ? "Erst speichern" : ""} onClick={() => setDeployOpen(true)}>Ausrollen</Button>
        </div>
      )}
      {errors.length > 0 && !readOnly && <p className="text-xs text-red-text">{errors.length} Fehler in der Prüfung – Ausrollen nur mit ausdrücklicher Bestätigung.</p>}

      {edit && <RuleDialog rule={edit} cat={c} onReload={cat.reload} onClose={() => setEdit(null)}
        onSave={(r) => { const i = draft.rules.findIndex((x) => x.id === r.id); setRules(i >= 0 ? draft.rules.map((x) => (x.id === r.id ? r : x)) : [...draft.rules, r]); setEdit(null); }} />}
      {natEdit && <NatDialog nat={natEdit} cat={c} onReload={cat.reload} onClose={() => setNatEdit(null)}
        onSave={(n) => { const i = draft.nat.findIndex((x) => x.id === n.id); set({ nat: i >= 0 ? draft.nat.map((x) => (x.id === n.id ? n : x)) : [...draft.nat, n] }); setNatEdit(null); }} />}
      {blocksOpen && <BlocksDialog cat={c} onClose={() => setBlocksOpen(false)}
        onInsert={(rules, nat) => { set({ rules: [...draft.rules, ...rules], nat: [...draft.nat, ...nat] }); setBlocksOpen(false); }} />}
      {previewOpen && <PreviewDialog policyId={policy.id} spec={draft} onClose={() => setPreviewOpen(false)} />}
      {deployOpen && <DeployDialog policyId={policy.id} onClose={() => setDeployOpen(false)} onDone={onSaved} />}
    </div>
  );
}

function LintBar({ lint }: { lint: LintIssue[] }) {
  if (!lint.length) return <Notice tone="green" icon="checkCircle" title="Prüfung ohne Befund" />;
  const order = { error: 0, warn: 1, info: 2 };
  return (
    <Card flush title="Prüfung" subtitle={`${lint.filter((i) => i.level === "error").length} Fehler · ${lint.filter((i) => i.level === "warn").length} Warnungen`}>
      {[...lint].sort((a, b) => order[a.level] - order[b.level]).map((i, n) => (
        <div key={n} className="flex items-start gap-3 border-b border-line px-4 py-2 last:border-b-0">
          <Pill tone={LINT_TONE[i.level]} icon={i.level === "error" ? "xCircle" : i.level === "warn" ? "alert" : "info"}>{i.level === "error" ? "Fehler" : i.level === "warn" ? "Warnung" : "Hinweis"}</Pill>
          <span className="text-fg2">{i.message}</span>
        </div>
      ))}
    </Card>
  );
}

function names(ids: string[], pool: { id: string; name: string }[]): string {
  return ids.map((i) => pool.find((x) => x.id === i)?.name ?? "?").join(", ");
}

function zoneName(id: string | null, c: FwCatalog, side: "src" | "dst"): string {
  if (!id) return "beliebig";
  if (id === ROUTER) return side === "dst" ? "Router selbst" : "Router";
  return c.zones.find((z) => z.id === id)?.name ?? "?";
}

/** Grundregeln/Default-Drop: sichtbar, grau, nicht editierbar. */
function BaseRows({ spec, cat, where }: { spec: Spec; cat: FwCatalog; where: "top" | "bottom" }) {
  const rows: string[] = [];
  if (where === "top" && spec.options.baseline) {
    rows.push("Plattform-Zugang: Hub über den Management-Tunnel, VPN-Mesh, VRRP, Ping – immer erlaubt");
    rows.push("Bestehende/zugehörige Verbindungen erlauben, ungültige verwerfen (input/forward)");
    const mz = cat.zones.filter((z) => z.management).map((z) => z.name).join(", ");
    rows.push(`Verwaltung (WinBox/SSH/API/Web) nur aus ${mz || "–"} und dem Tunnel`);
  }
  if (where === "bottom" && spec.options.default_drop) rows.push("Default-Drop: alles andere verwerfen (input und forward)");
  return <>{rows.map((r) => (
    <div key={r} className="flex items-center gap-3 border-b border-line bg-panel2 px-4 py-2 text-fg3">
      <Icon name="lock" className="text-[13px]" /><span className="text-[12.5px]">{r}</span><span className="ml-auto text-[11px]">Grundregel</span>
    </div>
  ))}</>;
}

function RawRows({ rows }: { rows: Record<string, string>[] }) {
  return (
    <details className="border-b border-line">
      <summary className="cursor-pointer px-4 py-2 text-fg2">{rows.length} Rohregeln aus dem Expertenmodus (unverändert wirksam)</summary>
      <div className="px-4 pb-3"><CodeBlock highlight={false} text={rows.map((r) => Object.entries(r).map(([k, v]) => `${k}=${v}`).join(" ")).join("\n")} /></div>
    </details>
  );
}

function RuleTable({ spec, cat, hits, q, readOnly, lint, onChange, onEdit }: {
  spec: Spec; cat: FwCatalog; hits: Record<string, Hit>; q: string; readOnly: boolean; lint: LintIssue[];
  onChange: (r: SpecRule[]) => void; onEdit: (r: SpecRule) => void;
}) {
  const drag = useRef<number | null>(null);
  const [over, setOver] = useState<number | null>(null);
  const move = (from: number, to: number) => {
    if (to < 0 || to >= spec.rules.length || from === to) return;
    const n = [...spec.rules]; const [x] = n.splice(from, 1); n.splice(to, 0, x); onChange(n);
  };
  const text = (r: SpecRule) => [r.comment, zoneName(r.src_zone, cat, "src"), zoneName(r.dst_zone, cat, "dst"), names(r.src, cat.objects), names(r.dst, cat.objects), names(r.services, cat.services)].join(" ").toLowerCase();
  const visible = spec.rules.map((r, i) => [r, i] as const).filter(([r]) => !q || text(r).includes(q.toLowerCase()));
  if (!spec.rules.length) return <EmptyState compact title="Noch keine Regeln" text="Regel anlegen oder einen Baustein einfügen." />;
  return (
    <div className="overflow-x-auto">
      <div className="min-w-[980px]">
        <div className="grid grid-cols-[28px_32px_minmax(0,1.2fr)_minmax(0,1.2fr)_minmax(0,1fr)_100px_120px_130px] gap-3 border-b border-line bg-panel2 px-4 py-2 text-xs font-medium text-fg3">
          <span /><span>#</span><span>Quelle</span><span>Ziel</span><span>Dienst</span><span>Aktion</span><span>Treffer</span><span />
        </div>
        {visible.map(([r, i]) => {
          const h = hits[r.id];
          const issue = lint.find((x) => x.rule_id === r.id);
          const stale = h && (h.last_hit_at ? Date.now() - new Date(h.last_hit_at).getTime() : Date.now() - new Date(h.tracking_since).getTime()) > 7 * 86400e3;
          return (
            <div key={r.id} draggable={!readOnly} tabIndex={0} aria-label={`Regel ${i + 1}: ${r.comment || ACTION_LABEL[r.action]}`}
              onDragStart={() => { drag.current = i; }} onDragOver={(e) => { e.preventDefault(); setOver(i); }} onDragLeave={() => setOver(null)}
              onDrop={() => { if (drag.current !== null) move(drag.current, i); drag.current = null; setOver(null); }}
              onKeyDown={(e) => { if (readOnly || !e.altKey) return; if (e.key === "ArrowUp") { e.preventDefault(); move(i, i - 1); } if (e.key === "ArrowDown") { e.preventDefault(); move(i, i + 1); } }}
              className={cls("grid min-h-11 grid-cols-[28px_32px_minmax(0,1.2fr)_minmax(0,1.2fr)_minmax(0,1fr)_100px_120px_130px] items-center gap-3 border-b border-line px-4 py-1.5 focus:outline-2 focus:outline-blue",
                !r.enabled && "opacity-55", over === i && "bg-blue-bg")}>
              <span className={cls("text-fg3", !readOnly && "cursor-grab")} aria-hidden><Icon name="grip" /></span>
              <span className="text-xs text-fg3">{i + 1}</span>
              <span className="min-w-0"><span className="block truncate">{zoneName(r.src_zone, cat, "src")}</span>{r.src.length > 0 && <span className="block truncate text-xs text-fg2">{names(r.src, cat.objects)}</span>}</span>
              <span className="min-w-0"><span className="block truncate">{zoneName(r.dst_zone, cat, "dst")}</span>{r.dst.length > 0 && <span className="block truncate text-xs text-fg2">{names(r.dst, cat.objects)}</span>}</span>
              <span className="min-w-0 truncate text-fg2">{r.services.length ? names(r.services, cat.services) : "alle"}{r.comment && <span className="block truncate text-xs text-fg3">{r.comment}</span>}</span>
              <span className="flex items-center gap-1"><Pill tone={ACTION_TONE[r.action]}>{ACTION_LABEL[r.action]}</Pill>{r.log && <Icon name="file" className="text-[12px] text-fg3" title="Logging" />}</span>
              <span className="text-xs">{h ? <span title={`${fmtBytes(h.bytes)} · ${h.devices} Geräte`} className={stale ? "text-orange-text" : "text-fg2"}>
                {h.packets.toLocaleString("de-DE")} Pakete{stale && <span className="block">0 Treffer seit {fmtAgo(h.last_hit_at ?? h.tracking_since)}</span>}</span> : <span className="text-fg3">–</span>}
                {issue && <span className="block text-orange-text" title={issue.message}>{issue.level === "error" ? "Fehler" : "Hinweis"}</span>}</span>
              <span className="flex justify-end gap-0.5">
                {!readOnly && <>
                  <IconButton icon="arrowUp" label="Nach oben" disabled={i === 0} onClick={() => move(i, i - 1)} />
                  <IconButton icon="arrowDown" label="Nach unten" disabled={i === spec.rules.length - 1} onClick={() => move(i, i + 1)} />
                  <IconButton icon="edit" label="Bearbeiten" onClick={() => onEdit(r)} />
                  <IconButton icon="trash" label="Löschen" onClick={() => onChange(spec.rules.filter((x) => x.id !== r.id))} />
                </>}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function MultiPick({ label, options, value, onChange }: { label: string; options: { id: string; name: string; sub?: string }[]; value: string[]; onChange: (v: string[]) => void }) {
  const [q, setQ] = useState("");
  const shown = options.filter((o) => !q || o.name.toLowerCase().includes(q.toLowerCase()));
  return (
    <fieldset className="flex flex-col gap-1.5">
      <legend className="mb-1 text-xs font-medium text-fg2">{label}{value.length > 0 && <span className="text-fg3"> · {value.length} gewählt</span>}</legend>
      <input aria-label={`${label} filtern`} placeholder="Filtern …" className="h-8 rounded-md border border-line bg-panel px-2 text-sm" value={q} onChange={(e) => setQ(e.target.value)} />
      <div className="max-h-40 overflow-y-auto rounded-md border border-line px-2 py-1">
        {shown.length === 0 && <p className="py-1 text-xs text-fg3">Keine Einträge</p>}
        {shown.map((o) => <Checkbox key={o.id} label={<>{o.name}{o.sub && <span className="ml-1 text-xs text-fg3">{o.sub}</span>}</>} checked={value.includes(o.id)}
          onChange={(c) => onChange(c ? [...value, o.id] : value.filter((x) => x !== o.id))} />)}
      </div>
    </fieldset>
  );
}

/** Objekt/Dienst direkt im Regeldialog anlegen. */
function InlineCreate({ kind, onCreated }: { kind: "objects" | "services"; onCreated: (id: string) => void }) {
  const [open, setOpen] = useState(false);
  const [f, setF] = useState({ name: "", type: "host", values: "", protocol: "tcp", ports: "" });
  const { busy, error, run } = useAction();
  if (!open) return <Button size="sm" variant="ghost" icon="plus" onClick={() => setOpen(true)}>{kind === "objects" ? "Neues Objekt" : "Neuer Dienst"}</Button>;
  return (
    <div className="flex flex-col gap-2 rounded-md border border-line p-2">
      <ErrorBox error={error} />
      <Input label="Name" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />
      {kind === "objects" ? <>
        <Select label="Typ" value={f.type} onChange={(e) => setF({ ...f, type: e.target.value })}>
          <option value="host">Host</option><option value="network">Netz (CIDR)</option><option value="range">Bereich (von-bis)</option>
        </Select>
        <Input label="Adressen" hint="kommagetrennt" className="font-mono" value={f.values} onChange={(e) => setF({ ...f, values: e.target.value })} />
      </> : <>
        <Select label="Protokoll" value={f.protocol} onChange={(e) => setF({ ...f, protocol: e.target.value })}>{["tcp", "udp", "icmp"].map((p) => <option key={p}>{p}</option>)}</Select>
        <Input label="Ports" hint="z. B. 80,443 oder 5000-5100" className="font-mono" value={f.ports} onChange={(e) => setF({ ...f, ports: e.target.value })} />
      </>}
      <div className="flex gap-2">
        <Button size="sm" disabled={busy || !f.name} onClick={() => void run(async () => {
          const body = kind === "objects" ? { name: f.name, kind: f.type, values: f.values.split(",").map((v) => v.trim()).filter(Boolean) } : { name: f.name, entries: [{ protocol: f.protocol, ports: f.ports }] };
          const r = await api.post<{ id: string }>(`/fw/${kind}`, body);
          onCreated(r.id); setOpen(false); setF({ name: "", type: "host", values: "", protocol: "tcp", ports: "" });
        })}>Anlegen</Button>
        <Button size="sm" variant="secondary" onClick={() => setOpen(false)}>Abbrechen</Button>
      </div>
    </div>
  );
}

function RuleDialog({ rule, cat, onSave, onClose, onReload }: { rule: SpecRule; cat: FwCatalog; onSave: (r: SpecRule) => void; onClose: () => void; onReload: () => Promise<void> }) {
  const [r, setR] = useState(rule);
  const objs = cat.objects.map((o) => ({ id: o.id, name: o.name, sub: KIND_LABEL[o.kind] }));
  const svcs = cat.services.map((s) => ({ id: s.id, name: s.name, sub: serviceText(s, cat.services) }));
  const created = (k: "src" | "dst" | "services") => (id: string) => { void onReload(); setR((x) => ({ ...x, [k]: [...x[k], id] })); };
  return (
    <Modal open onClose={onClose} title="Regel" size="xl"
      footer={<><Button variant="secondary" onClick={onClose}>Abbrechen</Button><Button onClick={() => onSave(r)}>Übernehmen</Button></>}>
      <div className="grid gap-4 md:grid-cols-2">
        <div className="flex flex-col gap-3">
          <Select label="Quelle – Zone" value={r.src_zone ?? ""} onChange={(e) => setR({ ...r, src_zone: e.target.value || null })}>
            <option value="">beliebig</option>{cat.zones.map((z) => <option key={z.id} value={z.id}>{z.name}</option>)}
          </Select>
          <MultiPick label="Quelle – Objekte (leer = alle)" options={objs} value={r.src} onChange={(v) => setR({ ...r, src: v })} />
          <InlineCreate kind="objects" onCreated={created("src")} />
        </div>
        <div className="flex flex-col gap-3">
          <Select label="Ziel – Zone" value={r.dst_zone ?? ""} onChange={(e) => setR({ ...r, dst_zone: e.target.value || null, dst: e.target.value === ROUTER ? [] : r.dst })}>
            <option value="">beliebig</option><option value={ROUTER}>Router selbst</option>{cat.zones.map((z) => <option key={z.id} value={z.id}>{z.name}</option>)}
          </Select>
          {r.dst_zone !== ROUTER && <><MultiPick label="Ziel – Objekte (leer = alle)" options={objs} value={r.dst} onChange={(v) => setR({ ...r, dst: v })} />
            <InlineCreate kind="objects" onCreated={created("dst")} /></>}
        </div>
        <div className="flex flex-col gap-3">
          <MultiPick label="Dienste (leer = alle)" options={svcs} value={r.services} onChange={(v) => setR({ ...r, services: v })} />
          <InlineCreate kind="services" onCreated={created("services")} />
        </div>
        <div className="flex flex-col gap-3">
          <Select label="Aktion" value={r.action} onChange={(e) => setR({ ...r, action: e.target.value as SpecRule["action"] })}>
            {(["accept", "drop", "reject"] as const).map((a) => <option key={a} value={a}>{ACTION_LABEL[a]}</option>)}
          </Select>
          <Input label="Kommentar" maxLength={120} value={r.comment} onChange={(e) => setR({ ...r, comment: e.target.value })} />
          <ToggleField label="Logging" checked={r.log} onChange={(v) => setR({ ...r, log: v })} />
          <ToggleField label="Aktiv" checked={r.enabled} onChange={(v) => setR({ ...r, enabled: v })} />
        </div>
      </div>
    </Modal>
  );
}

function NatCard({ spec, cat, readOnly, onChange, onEdit }: { spec: Spec; cat: FwCatalog; readOnly: boolean; onChange: (n: SpecNat[]) => void; onEdit: (n: SpecNat) => void }) {
  const wan = cat.zones.find((z) => z.source === "wan")?.id ?? null;
  const text = (n: SpecNat) => n.type === "masquerade" ? `Masquerade über ${zoneName(n.zone ?? null, cat, "dst")}`
    : n.type === "portforward" ? `Portweiterleitung ${n.protocol?.toUpperCase()} ${n.ext_port} → ${names([n.host ?? ""], cat.objects)}${n.int_port ? `:${n.int_port}` : ""} (aus ${zoneName(n.in_zone ?? null, cat, "src")})`
      : `Umleitung ${n.protocol?.toUpperCase()} ${n.port} auf den Router (aus ${zoneName(n.in_zone ?? null, cat, "src")})`;
  return (
    <Card flush title="NAT" subtitle="Masquerade je WAN-Zone, Portweiterleitungen" actions={!readOnly && <>
      <Button size="sm" variant="secondary" icon="plus" onClick={() => onChange([...spec.nat, { id: newId(), type: "masquerade", enabled: true, zone: wan }])}>Masquerade</Button>
      <Button size="sm" variant="secondary" icon="plus" onClick={() => onEdit({ id: newId(), type: "portforward", enabled: true, in_zone: wan, protocol: "tcp", ext_port: "", host: "", int_port: "" })}>Portweiterleitung</Button>
    </>}>
      {spec.nat.length === 0 && spec.raw.nat.length === 0 ? <EmptyState compact title="Keine NAT-Regeln" /> : spec.nat.map((n) => (
        <div key={n.id} className={cls("flex items-center gap-3 border-b border-line px-4 py-2 last:border-b-0", !n.enabled && "opacity-55")}>
          <span className="flex-1">{text(n)}</span>
          {!readOnly && <>
            {n.type !== "masquerade" && <IconButton icon="edit" label="Bearbeiten" onClick={() => onEdit(n)} />}
            <IconButton icon="trash" label="Löschen" onClick={() => onChange(spec.nat.filter((x) => x.id !== n.id))} />
          </>}
        </div>
      ))}
      {spec.raw.nat.length > 0 && <RawRows rows={spec.raw.nat} />}
    </Card>
  );
}

function NatDialog({ nat, cat, onSave, onClose, onReload }: { nat: SpecNat; cat: FwCatalog; onSave: (n: SpecNat) => void; onClose: () => void; onReload: () => Promise<void> }) {
  const [n, setN] = useState(nat);
  const hosts = cat.objects.filter((o) => o.kind === "host");
  return (
    <Modal open onClose={onClose} title={n.type === "portforward" ? "Portweiterleitung (Assistent)" : "Umleitung"}
      footer={<><Button variant="secondary" onClick={onClose}>Abbrechen</Button><Button disabled={n.type === "portforward" && (!n.ext_port || !n.host)} onClick={() => onSave(n)}>Übernehmen</Button></>}>
      <div className="grid gap-3 sm:grid-cols-2">
        <Select label="Eingehend aus Zone" value={n.in_zone ?? ""} onChange={(e) => setN({ ...n, in_zone: e.target.value || null })}>
          <option value="">beliebig</option>{cat.zones.map((z) => <option key={z.id} value={z.id}>{z.name}</option>)}
        </Select>
        <Select label="Protokoll" value={n.protocol} onChange={(e) => setN({ ...n, protocol: e.target.value })}><option>tcp</option><option>udp</option></Select>
        {n.type === "portforward" ? <>
          <Input label="1. Externer Port" className="font-mono" value={n.ext_port ?? ""} onChange={(e) => setN({ ...n, ext_port: e.target.value })} />
          <Select label="2. Interner Host" value={n.host ?? ""} onChange={(e) => setN({ ...n, host: e.target.value })}>
            <option value="">– wählen –</option>{hosts.map((o) => <option key={o.id} value={o.id}>{o.name} ({o.values[0]})</option>)}
          </Select>
          <Input label="3. Interner Port" hint="leer = gleicher Port" className="font-mono" value={n.int_port ?? ""} onChange={(e) => setN({ ...n, int_port: e.target.value })} />
          <div className="self-end"><InlineCreate kind="objects" onCreated={(id) => { void onReload(); setN((x) => ({ ...x, host: id })); }} /></div>
          <p className="text-xs text-fg3 sm:col-span-2">Zusätzlich ist eine erlaubende Forward-Regel zum Host nötig – die Prüfung weist darauf hin.</p>
        </> : <Input label="Port" className="font-mono" value={n.port ?? ""} onChange={(e) => setN({ ...n, port: e.target.value })} />}
        <Input label="Kommentar" value={n.comment ?? ""} onChange={(e) => setN({ ...n, comment: e.target.value })} />
      </div>
    </Modal>
  );
}

function BlocksDialog({ cat, onInsert, onClose }: { cat: FwCatalog; onInsert: (r: SpecRule[], n: SpecNat[]) => void; onClose: () => void }) {
  const [sel, setSel] = useState<string | null>(null);
  const [params, setParams] = useState<Record<string, string | string[]>>({});
  const { busy, error, run } = useAction();
  const b = cat.blocks.find((x) => x.id === sel);
  return (
    <Modal open onClose={onClose} title="Baustein einfügen" size="lg"
      footer={<><Button variant="secondary" onClick={onClose}>Abbrechen</Button><Button disabled={!b || busy} onClick={() => void run(async () => {
        const r = await api.post<{ rules: SpecRule[]; nat: SpecNat[] }>(`/fw/blocks/${b!.id}/expand`, { params });
        onInsert(r.rules, r.nat);
      })}>Einfügen</Button></>}>
      <ErrorBox error={error} />
      <div className="flex flex-col gap-2">
        {cat.blocks.map((x) => (
          <label key={x.id} className={cls("flex cursor-pointer flex-col gap-0.5 rounded-md border px-3 py-2", sel === x.id ? "border-blue bg-blue-bg" : "border-line hover:bg-hover")}>
            <span className="flex items-center gap-2"><input type="radio" name="block" checked={sel === x.id} onChange={() => { setSel(x.id); setParams({}); }} /><b>{x.name}</b>{x.scope === "tenant" && <Pill>Mandant</Pill>}</span>
            {x.description && <span className="pl-6 text-xs text-fg2">{x.description}</span>}
          </label>
        ))}
      </div>
      {b && b.params.length > 0 && (
        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          {b.params.map((p) => p.type === "zone" ? (
            <Select key={p.key} label={p.label} value={(params[p.key] as string) ?? ""} onChange={(e) => setParams({ ...params, [p.key]: e.target.value })}>
              <option value="">{p.default ? "Vorgabe" : "– wählen –"}</option>{cat.zones.map((z) => <option key={z.id} value={z.id}>{z.name}</option>)}
            </Select>
          ) : (
            <MultiPick key={p.key} label={p.label} options={(p.type === "object" ? cat.objects : cat.services).map((o) => ({ id: o.id, name: o.name }))}
              value={(params[p.key] as string[]) ?? []} onChange={(v) => setParams({ ...params, [p.key]: v })} />
          ))}
        </div>
      )}
    </Modal>
  );
}

function PreviewDialog({ policyId, spec, onClose }: { policyId: string; spec: Spec; onClose: () => void }) {
  const [p, setP] = useState<Preview | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [view, setView] = useState<"commands" | "diff">("commands");
  useEffect(() => { api.post<Preview>(`/policies/${policyId}/preview`, { spec }).then(setP).catch((e: Error) => setErr(e.message)); }, [policyId, spec]);
  const diffLines = useMemo(() => p?.diff.lines.filter((l) => !l.startsWith("---") && !l.startsWith("+++")) ?? [], [p]);
  return (
    <Modal open onClose={onClose} title="Vorschau" subtitle={p?.placement} size="xl" footer={<Button onClick={onClose}>Schließen</Button>}>
      <ErrorBox error={err} />
      {!p ? !err && <Loading rows={4} /> : <>
        <div className="mb-3 flex gap-2">
          <Button size="sm" variant={view === "commands" ? "primary" : "secondary"} onClick={() => setView("commands")}>RouterOS-Befehle</Button>
          <Button size="sm" variant={view === "diff" ? "primary" : "secondary"} onClick={() => setView("diff")}>
            Diff zu {p.deployed_version ? `ausgerollter v${p.deployed_version}` : "leer (noch nie ausgerollt)"} (+{p.diff.added}/−{p.diff.removed})
          </Button>
        </div>
        {view === "commands" ? <CodeBlock highlight={false} text={p.commands.join("\n")} /> : (
          <pre className="max-h-[60vh] overflow-auto rounded-md bg-code p-3 font-mono text-xs leading-[1.6]">
            {diffLines.length === 0 ? "Keine Änderungen" : diffLines.map((l, i) => (
              <div key={i} className={l.startsWith("+") ? "bg-green-bg text-green-text" : l.startsWith("-") ? "bg-red-bg text-red-text" : "text-fg2"}>{l}</div>
            ))}
          </pre>
        )}
      </>}
    </Modal>
  );
}

interface FwRule { chain: string; action: string; comment: string; summary: string }
interface CheckDevice { device_id: string; name: string; reachable: boolean | null; error?: string; unmanaged: FwRule[]; defconf?: FwRule[] }

/** Ausrollen: manuelle Regeln hinter dem Default-Drop je Gerät bestätigen, Lint-Fehler bestätigen. */
function DeployDialog({ policyId, onClose, onDone }: { policyId: string; onClose: () => void; onDone: () => Promise<void> | void }) {
  const [chk, setChk] = useState<{ default_drop: boolean; lint: LintIssue[]; devices: CheckDevice[] } | null>(null);
  const [confirmed, setConfirmed] = useState<string[]>([]);
  const [lintOk, setLintOk] = useState(false);
  const [defconfOff, setDefconfOff] = useState(true);
  const [result, setResult] = useState<{ skipped: { name: string; reason: string }[] } | null>(null);
  const { busy, error, run } = useAction();
  useEffect(() => { void run(async () => setChk(await api.post(`/policies/${policyId}/deploy-check`, {}))); }, [policyId]); // eslint-disable-line react-hooks/exhaustive-deps
  const errors = chk?.lint.filter((i) => i.level === "error") ?? [];
  return (
    <Modal open onClose={onClose} title="Policy ausrollen" size="lg"
      footer={result ? <Button onClick={onClose}>Schließen</Button> : <>
        <Button variant="secondary" onClick={onClose}>Abbrechen</Button>
        <Button icon="upload" disabled={!chk || busy || (errors.length > 0 && !lintOk)} onClick={() => void run(async () => {
          setResult(await api.post(`/policies/${policyId}/deploy`, { confirm_devices: confirmed, confirm_lint: lintOk, disable_defconf: chk?.default_drop ? defconfOff : false }));
          await onDone();
        })}>{busy ? "Starte …" : "Ausrollen"}</Button>
      </>}>
      <ErrorBox error={error} />
      {!chk ? <Loading rows={3} /> : result ? (
        <Notice tone={result.skipped.length ? "orange" : "green"} title="Push gestartet">
          {result.skipped.length ? <>Übersprungen: {result.skipped.map((s) => s.name).join(", ")}</> : "Alle Geräte werden aktualisiert."}
        </Notice>
      ) : (
        <div className="flex flex-col gap-3">
          <p className="text-fg2">Verwaltete Regeln werden oben eingefügt – vor der ersten manuellen Regel des Routers.</p>
          {errors.length > 0 && (
            <Notice tone="red" icon="xCircle" title={`${errors.length} Fehler in der Prüfung`}>
              <ul className="list-disc pl-4">{errors.map((e, i) => <li key={i}>{e.message}</li>)}</ul>
              <div className="mt-2"><Checkbox label="Trotzdem ausrollen – Auswirkungen verstanden" checked={lintOk} onChange={setLintOk} /></div>
            </Notice>
          )}
          {chk.default_drop && chk.devices.some((d) => d.defconf?.length) && (
            <div className="rounded-md border border-line bg-panel2 px-3 py-2">
              <Checkbox label="defconf-Regeln deaktivieren (disabled=yes, nicht löschen – rückgängig beim Entfernen der Policy oder per Button im Firewall-Tab)" checked={defconfOff} onChange={setDefconfOff} />
            </div>
          )}
          {chk.devices.map((d) => { const blocking = [...d.unmanaged, ...(defconfOff ? [] : d.defconf ?? [])]; return (
            <div key={d.device_id} className="rounded-md border border-line px-3 py-2">
              <div className="flex items-center gap-2"><Link to={`/devices/${d.device_id}`} className="font-medium hover:underline">{d.name}</Link>
                {d.reachable === false && <Pill tone="red">nicht erreichbar</Pill>}
                {blocking.length === 0 ? <Pill tone="green" icon="checkCircle">bereit</Pill> : <Pill tone="orange" icon="alert">{blocking.length} manuelle Regeln</Pill>}</div>
              {(d.defconf?.length ?? 0) > 0 && <>
                <p className="mt-1 text-xs text-fg2"><b>Werks-Firewall (defconf)</b> – wird durch die Grundregeln der Plattform abgedeckt{defconfOff ? "; wird deaktiviert" : "; bleibt aktiv, liegt aber hinter dem Default-Drop"}:</p>
                <ul className="mt-1 max-h-24 overflow-y-auto font-mono text-[11.5px] text-fg3">{d.defconf!.map((u, i) => <li key={i}>{u.chain} {u.action} {u.summary} # {u.comment}</li>)}</ul>
              </>}
              {blocking.length > 0 && <>
                <p className="mt-1 text-xs text-orange-text">Diese {blocking.length} Regeln würden hinter dem Default-Drop nie mehr greifen:</p>
                <ul className="mt-1 max-h-32 overflow-y-auto font-mono text-[11.5px] text-fg2">{blocking.map((u, i) => <li key={i}>{u.chain} {u.action} {u.summary} {u.comment && `# ${u.comment}`}</li>)}</ul>
                <div className="mt-2"><Checkbox label="Für dieses Gerät trotzdem ausrollen (sonst wird es übersprungen)" checked={confirmed.includes(d.device_id)}
                  onChange={(c) => setConfirmed(c ? [...confirmed, d.device_id] : confirmed.filter((x) => x !== d.device_id))} /></div>
              </>}
            </div>
          ); })}
        </div>
      )}
    </Modal>
  );
}
