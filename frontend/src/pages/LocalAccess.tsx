import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Button, Card, Checkbox, ErrorBox, Input, Loading, Notice, PageHeader, Pill, RowCheck, Segment, Table, useAction } from "../components/ui";
import { api, saveBlob } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtDate } from "../lib/format";
import { LA_STATUS, type LocalAccessRow } from "../lib/localAccess";
import { useFetch } from "../lib/useFetch";

interface Settings {
  local_admin_name: string; local_admin_address_restrict: boolean; local_access_auto: boolean; local_access_rotate_days: number | null;
  local_access_rotate_after_view: boolean; local_access_age_recipient: string; local_access_webhook_url: string | null;
}

/** Vor-Ort-Zugang (Break-Glass): Übersicht, Massenaktion, Einstellungen je Mandant, verschlüsselter Export. */
export default function LocalAccess() {
  const { can } = useAuth();
  const rows = useFetch<LocalAccessRow[]>("/local-access");
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [result, setResult] = useState<{ device: string; status: string; reason?: string }[] | null>(null);
  const { busy, error, run } = useAction();
  const list = rows.data ?? [];
  const missing = list.filter((r) => r.access?.status !== "active");
  return (
    <>
      <PageHeader title="Vor-Ort-Zugang" subtitle="Lokaler Notfall-Benutzer je Router – eigenes Passwort je Gerät, nur aus LAN/Management bzw. Service-Port" />
      <div className="mb-4"><Notice tone="orange" icon="alert" title="Vor-Ort-Passwörter müssen auch ohne Plattform verfügbar sein">
        Exportieren Sie die Passwörter regelmäßig verschlüsselt in Ihren Passwort-Tresor (KeePass-kompatible CSV) – oder richten Sie den automatischen
        Export per Webhook ein. Fällt die Plattform aus, ist der Export der einzige Weg zu den Passwörtern.</Notice></div>
      <ErrorBox error={error ?? rows.error} />
      <Card title={`Geräte (${list.length - missing.length} von ${list.length} aktiv)`} flush
        actions={can("admin") && <Button icon="plus" disabled={!sel.size || busy} onClick={() => void run(async () => {
          const r = await api.post<{ results: { device: string; status: string; reason?: string }[] }>("/local-access/bulk", { device_ids: [...sel] });
          setResult(r.results); setSel(new Set()); await rows.reload();
        })}>{busy ? "Lege an …" : `Auf ${sel.size} Geräten anlegen`}</Button>}>
        {!rows.data ? <Loading rows={4} /> : (
          <Table head={[<RowCheck key="all" label="Alle ohne aktiven Zugang wählen" checked={missing.length > 0 && missing.every((r) => sel.has(r.device_id))} onChange={(v) => setSel(v ? new Set(missing.map((r) => r.device_id)) : new Set())} />,
            "Gerät", "Status", "Benutzer", "Erlaubte Netze", "Passwort gesetzt", "Grund"]} empty={list.length === 0} emptyText="Keine verbundenen Geräte">
            {list.map((r) => {
              const [label, tone] = LA_STATUS[r.access?.status ?? "none"];
              return (
                <tr key={r.device_id}>
                  <td className="px-3 py-2"><RowCheck label={`${r.device} auswählen`} checked={sel.has(r.device_id)} onChange={(v) => { const n = new Set(sel); if (v) n.add(r.device_id); else n.delete(r.device_id); setSel(n); }} /></td>
                  <td className="px-3 py-2 font-medium"><Link to={`/devices/${r.device_id}`} className="text-blue-text hover:underline">{r.device}</Link></td>
                  <td className="px-3 py-2"><Pill tone={tone}>{label}</Pill></td>
                  <td className="px-3 py-2 font-mono text-xs">{r.access?.username ?? "–"}</td>
                  <td className="px-3 py-2 font-mono text-xs">{r.access?.networks.join(", ") || "–"}</td>
                  <td className="px-3 py-2 text-fg2">{fmtDate(r.access?.password_set_at)}</td>
                  <td className="max-w-[320px] px-3 py-2 text-xs text-fg2">{r.access?.status !== "active" ? r.access?.reason : ""}</td>
                </tr>
              );
            })}
          </Table>
        )}
      </Card>
      {result && <Card title="Ergebnis der Massenaktion" className="mt-4">
        <ul className="flex flex-col gap-1 text-sm">{result.map((r) => <li key={r.device}><Pill tone={LA_STATUS[r.status]?.[1] ?? "gray"}>{LA_STATUS[r.status]?.[0] ?? r.status}</Pill> <span className="font-medium">{r.device}</span> <span className="text-fg3">{r.reason}</span></li>)}</ul>
      </Card>}
      {can("admin") && <div className="mt-4 grid gap-4 lg:grid-cols-2"><ExportCard /><SettingsCard /></div>}
    </>
  );
}

