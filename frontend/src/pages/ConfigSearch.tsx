import { useState } from "react";
import { Link } from "react-router-dom";
import { Button, Card, EmptyState, ErrorBox, Loading, Notice, PageHeader, ToggleField, useAction } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtFull } from "../lib/format";

interface Hit { device_id: string; device: string; backup_id: string; backup_at: string; matches: { line: number; context: { n: number; text: string; hit: boolean }[] }[] }
interface Result { hits: Hit[]; devices_searched: number; truncated: boolean }

/** Volltext-/Regex-Suche über die jeweils letzten Backups; Geheimnisse sind maskiert. */
export default function ConfigSearch() {
  const { me } = useAuth();
  const [q, setQ] = useState("");
  const [regex, setRegex] = useState(false);
  const [all, setAll] = useState(false);
  const [res, setRes] = useState<Result | null>(null);
  const { busy, error, run } = useAction();
  const search = () => run(async () => setRes(await api.get<Result>(`/config-search?${new URLSearchParams({ q, regex: String(regex), all_tenants: String(all) })}`)));
  return (
    <>
      <PageHeader title="Config-Suche" subtitle="Durchsucht das jeweils letzte Backup aller Geräte. Passwörter und Schlüssel sind maskiert und nie Treffer." />
      <Card>
        <form className="flex flex-wrap items-center gap-3" onSubmit={(e) => { e.preventDefault(); if (q.trim()) void search(); }}>
          <input aria-label="Suchbegriff" autoFocus placeholder={regex ? "regulärer Ausdruck, z. B. dst-port=(23|21)\\b" : "z. B. 192.0.2.10 oder /ip service"} value={q} onChange={(e) => setQ(e.target.value)}
            className="h-9 min-w-72 flex-1 rounded-md border border-line bg-panel px-3 font-mono text-sm" />
          <ToggleField label="Regulärer Ausdruck" checked={regex} onChange={setRegex} />
          {me?.user.is_superuser && <ToggleField label="Alle Mandanten" checked={all} onChange={setAll} />}
          <Button icon={busy ? "loader" : "search"} disabled={busy || !q.trim()}>Suchen</Button>
        </form>
      </Card>
      <div className="mt-4 flex flex-col gap-4">
        <ErrorBox error={error} />
        {busy && <Loading rows={3} />}
        {res && !busy && (res.hits.length === 0 ? <Card><EmptyState compact title="Keine Treffer" text={`${res.devices_searched} Backups durchsucht.`} /></Card> : <>
          <p className="text-fg2">{res.hits.reduce((n, h) => n + h.matches.length, 0)} Treffer auf {res.hits.length} Geräten ({res.devices_searched} Backups durchsucht)</p>
          {res.truncated && <Notice tone="orange">Trefferzahl begrenzt – Suche eingrenzen.</Notice>}
          {res.hits.map((h) => (
            <Card key={h.device_id} flush title={<Link to={`/devices/${h.device_id}?tab=backups`} className="hover:underline">{h.device}</Link>} subtitle={`Backup vom ${fmtFull(h.backup_at)}`}>
              {h.matches.map((m) => (
                <pre key={m.line} className="m-0 overflow-x-auto border-b border-line px-4 py-2 font-mono text-xs leading-[1.6] last:border-b-0">
                  {m.context.map((c) => <div key={c.n} className={c.hit ? "bg-orange-bg text-fg" : "text-fg3"}><span className="mr-3 inline-block w-10 select-none text-right text-fg3">{c.n}</span>{c.text}</div>)}
                </pre>
              ))}
            </Card>
          ))}
        </>)}
      </div>
    </>
  );
}
