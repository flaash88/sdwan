import { useEffect, useMemo, useState } from "react";
import { Button, Card, EmptyState, ErrorBox, IconButton, Loading, Segment, cls, useAction } from "../components/ui";
import { api, download } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtBytes, fmtFull } from "../lib/format";
import type { Device } from "../lib/types";
import { useFetch } from "../lib/useFetch";

interface Backup { id: string; trigger: string; created_by: string | null; sha256: string; routeros_version: string | null; size: number; pinned: boolean; note: string | null; created_at: string; added: number; removed: number; previous_id: string | null }
interface Diff { from: string | null; to: string; added: number; removed: number; lines: string[] }

const TRIGGER: Record<string, string> = { scheduled: "Geplant", manual: "Manuell", "pre-update": "Vor Firmware-Update", "post-policy": "Nach Policy-Push" };
const why = (b: Backup) => [TRIGGER[b.trigger] ?? b.trigger, b.note].filter(Boolean).join(" · ");
const COLS = "grid-cols-[64px_150px_minmax(150px,1.2fr)_minmax(120px,1fr)_130px_110px_70px_100px_40px]";

/** SHA-256 gekürzt; Klick kopiert den vollen Wert. */
function Checksum({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button type="button" title={`SHA-256: ${value}\nKlicken zum Kopieren`} aria-label={`Prüfsumme ${value} kopieren`}
      className="cursor-pointer truncate text-left font-mono text-xs text-fg2 hover:text-fg"
      onClick={() => void navigator.clipboard?.writeText(value).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); })}>
      {copied ? "kopiert" : value.slice(0, 12)}
    </button>
  );
}

type Side = { n: number | ""; text: string; mark: "" | "-" | "+" };
type Row = { l: Side; r: Side; kind: "ctx" | "chg" | "gap" };

/** Unified Diff -> Zeilen nebeneinander (links alt, rechts neu); Löschungen/Hinzufügungen werden gepaart. */
function sideBySide(lines: string[]): Row[] {
  const rows: Row[] = [];
  let ln = 0, rn = 0;
  let del: string[] = [], add: string[] = [];
  const flush = () => {
    for (let k = 0; k < Math.max(del.length, add.length); k++) {
      const d = del[k], a = add[k];
      rows.push({ kind: "chg", l: d != null ? { n: ++ln, text: d, mark: "-" } : { n: "", text: "", mark: "" }, r: a != null ? { n: ++rn, text: a, mark: "+" } : { n: "", text: "", mark: "" } });
    }
    del = []; add = [];
  };
  for (const l of lines) {
    if (l.startsWith("---") || l.startsWith("+++")) continue;
    if (l.startsWith("@@")) {
      flush();
      const m = l.match(/-(\d+)(?:,\d+)? \+(\d+)/);
      if (m) { if (rows.length) rows.push({ kind: "gap", l: { n: "", text: "…", mark: "" }, r: { n: "", text: "…", mark: "" } }); ln = Number(m[1]) - 1; rn = Number(m[2]) - 1; }
      continue;
    }
    if (l.startsWith("-")) del.push(l.slice(1));
    else if (l.startsWith("+")) add.push(l.slice(1));
    else { flush(); const t = l.slice(1); rows.push({ kind: "ctx", l: { n: ++ln, text: t, mark: "" }, r: { n: ++rn, text: t, mark: "" } }); }
  }
  flush();
  return rows;
}

function Chip({ label, on, blue, onClick, title }: { label: string; on: boolean; blue?: boolean; onClick: () => void; title: string }) {
  return (
    <button type="button" aria-pressed={on} title={title} aria-label={title} onClick={onClick}
      className={cls("flex h-[22px] w-[22px] cursor-pointer items-center justify-center rounded-[5px] border text-[11px] font-semibold",
        on ? (blue ? "border-blue bg-blue text-white" : "border-fg bg-fg text-panel") : "border-line-strong bg-panel text-fg3 hover:text-fg")}>{label}</button>
  );
}