function ExportCard() {
  const [fmt, setFmt] = useState<"age" | "zip">("age");
  const [pw, setPw] = useState("");
  const [rcpt, setRcpt] = useState("");
  const { busy, error, run } = useAction();
  return (
    <Card title="Export (KeePass-CSV, verschlüsselt)" subtitle="Spalten Group, Title, Username, Password, URL, Notes – nie unverschlüsselt">
      <ErrorBox error={error} />
      <div className="flex flex-col gap-3">
        <Segment label="Format" value={fmt} onChange={setFmt} options={[{ value: "age", label: "age (Public Key)" }, { value: "zip", label: "ZIP (AES, Passwort)" }]} />
        {fmt === "age"
          ? <Input label="age-Empfänger" placeholder="age1… (leer = aus den Einstellungen)" value={rcpt} onChange={(e) => setRcpt(e.target.value)} className="font-mono" />
          : <Input label="ZIP-Passwort (mind. 12 Zeichen)" type="password" value={pw} onChange={(e) => setPw(e.target.value)} autoComplete="new-password" />}
        <div><Button icon="download" disabled={busy} onClick={() => void run(async () => {
          const blob = await api.postBlob("/local-access/export", fmt === "age" ? { format: "age", recipient: rcpt || null } : { format: "zip", password: pw });
          saveBlob(blob, fmt === "age" ? "vor-ort-zugang.csv.age" : "vor-ort-zugang.zip");
        })}>Exportieren</Button></div>
        <p className="text-xs text-fg3">ZIP mit AES-256 öffnen z. B. 7-Zip; age-Dateien mit <span className="font-mono">age -d -i schluessel.txt</span>. Jeder Export steht im Audit-Log.</p>
      </div>
    </Card>
  );
}

function SettingsCard() {
  const q = useFetch<Settings>("/local-access/settings");
  const [f, setF] = useState<Settings | null>(null);
  const [hook, setHook] = useState("");
  const { busy, error, run } = useAction();
  const [saved, setSaved] = useState(false);
  useEffect(() => { if (q.data) setF(q.data); }, [q.data]);
  if (!f) return <Card title="Einstellungen"><Loading rows={3} /></Card>;
  return (
    <Card title="Einstellungen (Mandant)">
      <ErrorBox error={error} />
      <div className="flex flex-col gap-3">
        <Input label="Benutzername auf den Routern" value={f.local_admin_name} onChange={(e) => setF({ ...f, local_admin_name: e.target.value })} hint="Gilt für neu angelegte Zugänge. Standard: localadmin" />
        <Checkbox label="Beim Onboarding/Zero-Touch automatisch anlegen" checked={f.local_access_auto} onChange={(v) => setF({ ...f, local_access_auto: v })} />
        <div>
          <Checkbox label="Adressbeschränkung des Vor-Ort-Benutzers (address= lokale Netze)" checked={f.local_admin_address_restrict} onChange={(v) => setF({ ...f, local_admin_address_restrict: v })} />
          <p className="ml-6 mt-0.5 text-xs text-fg3">Aus: Benutzer ohne Adressbeschränkung; WinBox/SSH werden dann über die Dienst-Adressen auf lokale Netze und Tunnel beschränkt. Der Schutz aus dem WAN bleibt über Firewall und MAC-Server-Liste bestehen. Gilt beim nächsten Anlegen/Abgleich.</p>
        </div>
        <Checkbox label="Nach jeder Anzeige automatisch rotieren (nach 4 Stunden)" checked={f.local_access_rotate_after_view} onChange={(v) => setF({ ...f, local_access_rotate_after_view: v })} />
        <Input label="Rotation alle … Tage (leer = aus)" type="number" min={1} value={f.local_access_rotate_days ?? ""} onChange={(e) => setF({ ...f, local_access_rotate_days: e.target.value ? Number(e.target.value) : null })} />
        <Input label="age-Empfänger für Exporte" placeholder="age1…" value={f.local_access_age_recipient} onChange={(e) => setF({ ...f, local_access_age_recipient: e.target.value })} className="font-mono" />
        <Input label="Automatischer Export per Webhook (nach Anlegen/Rotation, nur age-verschlüsselt)" placeholder={f.local_access_webhook_url ?? "https://…"} value={hook} onChange={(e) => setHook(e.target.value)}
          hint={f.local_access_webhook_url ? <>Aktuell: {f.local_access_webhook_url} · <button type="button" className="text-blue-text hover:underline" onClick={() => void run(async () => { setF(await api.put("/local-access/settings", { ...f, local_access_webhook_url: "" })); })}>entfernen</button></> : "Leer lassen = kein automatischer Export"} />
        <div className="flex items-center gap-3"><Button disabled={busy} onClick={() => void run(async () => {
          setF(await api.put<Settings>("/local-access/settings", { ...f, local_access_webhook_url: hook || null })); setHook(""); setSaved(true);
        })}>Speichern</Button>{saved && <span className="text-sm text-green-text">Gespeichert</span>}</div>
      </div>
    </Card>
  );
}
