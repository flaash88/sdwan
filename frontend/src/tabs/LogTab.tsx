import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Button, Card, Checkbox, EmptyState, ErrorBox, Loading, Notice, Pill, Select, useAction, type Tone } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtFull } from "../lib/format";
import type { Device } from "../lib/types";
import { useFetch } from "../lib/useFetch";

interface Cfg { enabled: boolean; topics: string[]; allowed_topics: string[]; applied_at: string | null; last_error: string | null; retention_days: number }
interface Msg { id: string; at: string; severity: number | null; topics: string | null; message: string }
const SEV: [string, Tone][] = [["emerg", "red"], ["alert", "red"], ["crit", "red"], ["error", "red"], ["warning", "orange"], ["notice", "blue"], ["info", "gray"], ["debug", "gray"]];

/** Zentrales Syslog des Geräts (opt-in) mit Filter; ``?around=<Zeit>`` zeigt ±15 min um einen Zeitpunkt. */
export default function LogTab({ device }: { device: Device }) {
  const { can } = useAuth();
  const [params, setParams] = useSearchParams();
  const around = params.get("around");
  const cfg = useFetch<Cfg>(`/devices/${device.id}/syslog`);
  const [q, setQ] = useState("");
  const [topic, setTopic] = useState("");
  const [sev, setSev] = useState("");
  const [query, setQuery] = useState("");
  const logs = useFetch<{ messages: Msg[]; since: string | null; until: string | null }>(`/devices/${device.id}/logs?limit=500${query}${around ? `&around=${encodeURIComponent(around)}` : ""}`);
  const [edit, setEdit] = useState<string[] | null>(null);
  const { busy, error, run } = useAction();
  useEffect(() => { if (cfg.data) setEdit(null); }, [cfg.data]);
  const c = cfg.data;
  const apply = () => setQuery(`${q ? `&q=${encodeURIComponent(q)}` : ""}${topic ? `&topic=${encodeURIComponent(topic)}` : ""}${sev ? `&max_severity=${sev}` : ""}`);
  return (
    <div className="flex flex-col gap-4">
      <Card title="Syslog" subtitle={c ? `${c.enabled ? "aktiv" : "aus"} · Aufbewahrung ${c.retention_days} Tage` : undefined}
        actions={can("technician") && c && <Button size="sm" variant="secondary" icon="settings" onClick={() => setEdit(edit ? null : c.topics)}>{edit ? "Schließen" : "Einstellungen"}</Button>}>
        <ErrorBox error={error ?? c?.last_error} />
        {!c ? <Loading rows={1} /> : !edit ? (
          <p className="text-fg2">{c.enabled ? <>Der Router sendet die Topics <span className="font-mono">{c.topics.join(", ")}</span> über den Management-Tunnel an die Plattform.</> : "Der Router sendet kein Syslog an die Plattform. In den Einstellungen aktivieren (legt auf dem Router die Aktion sdwan-syslog und passende Logging-Regeln an)."}</p>
        ) : (
          <div className="flex flex-col gap-3">
            <div className="grid grid-cols-2 gap-1 sm:grid-cols-4 lg:grid-cols-6">
              {c.allowed_topics.map((t) => <Checkbox key={t} label={t} checked={edit.includes(t)} onChange={(v) => setEdit(v ? [...edit, t] : edit.filter((x) => x !== t))} />)}
            </div>
            <div className="flex gap-2">
              <Button disabled={busy || !edit.length} onClick={() => void run(async () => { await api.put(`/devices/${device.id}/syslog`, { enabled: true, topics: edit }); await cfg.reload(); })}>Aktivieren / übernehmen</Button>
              {c.enabled && <Button variant="danger-outline" disabled={busy} onClick={() => void run(async () => { await api.put(`/devices/${device.id}/syslog`, { enabled: false, topics: c.topics }); await cfg.reload(); })}>Syslog abschalten</Button>}
            </div>
          </div>
        )}
      </Card>
      {around && <Notice tone="blue" title={`±15 Minuten um ${fmtFull(around)}`}><Button size="sm" variant="ghost" onClick={() => { params.delete("around"); setParams(params, { replace: true }); }}>Zeitfilter entfernen</Button></Notice>}
      <Card flush title="Meldungen" actions={<form className="flex flex-wrap items-center gap-2" onSubmit={(e) => { e.preventDefault(); apply(); }}>
        <input aria-label="Text" placeholder="Text …" className="h-8 w-40 rounded-md border border-line bg-panel px-2 text-sm" value={q} onChange={(e) => setQ(e.target.value)} />
        <input aria-label="Topic" placeholder="Topic" className="h-8 w-28 rounded-md border border-line bg-panel px-2 text-sm" value={topic} onChange={(e) => setTopic(e.target.value)} />
        <div className="w-36"><Select aria-label="Schweregrad" value={sev} onChange={(e) => setSev(e.target.value)}><option value="">alle</option><option value="3">bis error</option><option value="4">bis warning</option><option value="6">bis info</option></Select></div>
        <Button size="sm" icon="search">Filtern</Button>
      </form>}>
        {!logs.data ? <Loading rows={3} /> : logs.data.messages.length === 0 ? <EmptyState compact title="Keine Meldungen" text={c?.enabled ? "Im gewählten Zeitraum/Filter nichts empfangen." : "Syslog ist für dieses Gerät nicht aktiv."} /> : (
          <div className="max-h-[65vh] overflow-y-auto">
            {logs.data.messages.map((m) => {
              const [label, tone] = m.severity != null ? SEV[m.severity] : ["–", "gray" as Tone];
              return (
                <div key={m.id} className="grid grid-cols-[150px_80px_minmax(0,160px)_minmax(0,1fr)] items-start gap-3 border-b border-line px-4 py-1.5 font-mono text-xs last:border-b-0">
                  <span className="text-fg2">{fmtFull(m.at)}</span><span><Pill tone={tone}>{label}</Pill></span><span className="truncate text-fg3">{m.topics}</span><span className="break-words">{m.message}</span>
                </div>
              );
            })}
          </div>
        )}
      </Card>
    </div>
  );
}
