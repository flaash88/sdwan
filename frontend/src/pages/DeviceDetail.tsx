import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import PairingBox from "../components/PairingBox";
import { Button, Card, ErrorBox, Input, PageHeader, Select, StatusBadge, cls, useAction } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtAgo, fmtBytes, fmtDate } from "../lib/format";
import { useLive } from "../lib/live";
import { useMeta } from "../lib/meta";
import type { Device, PairingInfo, Site } from "../lib/types";
import { useFetch } from "../lib/useFetch";
import { deviceTabs } from "../deviceTabs";

export default function DeviceDetail() {
  const { id } = useParams();
  const dev = useFetch<Device>(`/devices/${id}`);
  const [tab, setTab] = useState("overview");
  useLive((e) => {
    if ((e.data as { id?: string }).id === id) void dev.reload();
  }, ["device.status", "device.paired", "device.poll"]);
  const d = dev.data;
  if (!d) return <ErrorBox error={dev.error} />;
  const tabs = [{ key: "overview", label: "Übersicht" }, ...deviceTabs.filter((t) => !t.pairedOnly || d.pairing_status === "paired")];
  const Active = deviceTabs.find((t) => t.key === tab)?.component;
  return (
    <>
      <PageHeader
        title={d.name}
        subtitle={<span className="flex items-center gap-2"><StatusBadge status={d.pairing_status === "paired" ? d.status : d.pairing_status} /> {d.model ?? "unbekanntes Modell"} · {d.tunnel_ip}</span>}
      />
      <div className="mb-5 flex gap-1 border-b border-slate-200">
        {tabs.map((t) => (
          <button key={t.key} onClick={() => setTab(t.key)} className={cls("-mb-px border-b-2 px-4 py-2 text-sm", tab === t.key ? "border-brand-600 font-medium text-brand-700" : "border-transparent text-slate-500 hover:text-slate-800")}>
            {t.label}
          </button>
        ))}
      </div>
      {tab === "overview" ? <Overview device={d} reload={dev.reload} /> : Active ? <Active device={d} /> : null}
    </>
  );
}

function Overview({ device: d, reload }: { device: Device; reload: () => Promise<void> }) {
  const { can } = useAuth();
  const meta = useMeta();
  const nav = useNavigate();
  const sites = useFetch<Site[]>("/sites");
  const [pairing, setPairing] = useState<PairingInfo | null>(null);
  const [name, setName] = useState(d.name);
  const [siteId, setSiteId] = useState(d.site_id ?? "");
  const [tags, setTags] = useState(d.tags.join(", "));
  const [meshEp, setMeshEp] = useState(d.mesh_endpoint ?? "");
  const { busy, error, run } = useAction();
  const f = d.facts as Record<string, number | undefined>;
  const rows: [string, string][] = [
    ["Identity", d.identity ?? "–"],
    ["Seriennummer", d.serial ?? "–"],
    ["RouterOS", d.routeros_version ?? "–"],
    ["Architektur", d.architecture ?? "–"],
    ["Uptime", d.uptime ?? "–"],
    ["CPU-Last", f.cpu_load != null ? `${f.cpu_load}%` : "–"],
    ["RAM", f.total_memory ? `${fmtBytes((f.total_memory ?? 0) - (f.free_memory ?? 0))} / ${fmtBytes(f.total_memory)}` : "–"],
    ["Tunnel-IP", d.tunnel_ip],
    ["Mesh-IP", d.mesh_ip ?? "–"],
    ["WG-Public-Key", d.wg_public_key ?? "–"],
    ["Gepairt", fmtDate(d.paired_at)],
    ["Letzter API-Kontakt", fmtAgo(d.last_seen_at)],
    ["Letzter WG-Handshake", fmtAgo(d.last_handshake_at)],
  ];
  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <Card title="Systeminformationen">
        <dl className="grid grid-cols-3 gap-y-2 text-sm">
          {rows.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="text-slate-500">{k}</dt>
              <dd className="col-span-2 break-all font-mono text-xs leading-5">{v}</dd>
            </div>
          ))}
        </dl>
      </Card>
      <div className="space-y-6">
        {(d.pairing_status !== "paired" || pairing) && can("technician") && (
          <Card title="Onboarding">
            <ErrorBox error={error} />
            {pairing ? (
              <PairingBox pairing={pairing} />
            ) : (
              <div className="space-y-3 text-sm">
                <p>Dieses Gerät ist noch nicht verbunden. Erzeuge einen neuen Onboarding-Befehl:</p>
                <div className="flex gap-2">
                  <Button disabled={busy} onClick={() => void run(async () => setPairing(await api.post<PairingInfo>(`/devices/${d.id}/pairing-token`)))}>Onboarding-Befehl erzeugen</Button>
                  {meta?.simulator && d.pairing_status === "pending" && (
                    <Button variant="secondary" disabled={busy} onClick={() => void run(async () => { await api.post(`/devices/${d.id}/simulate-pair`); await reload(); })}>
                      Pairing simulieren
                    </Button>
                  )}
                </div>
              </div>
            )}
          </Card>
        )}
        {d.ztp_state !== "none" && (
          <Card title={`Zero-Touch: ${d.ztp_state}`}>
            <ul className="space-y-1 text-xs">
              {d.ztp_log.map((l, i) => <li key={i}><span className="text-slate-500">{fmtDate(l.at)}</span> – {l.msg}</li>)}
            </ul>
          </Card>
        )}
        {can("technician") && (
          <Card title="Einstellungen">
            <form
              className="space-y-3"
              onSubmit={(e) => {
                e.preventDefault();
                void run(async () => {
                  await api.patch(`/devices/${d.id}`, { name, site_id: siteId || null, mesh_endpoint: meshEp || null, tags: tags.split(",").map((t) => t.trim()).filter(Boolean) });
                  await reload();
                });
              }}
            >
              <Input label="Name" value={name} onChange={(e) => setName(e.target.value)} />
              <Select label="Standort" value={siteId} onChange={(e) => setSiteId(e.target.value)}>
                <option value="">– kein Standort –</option>
                {sites.data?.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
              </Select>
              <Input label="Tags" value={tags} onChange={(e) => setTags(e.target.value)} />
              <Input label="Öffentlicher Mesh-Endpoint (Hostname/IP, optional)" value={meshEp} onChange={(e) => setMeshEp(e.target.value)} placeholder="automatisch erkannt" />
              <div className="flex flex-wrap justify-between gap-2">
                <Button disabled={busy}>Speichern</Button>
                {can("admin") && (
                  <div className="flex gap-2">
                    {d.pairing_status === "paired" && (
                      <Button type="button" variant="secondary" onClick={() => confirm("Gerät sperren? Der Tunnel wird getrennt.") && void run(async () => { await api.post(`/devices/${d.id}/revoke`); await reload(); })}>
                        Sperren
                      </Button>
                    )}
                    <Button type="button" variant="danger" onClick={() => confirm("Gerät endgültig löschen?") && void run(async () => { await api.del(`/devices/${d.id}`); nav("/devices"); })}>
                      Löschen
                    </Button>
                  </div>
                )}
              </div>
            </form>
          </Card>
        )}
      </div>
    </div>
  );
}
