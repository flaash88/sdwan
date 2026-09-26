import { useState } from "react";
import { Link } from "react-router-dom";
import { Button, Card, Checkbox, EmptyState, ErrorBox, IconButton, Input, Loading, Modal, PageHeader, Pill, Segment, Select, Textarea, ToggleField, useAction } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { KIND_LABEL, serviceText, useFwCatalog, type FwBlock, type FwCatalog, type FwObject, type FwService, type FwZone } from "../lib/fw";

type Kind = "objects" | "services" | "zones" | "blocks";
const TITLE: Record<Kind, string> = { objects: "Netzwerk-Objekte", services: "Dienste", zones: "Zonen", blocks: "Bausteine" };
type Item = FwObject | FwService | FwZone | FwBlock;

/** Objekte, Dienste, Zonen und Bausteine des Firewall-Editors (global = MSP, sonst Mandant). */
export default function FwObjects() {
  const cat = useFwCatalog();
  const { can } = useAuth();
  const [kind, setKind] = useState<Kind>("objects");
  const [edit, setEdit] = useState<{ kind: Kind; item: Partial<Item> } | null>(null);
  const { error, run } = useAction();
  const c = cat.data;
  const items: Item[] = c ? c[kind] : [];
  const canWrite = kind === "blocks" ? can("admin") : can("technician");
  const detail = (x: Item): string => {
    if (kind === "objects") { const o = x as FwObject; return o.kind === "group" ? `Gruppe: ${o.members.map((m) => c?.objects.find((y) => y.id === m)?.name ?? "?").join(", ")}` : o.values.join(", ") || "–"; }
    if (kind === "services") return serviceText(x as FwService, c?.services ?? []);
    if (kind === "zones") { const z = x as FwZone; return z.source === "wan" ? "folgt der WAN-Konfiguration (sdwan-wan)" : `Interface-List sdwan-zone-${z.slug}${z.management ? " · Management-Zugriff" : ""}`; }
    return (x as FwBlock).description ?? "";
  };
  return (
    <>
      <PageHeader title="Objekte, Dienste, Zonen" subtitle={<><Link to="/policies" className="hover:underline">Firewall-Policies</Link> · Bausteine für den einfachen Modus</>}
        actions={canWrite && <Button icon="plus" onClick={() => setEdit({ kind, item: {} })}>{TITLE[kind].replace(/e$/, "")} anlegen</Button>} />
      <div className="mb-4"><Segment label="Art" value={kind} onChange={setKind} options={(Object.keys(TITLE) as Kind[]).map((k) => ({ value: k, label: TITLE[k], count: c?.[k].length }))} /></div>
      <ErrorBox error={error ?? cat.error} />
      <Card flush>
        {!c ? <Loading rows={4} /> : items.length === 0 ? <EmptyState compact title="Keine Einträge" /> : items.map((x) => (
          <div key={x.id} className="flex items-center gap-3 border-b border-line px-4 py-2.5 last:border-b-0">
            <div className="flex min-w-0 flex-1 flex-col">
              <span className="flex items-center gap-2 font-medium">{x.name}
                {kind === "objects" && <Pill>{KIND_LABEL[(x as FwObject).kind]}</Pill>}
                {x.builtin ? <Pill tone="gray">vordefiniert</Pill> : x.scope === "global" ? <Pill tone="blue">global</Pill> : <Pill>Mandant</Pill>}</span>
              <span className="truncate text-xs text-fg2">{detail(x)}</span>
            </div>
            {can("technician") && (kind !== "blocks" || can("admin")) && <IconButton icon="copy" label="Kopieren" onClick={() => void run(async () => { await api.post(`/fw/${kind}/${x.id}/copy`); await cat.reload(); })} />}
            {canWrite && !x.builtin && <>
              <IconButton icon="edit" label="Bearbeiten" onClick={() => setEdit({ kind, item: structuredClone(x) })} />
              <IconButton icon="trash" label="Löschen" onClick={() => confirm(`${x.name} löschen?`) && void run(async () => { await api.del(`/fw/${kind}/${x.id}`); await cat.reload(); })} />
            </>}
          </div>
        ))}
      </Card>
      {edit && c && <EditDialog kind={edit.kind} item={edit.item} cat={c} onClose={() => setEdit(null)} onSaved={async () => { setEdit(null); await cat.reload(); }} />}
    </>
  );
}

