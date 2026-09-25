import { useState } from "react";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtFull } from "../lib/format";
import { useMeta } from "../lib/meta";
import type { Device } from "../lib/types";
import { useFetch } from "../lib/useFetch";
import { Icon } from "./Icon";
import { Button, Card, CodeBlock, EmptyState, ErrorBox, Loading, Modal, Notice, Pill, Segment, cls, useAction, type Tone } from "./ui";

type St = "ok" | "warn" | "error";
interface Check {
  key: string; label: string; kind: "path" | "check"; status: St; command?: string; used_by?: string; ms?: number; rows?: number; count?: number | null;
  reachable?: boolean; missing?: string[]; action?: string; group?: string; missing_optional?: string[]; notes?: string[]; error?: string | null; expected?: string[]; sample_fields?: string[]; value?: unknown;
}
interface Selftest { status: St; ran_at: string; ran_by: string | null; duration_ms: number; checks: Check[]; summary?: Record<St, number> }

const TONE: Record<St, [string, Tone, "checkCircle" | "alert" | "xCircle"]> = {
  ok: ["OK", "green", "checkCircle"], warn: ["Warnung", "orange", "alert"], error: ["Fehler", "red", "xCircle"],
};
interface RestrictResult {
  status: "ok" | "unchanged" | "readback_mismatch" | "reverting"; previous_group: string; message?: string;
  scheduler: string; revert_after: string; api_user: string; selftest?: Selftest;
}

/** „Rechte einschränken“: Bestätigung und Ergebnis der Umstellung mit Totmannschaltung. */
function RestrictDialog({ device, onClose, onDone }: { device: Device; onClose: () => void; onDone: (r: RestrictResult) => void }) {
  const { busy, error, run } = useAction();
  return (
    <Modal open onClose={onClose} title="Rechte des API-Benutzers einschränken" subtitle={device.name}
      footer={<>
        <Button variant="secondary" onClick={onClose}>Abbrechen</Button>
        <Button icon={busy ? "loader" : "shield"} disabled={busy}
          onClick={() => void run(async () => { onDone(await api.post<RestrictResult>(`/devices/${device.id}/restrict-api-user`)); onClose(); })}>
          {busy ? "Stelle um …" : "Rechte einschränken"}</Button>
      </>}>
      <div className="flex flex-col gap-3">
        <p>Der API-Benutzer wird von seiner bisherigen Gruppe auf die Gruppe <span className="font-mono">sdwan-api</span> mit genau den nötigen Rechten umgestellt.</p>
        <Notice tone="blue" title="Totmannschaltung">
          Vorher legt die Plattform auf dem Router einen Scheduler an, der die bisherige Gruppe nach ca. 3 Minuten automatisch wiederherstellt.
          Er wird erst gelöscht, wenn eine neue Verbindung und der Selbsttest mit den neuen Rechten funktionieren.
        </Notice>
        <ErrorBox error={error} />
      </div>
    </Modal>
  );
}

function RestrictNotice({ r, onRetest, busy }: { r: RestrictResult; onRetest: () => void; busy: boolean }) {
  if (r.status === "ok" || r.status === "unchanged")
    return <Notice tone="green" title="Rechte eingeschränkt">Der API-Benutzer ist in der Gruppe <span className="font-mono">sdwan-api</span>{r.status === "ok" ? `, vorher „${r.previous_group}“. Die Totmannschaltung wurde entfernt.` : "."}</Notice>;
  if (r.status === "readback_mismatch")
    return <Notice tone="red" title="Nicht umgestellt">{r.message ?? "Die Gruppe hat nach dem Anlegen nicht die erwarteten Policies."} Der Benutzer bleibt in „{r.previous_group}“.</Notice>;
  return (
    <Notice tone="orange" title={`Rechte werden in ca. ${r.revert_after.replace("m", " Minuten")} automatisch zurückgestellt`}>
      <div className="flex flex-col gap-2">
        <span>{r.message}. Der Scheduler <span className="font-mono">{r.scheduler}</span> stellt die Gruppe „{r.previous_group}“ wieder her. Danach den Selbsttest erneut ausführen.</span>
        <span><Button size="sm" variant="secondary" icon="activity" disabled={busy} onClick={onRetest}>Selbsttest erneut ausführen</Button></span>
        <details className="text-xs">
          <summary className="cursor-pointer text-fg2">Letzte Rückfallebene, falls die automatische Rückstellung nicht greift</summary>
          <div className="mt-2"><CodeBlock text={`/user set [find name="${r.api_user}"] group="${r.previous_group}"`} highlight={false} /></div>
        </details>
      </div>
    </Notice>
  );
}

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
  const [restrictOpen, setRestrictOpen] = useState(false);
  const [restrict, setRestrict] = useState<RestrictResult | null>(null);
  const t = last.data;
  const rights = t?.checks.find((c) => c.key === "rights");
  const runTest = () => void run(async () => { last.setData(await api.post<Selftest>(`/devices/${device.id}/selftest`)); });
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
          onClick={runTest}>{busy ? "Läuft …" : "Selbsttest ausführen"}</Button>}
      </>}>
      {error && <div className="px-4 pt-3"><ErrorBox error={error} /></div>}
      {restrict && <div className="px-4 pt-3"><RestrictNotice r={restrict} onRetest={runTest} busy={busy} /></div>}
      {!restrict && rights?.action === "restrict_api_user" && (
        <div className="px-4 pt-3">
          <Notice tone="orange" title="API-Benutzer in Gruppe full – Umstellung empfohlen">
            <div className="flex flex-wrap items-center gap-3">
              <span className="flex-1">Mit „Rechte einschränken“ erhält der Benutzer die Gruppe <span className="font-mono">sdwan-api</span> mit genau den nötigen Rechten. Die bisherige Gruppe wird automatisch wiederhergestellt, falls danach etwas nicht funktioniert.</span>
              {can("technician") && <Button size="sm" icon="shield" disabled={device.status !== "online"} onClick={() => setRestrictOpen(true)}>Rechte einschränken</Button>}
            </div>
          </Notice>
        </div>
      )}
      {restrictOpen && <RestrictDialog device={device} onClose={() => setRestrictOpen(false)} onDone={(r) => { setRestrict(r); if (r.selftest) last.setData(r.selftest); }} />}
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
