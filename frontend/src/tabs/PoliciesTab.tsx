import { useState } from "react";
import { Link } from "react-router-dom";
import { Badge, Button, Card, Checkbox, ErrorBox, Input, Modal, StatusBadge, Table, cls, useAction } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtDate } from "../lib/format";
import type { Device } from "../lib/types";
import { useFetch } from "../lib/useFetch";

interface DP { policy_id: string; name: string; scope: string; version: number; deployed_version: number | null; status: string; last_error: string | null; deployed_at: string | null }
type Row = Record<string, string | boolean | null> & { managed: boolean };
interface Fw { filter: Row[]; nat: Row[]; address_lists: Row[] }
interface ImportResult { policy_id: string; counts: Record<string, number>; warnings: string[]; replaced: boolean; deployment_status?: string }

const MATCH_KEYS = ["protocol", "src-address", "dst-address", "src-address-list", "dst-address-list", "dst-port", "in-interface", "in-interface-list", "out-interface", "out-interface-list", "connection-state", "ipsec-policy", "to-addresses", "to-ports", "jump-target"];

function matchText(r: Row) {
  return MATCH_KEYS.filter((k) => r[k] != null && r[k] !== "").map((k) => `${k}=${r[k]}`).join(" ");
}

function isOff(v: unknown) {
  return v === true || v === "true" || v === "yes";
}

export default function PoliciesTab({ device }: { device: Device }) {
  const { can } = useAuth();
  const list = useFetch<DP[]>(`/devices/${device.id}/policies`);
  const fw = useFetch<Fw>(`/devices/${device.id}/firewall`);
  const [tab, setTab] = useState<keyof Fw>("filter");
  const [open, setOpen] = useState(false);
  const manualCount = fw.data ? fw.data.filter.filter((r) => !r.managed).length + fw.data.nat.filter((r) => !r.managed).length : 0;
  const rows = fw.data?.[tab] ?? [];
  return (
    <div className="space-y-6">
      <Card title="Zugewiesene Firewall-Policies (in Push-Reihenfolge)">
        <Table head={["Policy", "Geltung", "Stand", "Status", "Gepusht"]} empty={list.data?.length === 0}>
          {list.data?.map((p) => (
            <tr key={p.policy_id}>
              <td className="px-3 py-2"><Link className="text-brand-700 hover:underline" to={`/policies/${p.policy_id}`}>{p.name}</Link></td>
              <td className="px-3 py-2">{p.scope === "global" ? <Badge color="blue">global</Badge> : <Badge>Mandant</Badge>}</td>
              <td className="px-3 py-2">{p.deployed_version ? `v${p.deployed_version}` : "–"} / v{p.version}</td>
              <td className="px-3 py-2"><StatusBadge status={p.status === "deployed" && p.deployed_version !== p.version ? "pending" : p.status} />{p.last_error && <div className="text-xs text-red-600">{p.last_error}</div>}</td>
              <td className="px-3 py-2 text-slate-500">{fmtDate(p.deployed_at)}</td>
            </tr>
          ))}
        </Table>
      </Card>

      <Card
        title={
          <div className="flex items-center gap-3">
            <span>Aktuelle Firewall am Router</span>
            {(["filter", "nat", "address_lists"] as const).map((k) => (
              <button key={k} onClick={() => setTab(k)} className={cls("rounded px-2 py-1 text-sm font-normal", tab === k ? "bg-brand-100 text-brand-800" : "text-slate-500 hover:bg-slate-100")}>
                {k === "filter" ? "Filter" : k === "nat" ? "NAT" : "Address-Lists"} ({fw.data?.[k].length ?? "…"})
              </button>
            ))}
          </div>
        }
        actions={<>
          <Button variant="secondary" onClick={() => void fw.reload()}>Neu laden</Button>
          {can("technician") && <Button disabled={!manualCount && !fw.data?.address_lists.some((r) => !r.managed)} onClick={() => setOpen(true)}>Als Policy übernehmen</Button>}
        </>}
      >
        <ErrorBox error={fw.error} />
        {fw.loading && !fw.data && <p className="text-sm text-slate-400">Lese Regeln vom Router …</p>}
        {tab !== "address_lists" ? (
          <Table head={["#", "Chain", "Action", "Bedingungen", "Kommentar", "Herkunft"]} empty={rows.length === 0}>
            {rows.map((r, i) => (
              <tr key={String(r[".id"])} className={isOff(r.disabled) ? "text-slate-400 line-through" : ""}>
                <td className="px-3 py-1.5 text-slate-400">{i}</td>
                <td className="px-3 py-1.5 font-mono text-xs">{String(r.chain ?? "")}</td>
                <td className="px-3 py-1.5 font-mono text-xs">{String(r.action ?? "")}</td>
                <td className="px-3 py-1.5 font-mono text-xs">{matchText(r) || "–"}</td>
                <td className="max-w-xs truncate px-3 py-1.5 text-xs text-slate-600" title={String(r.comment ?? "")}>{String(r.comment ?? "")}</td>
                <td className="px-3 py-1.5">{r.managed ? <Badge color="blue">Plattform ({String(r.managed_by)})</Badge> : <Badge>manuell</Badge>}</td>
              </tr>
            ))}
          </Table>
        ) : (
          <Table head={["Liste", "Adresse", "Kommentar", "Herkunft"]} empty={rows.length === 0}>
            {rows.map((r) => (
              <tr key={String(r[".id"])}>
                <td className="px-3 py-1.5 font-mono text-xs">{String(r.list)}</td>
                <td className="px-3 py-1.5 font-mono text-xs">{String(r.address)}{r.timeout ? <span className="ml-1 text-slate-400">(temporär)</span> : null}</td>
                <td className="px-3 py-1.5 text-xs text-slate-600">{String(r.comment ?? "")}</td>
                <td className="px-3 py-1.5">{r.managed ? <Badge color="blue">Plattform</Badge> : <Badge>manuell</Badge>}</td>
              </tr>
            ))}
          </Table>
        )}
        <p className="mt-3 text-xs text-slate-500">Dynamische Einträge (z. B. von Diensten erzeugt) werden nicht angezeigt. „Plattform“ = von einer zentralen Policy, dem VPN-Mesh, WAN oder Content-Filter verwaltet.</p>
      </Card>
      {open && <ImportModal device={device} onClose={() => setOpen(false)} onDone={() => { void list.reload(); void fw.reload(); }} />}
    </div>
  );
}

