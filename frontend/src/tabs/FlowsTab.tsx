import { useState } from "react";
import { Button, Card, ErrorBox, Loading, Notice, Pill, Segment, Select, Table, useAction } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtBytes } from "../lib/format";
import type { Device } from "../lib/types";
import { useFetch } from "../lib/useFetch";

interface State { enabled: boolean; status: string; interfaces: string[]; error: string | null; retention_days: number }
interface Row { address: string; bytes: number; packets: number }
interface Top { period: string; wans: string[]; total_bytes: number; hosts: Row[]; destinations: Row[] }

/** Top-Verbraucher je WAN aus IPFIX-Aggregaten (opt-in je Gerät). */
export default function FlowsTab({ device }: { device: Device }) {
  const { can } = useAuth();
  const st = useFetch<State>(`/devices/${device.id}/flows`);
  const [period, setPeriod] = useState<"1h" | "24h" | "7d">("24h");
  const [wan, setWan] = useState("");
  const top = useFetch<Top>(`/devices/${device.id}/flows/top?period=${period}${wan ? `&wan=${encodeURIComponent(wan)}` : ""}`);
  const { busy, error, run } = useAction();
  const s = st.data;
  const toggle = (enabled: boolean) => run(async () => { await api.put(`/devices/${device.id}/flows`, { enabled }); await st.reload(); await top.reload(); });
  return (
    <div className="flex flex-col gap-4">
      <Card title="Top-Verbraucher" subtitle="IPFIX der WAN-Interfaces an die Plattform, gespeichert als 5-Minuten-Summen"
        actions={s && <Pill tone={s.status === "active" ? "green" : s.status === "error" ? "red" : "gray"}>{s.status === "active" ? `aktiv · ${s.interfaces.join(", ")}` : s.status === "error" ? "Fehler" : "aus"}</Pill>}>
        <ErrorBox error={error ?? st.error ?? s?.error} deviceId={device.id} />
        <Notice tone="blue" icon="info" title="Datenschutz">Es werden nur Summen je 5 Minuten gespeichert: interne Adresse, Gegenstelle, Bytes und Pakete je WAN – keine Ports,
          keine Inhalte, keine Einzelverbindungen. Aufbewahrung {s?.retention_days ?? 7} Tage. Die Auswertung kann Rückschlüsse auf das Verhalten einzelner Personen zulassen –
          vor dem Einschalten Rechtsgrundlage und Information der Betroffenen klären.</Notice>
        {can("technician") && s && <div className="mt-3">
          {s.enabled ? <Button variant="secondary" disabled={busy} onClick={() => void toggle(false)}>Ausschalten</Button>
            : <Button disabled={busy || device.status === "offline"} onClick={() => void toggle(true)}>Einschalten</Button>}
        </div>}
      </Card>
      {!top.data ? <Loading rows={3} /> : (
        <>
          <div className="flex flex-wrap items-center gap-3">
            <Segment label="Zeitraum" value={period} onChange={setPeriod} options={[{ value: "1h", label: "1 Stunde" }, { value: "24h", label: "24 Stunden" }, { value: "7d", label: "7 Tage" }]} />
            {top.data.wans.length > 1 && <Select value={wan} onChange={(e) => setWan(e.target.value)} aria-label="WAN"><option value="">Alle WAN</option>{top.data.wans.map((w) => <option key={w}>{w}</option>)}</Select>}
            <span className="text-sm text-fg2">Gesamt {fmtBytes(top.data.total_bytes)}</span>
          </div>
          <div className="grid gap-4 lg:grid-cols-2">
            <TopTable title="Top-Hosts (intern)" rows={top.data.hosts} total={top.data.total_bytes} />
            <TopTable title="Top-Ziele (extern)" rows={top.data.destinations} total={top.data.total_bytes} />
          </div>
        </>
      )}
    </div>
  );
}

function TopTable({ title, rows, total }: { title: string; rows: Row[]; total: number }) {
  return (
    <Card title={title} flush>
      <Table head={["Adresse", "Volumen", "Anteil", "Pakete"]} empty={rows.length === 0} emptyText="Keine Daten im Zeitraum">
        {rows.map((r) => (
          <tr key={r.address}>
            <td className="px-3 py-2 font-mono text-xs">{r.address}</td>
            <td className="px-3 py-2">{fmtBytes(r.bytes)}</td>
            <td className="w-40 px-3 py-2"><div className="h-1.5 overflow-hidden rounded bg-sunken"><div className="h-full bg-blue" style={{ width: `${total ? (r.bytes / total) * 100 : 0}%` }} /></div></td>
            <td className="px-3 py-2 text-fg2">{r.packets.toLocaleString("de-DE")}</td>
          </tr>
        ))}
      </Table>
    </Card>
  );
}