function EditDialog({ kind, item, cat, onClose, onSaved }: { kind: Kind; item: Partial<Item>; cat: FwCatalog; onClose: () => void; onSaved: () => Promise<void> }) {
  const o = item as Partial<FwObject & FwService & FwZone & FwBlock>;
  const [f, setF] = useState({
    name: o.name ?? "", description: o.description ?? "", kind: o.kind ?? "host", values: (o.values ?? []).join(", "), members: o.members ?? [],
    entries: o.entries?.length ? o.entries : [{ protocol: "tcp", ports: "" }], source: o.source ?? "manual", management: o.management ?? false,
    json: JSON.stringify({ params: o.params ?? [], rules: o.rules ?? [], nat: o.nat ?? [] }, null, 2),
  });
  const { busy, error, run } = useAction();
  const body = () => {
    const base = { name: f.name, description: f.description || null };
    if (kind === "objects") return { ...base, kind: f.kind, values: f.kind === "group" ? [] : f.values.split(",").map((v) => v.trim()).filter(Boolean), members: f.kind === "group" ? f.members : [] };
    if (kind === "services") return { ...base, entries: f.entries.filter((e) => e.protocol || e.ports), members: f.members };
    if (kind === "zones") return { ...base, source: f.source, management: f.management };
    return { ...base, ...JSON.parse(f.json) };
  };
  return (
    <Modal open onClose={onClose} title={`${TITLE[kind]}: ${o.id ? "bearbeiten" : "anlegen"}`} size="lg"
      footer={<><Button variant="secondary" onClick={onClose}>Abbrechen</Button><Button disabled={busy || !f.name} onClick={() => void run(async () => {
        const r: { recompiled_policies?: string[] } = o.id ? await api.patch(`/fw/${kind}/${o.id}`, body()) : await api.post(`/fw/${kind}`, body());
        if (r.recompiled_policies?.length) alert(`Neu kompiliert (neue Version, noch nicht ausgerollt): ${r.recompiled_policies.join(", ")}`);
        await onSaved();
      })}>Speichern</Button></>}>
      <ErrorBox error={error} />
      <div className="flex flex-col gap-3">
        <Input label="Name" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />
        <Input label="Beschreibung" value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} />
        {kind === "objects" && <>
          <Select label="Typ" value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value as FwObject["kind"] })}>
            <option value="host">Host (eine Adresse)</option><option value="network">Netz (CIDR)</option><option value="range">Bereich (von-bis)</option><option value="group">Gruppe aus Objekten</option>
          </Select>
          {f.kind === "group" ? (
            <div className="max-h-56 overflow-y-auto rounded-md border border-line px-2 py-1">
              {cat.objects.filter((x) => x.id !== o.id && x.kind !== "feed").map((x) => <Checkbox key={x.id} label={`${x.name} (${KIND_LABEL[x.kind]})`} checked={f.members.includes(x.id)}
                onChange={(c) => setF({ ...f, members: c ? [...f.members, x.id] : f.members.filter((m) => m !== x.id) })} />)}
            </div>
          ) : <Input label="Adressen" hint={f.kind === "range" ? "z. B. 192.0.2.10-192.0.2.20" : "kommagetrennt"} className="font-mono" value={f.values} onChange={(e) => setF({ ...f, values: e.target.value })} />}
        </>}
        {kind === "services" && <>
          {f.entries.map((e, i) => (
            <div key={i} className="grid grid-cols-[140px_1fr_32px] items-end gap-2">
              <Select label="Protokoll" value={e.protocol} onChange={(ev) => setF({ ...f, entries: f.entries.map((x, j) => (j === i ? { ...x, protocol: ev.target.value } : x)) })}>
                {["tcp", "udp", "icmp", "gre", "esp", ""].map((p) => <option key={p} value={p}>{p || "alle"}</option>)}
              </Select>
              <Input label="Ports" className="font-mono" value={e.ports} onChange={(ev) => setF({ ...f, entries: f.entries.map((x, j) => (j === i ? { ...x, ports: ev.target.value } : x)) })} />
              <IconButton icon="trash" label="Entfernen" onClick={() => setF({ ...f, entries: f.entries.filter((_, j) => j !== i) })} />
            </div>
          ))}
          <Button size="sm" variant="ghost" icon="plus" onClick={() => setF({ ...f, entries: [...f.entries, { protocol: "tcp", ports: "" }] })}>Protokoll/Port</Button>
          <details><summary className="cursor-pointer text-sm text-fg2">Als Dienstgruppe (andere Dienste einschließen)</summary>
            <div className="mt-2 max-h-48 overflow-y-auto rounded-md border border-line px-2 py-1">
              {cat.services.filter((x) => x.id !== o.id).map((x) => <Checkbox key={x.id} label={x.name} checked={f.members.includes(x.id)}
                onChange={(c) => setF({ ...f, members: c ? [...f.members, x.id] : f.members.filter((m) => m !== x.id) })} />)}
            </div>
          </details>
        </>}
        {kind === "zones" && <>
          <Select label="Interfaces" value={f.source} onChange={(e) => setF({ ...f, source: e.target.value as FwZone["source"] })}>
            <option value="manual">je Gerät zuordnen (Gerät → Firewall → Zonen)</option><option value="wan">alle WAN-Leitungen (aus der WAN-Konfiguration)</option>
          </Select>
          <ToggleField label="Management-Zugriff (WinBox/SSH) aus dieser Zone erlauben" checked={f.management} onChange={(v) => setF({ ...f, management: v })} />
        </>}
        {kind === "blocks" && <Textarea label="Parameter, Regeln und NAT (JSON)" hint="Verweise: $parameter, zone:<kürzel>, svc:<kürzel>, obj:<kürzel>" rows={14} className="font-mono text-xs" value={f.json} onChange={(e) => setF({ ...f, json: e.target.value })} />}
      </div>
    </Modal>
  );
}