function ImportModal({ device, onClose, onDone }: { device: Device; onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState(`${device.name} – Bestand`);
  const [sections, setSections] = useState(["filter", "nat", "address_lists"]);
  const [replace, setReplace] = useState(false);
  const [res, setRes] = useState<ImportResult | null>(null);
  const { busy, error, run } = useAction();
  const toggle = (s: string) => setSections(sections.includes(s) ? sections.filter((x) => x !== s) : [...sections, s]);
  return (
    <Modal open onClose={onClose} title="Bestehende Regeln als Policy übernehmen" wide>
      <ErrorBox error={error} />
      {res ? (
        <div className="space-y-3 text-sm">
          <p>Policy angelegt: {res.counts.filter} Filter-, {res.counts.nat} NAT-Regeln, {res.counts.address_lists} Address-List-Einträge.</p>
          {res.deployment_status && <p>Push: <StatusBadge status={res.deployment_status} /> {res.replaced ? "– Original-Regeln wurden ersetzt." : "– Original-Regeln blieben unverändert."}</p>}
          {res.warnings.length > 0 && (
            <div className="rounded-lg bg-amber-50 p-3 text-xs text-amber-800">
              <b>Nicht übernommen / angepasst:</b>
              <ul className="mt-1 list-disc pl-4">{res.warnings.map((w) => <li key={w}>{w}</li>)}</ul>
            </div>
          )}
          <div className="flex justify-end gap-2">
            <Link to={`/policies/${res.policy_id}`}><Button variant="secondary">Policy öffnen</Button></Link>
            <Button onClick={() => { onDone(); onClose(); }}>Fertig</Button>
          </div>
        </div>
      ) : (
        <div className="space-y-4">
          <Input label="Name der Policy" value={name} onChange={(e) => setName(e.target.value)} />
          <div className="flex gap-6">
            <Checkbox label="Filter-Regeln" checked={sections.includes("filter")} onChange={() => toggle("filter")} />
            <Checkbox label="NAT-Regeln" checked={sections.includes("nat")} onChange={() => toggle("nat")} />
            <Checkbox label="Address-Lists" checked={sections.includes("address_lists")} onChange={() => toggle("address_lists")} />
          </div>
          <div className="space-y-2 rounded-lg border p-3 text-sm">
            <label className="flex gap-2"><input type="radio" checked={!replace} onChange={() => setReplace(false)} /> <span><b>Nur als Policy anlegen</b> – Router bleibt unverändert. Die Policy kann danach bearbeitet und anderen Geräten zugewiesen werden.</span></label>
            <label className="flex gap-2"><input type="radio" checked={replace} onChange={() => setReplace(true)} /> <span><b>Übernehmen und ersetzen</b> – Policy wird diesem Router zugewiesen und gepusht, danach werden die Original-Regeln entfernt. Ab dann werden sie zentral verwaltet. Vorher wird automatisch ein Backup erstellt.</span></label>
          </div>
          <p className="text-xs text-slate-500">Regeln in eigenen Chains oder mit nicht unterstützten Feldern werden übersprungen und angezeigt. Beim Ersetzen bleiben diese unverändert auf dem Router.</p>
          <div className="flex justify-end"><Button disabled={busy || !sections.length} onClick={() => void run(async () => setRes(await api.post<ImportResult>(`/devices/${device.id}/firewall/import`, { name, sections, replace })))}>{busy ? "Übernehme …" : "Übernehmen"}</Button></div>
        </div>
      )}
    </Modal>
  );
}
