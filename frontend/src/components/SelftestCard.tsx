import { useState } from "react";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtFull } from "../lib/format";
import { useMeta } from "../lib/meta";
import type { Device } from "../lib/types";
import { useFetch } from "../lib/useFetch";
import { Icon } from "./Icon";
import { Button, Card, EmptyState, ErrorBox, Loading, Pill, Segment, cls, useAction, type Tone } from "./ui";

type St = "ok" | "warn" | "error";
interface Check {
  key: string; label: string; kind: "path" | "check"; status: St; command?: string; used_by?: string; ms?: number; rows?: number; count?: number | null;
  reachable?: boolean; missing?: string[]; missing_optional?: string[]; notes?: string[]; error?: string | null; expected?: string[]; sample_fields?: string[]; value?: unknown;
}
interface Selftest { status: St; ran_at: string; ran_by: string | null; duration_ms: number; checks: Check[]; summary?: Record<St, number> }

const TONE: Record<St, [string, Tone, "checkCircle" | "alert" | "xCircle"]> = {
  ok: ["OK", "green", "checkCircle"], warn: ["Warnung", "orange", "alert"], error: ["Fehler", "red", "xCircle"],
};
const OVERALL: Record<St, string> = { ok: "Bestanden", warn: "Bestanden mit Warnungen", error: "Abweichungen gefunden" };

function summaryLine(c: Check): string {
  if (c.error) return c.error;
  if (c.missing?.length) return `Fehlende Felder: ${c.missing.join(", ")}`;
  if (c.notes?.length) return c.notes[0];
  if (c.kind === "path") return `${c.rows ?? 0} ${c.rows === 1 ? "Eintrag" : "Einträge"}${c.missing_optional?.length ? ` · nicht geliefert (optional): ${c.missing_optional.join(", ")}` : ""}`;
  return "";
}

function Row({ c }: { c: Check }) {
  const [label, tone, icon] = TONE[c.status];
  return (
    <details className="group border-b border-line last:border-b-0 [&[open]]:bg-panel2">
      <summary className="grid cursor-pointer list-none grid-cols-[96px_minmax(0,220px)_minmax(0,1fr)_70px_16px] items-center gap-3 px-4 py-2 hover:bg-hover [&::-webkit-details-marker]:hidden">
        <Pill tone={tone} icon={icon}>{label}</Pill>
        <span className="flex min-w-0 flex-col leading-tight">
          <span className="truncate font-medium">{c.label}</span>
          {c.command && <span className="truncate font-mono text-[11.5px] text-fg3">{c.command}</span>}
        </span>
        <span className={cls("truncate", c.status === "error" ? "text-red-text" : c.status === "warn" ? "text-orange-text" : "text-fg2")}>{summaryLine(c)}</span>
        <span className="text-right text-xs text-fg3">{c.ms != null ? `${Math.round(c.ms)} ms` : ""}</span>
        <Icon name="chevDown" className="text-[14px] text-fg3 transition-transform group-open:rotate-180" />
      </summary>
      <div className="grid gap-x-6 gap-y-2 px-4 pb-3 pt-1 text-[12.5px] sm:grid-cols-2">
        {c.used_by && <div><span className="text-fg3">Genutzt von: </span>{c.used_by}</div>}
        {c.kind === "path" && <div><span className="text-fg3">Erreichbar: </span>{c.reachable ? "ja" : "nein"} · {c.rows ?? 0} Einträge{c.count != null ? ` (gesamt ${c.count})` : ""}</div>}
        {!!c.missing?.length && <div className="text-red-text"><span className="font-medium">Fehlende Pflichtfelder: </span><span className="font-mono">{c.missing.join(", ")}</span></div>}
        {!!c.missing_optional?.length && <div><span className="text-fg3">Nicht geliefert (optional): </span><span className="font-mono">{c.missing_optional.join(", ")}</span></div>}
        {!!c.expected?.length && <div><span className="text-fg3">Erwartet: </span><span className="font-mono">{c.expected.join(", ")}</span></div>}
        {!!c.sample_fields?.length && <div className="sm:col-span-2"><span className="text-fg3">Geliefert: </span><span className="break-all font-mono">{c.sample_fields.join(", ")}</span></div>}
        {c.notes?.map((n) => <div key={n} className="sm:col-span-2">{n}</div>)}
        {c.error && <div className="text-red-text sm:col-span-2">{c.error}</div>}
      </div>
    </details>
  );
}

export default function SelftestCard({ device }: { device: Device }) {
  const { can } = useAuth();
  const meta = useMeta();
  const last = useFetch<Selftest | null>(`/devices/${device.id}/selftest`);
  const [filter, setFilter] = useState<"issues" | "all">("issues");
  const { busy, error, run } = useAction();
  const t = last.data;
  const exportJson = () => {
    if (!t) return;
    const payload = {
      exported_at: new Date().toISOString(), platform: { product: meta?.product_name, version: meta?.version },
      device: { name: device.name, model: device.model, routeros_version: device.routeros_version, architecture: device.architecture, serial: device.serial, tunnel_ip: device.tunnel_ip },
      selftest: t,
    };
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" }));
    a.download = `selbsttest-${device.name}-${t.ran_at.slice(0, 16).replace(/[:T]/g, "-")}.json`;
    a.click();
  };
  const issues = t?.checks.filter((c) => c.status !== "ok") ?? [];
  const rows = filter === "issues" ? issues : t?.checks ?? [];
  return (
    <Card flush title="Selbsttest" subtitle={t ? `${fmtFull(t.ran_at)}${t.ran_by ? ` · ${t.ran_by}` : ""} · ${(t.duration_ms / 1000).toFixed(1)} s` : "prüft Pfade, Felder, Rechte und Uhrzeit – nur lesend"}
      actions={<>
        {t && <Button size="sm" variant="secondary" icon="download" onClick={exportJson}>JSON</Button>}
        {can("technician") && <Button size="sm" icon={busy ? "loader" : "activity"} disabled={busy || device.status !== "online"}
          onClick={() => void run(async () => { last.setData(await api.post<Selftest>(`/devices/${device.id}/selftest`)); })}>{busy ? "Läuft …" : "Selbsttest ausführen"}</Button>}
      </>}>
      {error && <div className="px-4 pt-3"><ErrorBox error={error} /></div>}
      {last.data === null && !last.loading ? (
        <EmptyState compact title="Noch kein Selbsttest" text="Vor dem ersten Labortest ausführen: zeigt, ob der Router alle Pfade und Felder liefert, die die Plattform liest." />
      ) : !t ? <Loading rows={3} /> : (
        <>
          <div className="flex flex-wrap items-center gap-3 border-b border-line px-4 py-2.5">
            <Pill tone={TONE[t.status][1]} icon={TONE[t.status][2]}>{OVERALL[t.status]}</Pill>
            <span className="text-xs text-fg2">{t.summary?.ok ?? 0} OK · {t.summary?.warn ?? 0} Warnungen · {t.summary?.error ?? 0} Fehler</span>
            <div className="flex-1" />
            <Segment label="Anzeige" value={filter} onChange={setFilter} options={[{ value: "issues", label: "Auffällig", count: issues.length }, { value: "all", label: "Alle", count: t.checks.length }]} />
          </div>
          {rows.length === 0 ? <EmptyState compact icon="checkCircle" title="Keine Auffälligkeiten" /> : rows.map((c) => <Row key={c.key} c={c} />)}
        </>
      )}
    </Card>
  );
}