export default function BackupsTab({ device }: { device: Device }) {
  const { can } = useAuth();
  const list = useFetch<Backup[]>(`/devices/${device.id}/backups`);
  const [a, setA] = useState<string | null>(null);
  const [b, setB] = useState<string | null>(null);
  const [view, setView] = useState<"diff" | "content">("diff");
  const { busy, error, run } = useAction();
  const items = list.data ?? [];
  // Standard: B = neuester Stand, A = dessen Vorgänger
  useEffect(() => {
    if (!items.length) return;
    if (!b || !items.some((x) => x.id === b)) setB(items[0].id);
    if (!a || !items.some((x) => x.id === a)) setA(items[0].previous_id && items.some((x) => x.id === items[0].previous_id) ? items[0].previous_id : items[1]?.id ?? null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [list.data]);
  const diff = useFetch<Diff>(b && a && a !== b && view === "diff" ? `/backups/${b}/diff?against=${a}&context=3` : null);
  const full = useFetch<{ content: string }>(b && view === "content" ? `/backups/${b}` : null);
  const byId = (id: string | null) => items.find((x) => x.id === id);
  const left = byId(diff.data?.from ?? a), right = byId(diff.data?.to ?? b);
  const rows = useMemo(() => sideBySide(diff.data?.lines ?? []), [diff.data]);

  return (
    <>
      <ErrorBox error={error ?? list.error} />
      <Card flush title="Konfig-Stände" subtitle={`${items.length} gespeichert · täglich (nur bei Änderungen), manuell, vor Firmware-Updates und nach Policy-Push`}
        actions={<>
          <span className="hidden text-xs text-fg3 md:inline">A und B zum Vergleich wählen</span>
          {can("technician") && <Button size="sm" icon="archive" disabled={busy || device.status !== "online"} onClick={() => void run(async () => { await api.post(`/devices/${device.id}/backups`); await list.reload(); setB(null); setA(null); })}>{busy ? "Backup läuft …" : "Backup jetzt"}</Button>}
        </>}>
        {!list.data ? <Loading rows={4} /> : items.length === 0 ? <EmptyState compact title="Noch keine Backups" text="Das erste Backup entsteht in der nächsten Nacht oder mit „Backup jetzt“." /> : (
          <div className="overflow-x-auto">
            <div className="min-w-[1080px]">
              <div className={cls("grid gap-3 border-b border-line bg-panel2 px-4 py-2 text-xs font-medium text-fg3", COLS)}>
                <span>Vergleich</span><span>Zeitpunkt</span><span>Auslöser</span><span>Erstellt von</span><span>RouterOS</span><span>Prüfsumme</span><span>Größe</span><span>Änderung</span><span />
              </div>
              {items.map((x) => (
                <div key={x.id} className={cls("grid h-[42px] items-center gap-3 border-b border-line px-4 last:border-b-0", COLS, (x.id === a || x.id === b) && "bg-panel2")}>
                  <span className="flex gap-1">
                    <Chip label="A" title={`Stand ${fmtFull(x.created_at)} als A wählen`} on={x.id === a} onClick={() => { setA(x.id); setView("diff"); }} />
                    <Chip label="B" blue title={`Stand ${fmtFull(x.created_at)} als B wählen`} on={x.id === b} onClick={() => setB(x.id)} />
                  </span>
                  <span className="font-mono text-xs">{fmtFull(x.created_at)}</span>
                  <span className="truncate" title={why(x)}>{why(x)}</span>
                  <span className={cls("truncate", !x.created_by || x.created_by === "unbekannt" ? "text-fg3" : x.created_by === "system" ? "text-fg2" : "")} title={x.created_by ?? "unbekannt"}>
                    {!x.created_by || x.created_by === "unbekannt" ? "unbekannt" : x.created_by === "system" ? "System" : x.created_by}
                  </span>
                  <span className="truncate font-mono text-xs text-fg2">{x.routeros_version ?? "–"}</span>
                  <Checksum value={x.sha256} />
                  <span className="text-fg2">{fmtBytes(x.size)}</span>
                  <span className="text-xs">{x.previous_id ? <><span className="text-green-text">+{x.added}</span> / <span className="text-red-text">−{x.removed}</span></> : <span className="text-fg3">erster Stand</span>}</span>
                  <span className="flex justify-end"><IconButton icon="download" label={`Stand ${fmtFull(x.created_at)} als .rsc herunterladen`} onClick={() => void download(`/backups/${x.id}/download`, `${device.name}-${x.created_at.slice(0, 16).replace(/[:T]/g, "-")}.rsc`)} /></span>
                </div>
              ))}
            </div>
          </div>
        )}
      </Card>

      {items.length > 0 && (
        <section className="overflow-hidden rounded-lg border border-line bg-panel">
          <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-2.5">
            <Segment label="Ansicht" value={view} onChange={setView} options={[{ value: "diff", label: "Vergleich A ↔ B" }, { value: "content", label: "Vollständiger Stand B" }]} />
          </div>
          {view === "content" ? (
            full.data ? <pre className="m-0 max-h-[36rem] overflow-auto bg-code p-4 font-mono text-xs leading-[1.6]">{full.data.content}</pre> : <Loading rows={4} />
          ) : !a || a === b ? <EmptyState compact title="Zwei unterschiedliche Stände als A und B wählen" /> : !diff.data ? <Loading rows={4} /> : (
            <>
              <div className="grid grid-cols-2 border-b border-line">
                {[{ s: left, tag: diff.data.from === a ? "A" : "B", count: <span className="text-xs font-medium text-red-text">− {diff.data.removed} Zeilen</span> },
                  { s: right, tag: diff.data.to === b ? "B" : "A", count: <span className="text-xs font-medium text-green-text">+ {diff.data.added} Zeilen</span> }].map((h, i) => (
                  <div key={i} className={cls("flex min-w-0 items-center gap-2 px-4 py-2.5", i === 0 && "border-r border-line")}>
                    <span className={cls("flex h-5 w-5 shrink-0 items-center justify-center rounded-[5px] text-[11px] font-semibold", h.tag === "A" ? "bg-fg text-panel" : "bg-blue text-white")}>{h.tag}</span>
                    <span className="shrink-0 font-semibold">{h.s ? fmtFull(h.s.created_at) : "–"}</span>
                    <span className="truncate text-fg3">{h.s ? why(h.s) : ""}</span>
                    <div className="flex-1" />{h.count}
                  </div>
                ))}
              </div>
              {rows.length === 0 ? <EmptyState compact icon="checkCircle" title="Keine Unterschiede" /> : (
                <div className="max-h-[36rem] overflow-auto bg-code py-1 font-mono text-xs leading-[1.6]">
                  {rows.map((r, i) => (
                    <div key={i} className="grid grid-cols-[40px_18px_minmax(0,1fr)_40px_18px_minmax(0,1fr)]">
                      {[r.l, r.r].map((s, k) => {
                        const bg = s.mark === "-" ? "bg-red-bg" : s.mark === "+" ? "bg-green-bg" : r.kind === "chg" ? "bg-sunken" : "";
                        const sec = s.text.startsWith("/") && !s.mark;
                        return [
                          <span key={k + "n"} className={cls("pr-2 text-right text-fg3", bg, k === 1 && "border-l border-line")}>{s.n}</span>,
                          <span key={k + "m"} className={cls("text-center font-semibold", s.mark === "-" ? "text-red-text" : "text-green-text", bg)}>{s.mark === "-" ? "−" : s.mark}</span>,
                          <span key={k + "t"} className={cls("whitespace-pre-wrap break-all pr-3", bg, r.kind === "gap" ? "text-fg3" : sec ? "text-blue-text" : s.mark ? "text-fg" : "text-fg2")}>{s.text}</span>,
                        ];
                      })}
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </section>
      )}
    </>
  );
}
