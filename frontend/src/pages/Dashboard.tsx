import { Link } from "react-router-dom";
import { Card, PageHeader, Stat, StatusDot, Table } from "../components/ui";
import { fmtAgo } from "../lib/format";
import { useLive } from "../lib/live";
import type { Device, Site } from "../lib/types";
import { useFetch } from "../lib/useFetch";
import { dashboardWidgets } from "../widgets";

interface Summary {
  devices_total: number;
  devices_online: number;
  devices_offline: number;
  devices_unknown: number;
  devices_pending: number;
  sites_total: number;
}

export default function Dashboard() {
  const summary = useFetch<Summary>("/dashboard/summary");
  const devices = useFetch<Device[]>("/devices");
  const sites = useFetch<Site[]>("/sites");
  const siteName = (id: string | null) => sites.data?.find((s) => s.id === id)?.name ?? "–";

  useLive((e) => {
    if (e.type === "device.status" || e.type === "device.paired") {
      void summary.reload();
      void devices.reload();
    } else if (e.type === "device.poll") {
      const d = e.data as { id: string; status: Device["status"]; uptime: string; last_seen_at: string; cpu_load: number };
      devices.setData((prev) => prev?.map((x) => (x.id === d.id ? { ...x, status: d.status, uptime: d.uptime, last_seen_at: d.last_seen_at, facts: { ...x.facts, cpu_load: d.cpu_load } } : x)) ?? prev);
    }
  });

  const s = summary.data;
  return (
    <>
      <PageHeader title="Dashboard" subtitle="Status der gesamten Router-Flotte in Echtzeit" />
      <div className="mb-6 grid grid-cols-2 gap-4 md:grid-cols-5">
        <Stat label="Geräte" value={s?.devices_total ?? "–"} />
        <Stat label="Online" value={s?.devices_online ?? "–"} tone="green" />
        <Stat label="Offline" value={s?.devices_offline ?? "–"} tone={s && s.devices_offline > 0 ? "red" : undefined} />
        <Stat label="Wartet auf Pairing" value={s?.devices_pending ?? "–"} tone={s && s.devices_pending > 0 ? "yellow" : undefined} />
        <Stat label="Standorte" value={s?.sites_total ?? "–"} />
      </div>
      {dashboardWidgets.map((W, i) => (
        <W key={i} />
      ))}
      <Card title="Geräte">
        <Table head={["", "Name", "Standort", "Tunnel-IP", "RouterOS", "CPU", "Uptime", "Zuletzt gesehen"]} empty={devices.data?.length === 0}>
          {devices.data?.map((d) => (
            <tr key={d.id} className="hover:bg-slate-50">
              <td className="px-3 py-2">
                <StatusDot status={d.pairing_status === "paired" ? d.status : "unknown"} />
              </td>
              <td className="px-3 py-2 font-medium">
                <Link to={`/devices/${d.id}`} className="text-brand-700 hover:underline">
                  {d.name}
                </Link>
                {d.pairing_status !== "paired" && <span className="ml-2 text-xs text-amber-600">({d.pairing_status})</span>}
              </td>
              <td className="px-3 py-2">{siteName(d.site_id)}</td>
              <td className="px-3 py-2 font-mono text-xs">{d.tunnel_ip}</td>
              <td className="px-3 py-2">{d.routeros_version ?? "–"}</td>
              <td className="px-3 py-2">{d.facts?.cpu_load != null ? `${d.facts.cpu_load}%` : "–"}</td>
              <td className="px-3 py-2">{d.uptime ?? "–"}</td>
              <td className="px-3 py-2 text-slate-500">{fmtAgo(d.last_seen_at)}</td>
            </tr>
          ))}
        </Table>
      </Card>
    </>
  );
}
