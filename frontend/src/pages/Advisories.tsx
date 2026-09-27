import { useState } from "react";
import { Button, Card, EmptyState, ErrorBox, IconButton, Input, Loading, Modal, Notice, PageHeader, Pill, Select, Textarea, ToggleField, useAction } from "../components/ui";
import { SEV_LABEL, SEV_TONE } from "../lib/advisories";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { useFetch } from "../lib/useFetch";

interface Adv {
  id?: string; cve: string; title: string; description: string | null; function: string; severity: string; affected_from: string;
  affected_to: string | null; fixed_in: string | null; link: string | null; enabled: boolean; builtin?: boolean; affected?: number; possible?: number;
}
const FN_LABEL: Record<string, string> = { general: "allgemein", hotspot: "Hotspot", wlan: "WLAN", vrrp: "VRRP", wireguard: "WireGuard", dns: "DNS", "rest-api": "REST-API", api: "API", winbox: "WinBox", www: "WebFig/HTTP", ssh: "SSH", other: "sonstige" };
const EMPTY: Adv = { cve: "", title: "", description: null, function: "general", severity: "high", affected_from: "", affected_to: null, fixed_in: null, link: null, enabled: true };

/** Sicherheitsmeldungen: vom MSP gepflegt (kein Scraping), Betroffenheit je Version und aktiver Funktion. */
export default function Advisories() {
  const { me } = useAuth();
  const msp = !!me?.user.is_superuser;
  const data = useFetch<{ functions: string[]; severities: string[]; advisories: Adv[] }>("/advisories");
  const [edit, setEdit] = useState<Adv | null>(null);
  const { busy, error, run } = useAction();
  return (
    <>
      <PageHeader title="Sicherheitsmeldungen" subtitle="Bekannte RouterOS-Schwachstellen mit betroffenen Versionen – betroffen ist ein Gerät, wenn die Version im Bereich liegt und die Funktion aktiv ist."
        actions={msp && <Button icon="plus" onClick={() => setEdit({ ...EMPTY })}>Meldung</Button>} />
      <ErrorBox error={error ?? data.error} />
      <div className="mb-3"><Notice tone="blue" icon="info">Die Liste pflegt der MSP (Quelle z. B. MikroTik-Security-Advisories). Meldungen mit Schweregrad hoch/kritisch blockieren das Ausrollen der betroffenen Funktion, bis die Firmware aktualisiert ist.</Notice></div>
      <Card flush>
        {!data.data ? <Loading rows={3} /> : data.data.advisories.length === 0 ? <EmptyState title="Keine Meldungen" /> : (
          <table className="w-full text-sm">
            <thead><tr className="border-b border-line bg-panel2 text-left text-xs text-fg3"><th className="px-4 py-2 font-medium">Kennung</th><th className="px-2 font-medium">Titel</th><th className="px-2 font-medium">Funktion</th><th className="px-2 font-medium">Schweregrad</th><th className="px-2 font-medium">Versionen</th><th className="px-2 font-medium">Geräte</th><th /></tr></thead>
            <tbody>{data.data.advisories.map((a) => (
              <tr key={a.id} className={`border-b border-line last:border-b-0 ${a.enabled ? "" : "opacity-60"}`}>
                <td className="px-4 py-2 font-mono">{a.link ? <a href={a.link} target="_blank" rel="noreferrer" className="hover:underline">{a.cve}</a> : a.cve}{a.builtin && <Pill tone="gray">Beispiel</Pill>}</td>
                <td className="px-2 py-2">{a.title}{!a.enabled && <span className="ml-1 text-xs text-fg3">(deaktiviert)</span>}</td>
                <td className="px-2 py-2">{FN_LABEL[a.function] ?? a.function}</td>
                <td className="px-2 py-2"><Pill tone={SEV_TONE[a.severity]}>{SEV_LABEL[a.severity]}</Pill></td>
                <td className="px-2 py-2 font-mono text-xs">ab {a.affected_from}{a.affected_to ? ` bis ${a.affected_to}` : ""}{a.fixed_in ? ` · behoben ${a.fixed_in}` : ""}</td>
                <td className="px-2 py-2">{a.affected ? <Pill tone="red">{a.affected} betroffen</Pill> : <span className="text-fg3">0</span>}{a.possible ? <span className="ml-1 text-xs text-fg3">+{a.possible} möglich</span> : null}</td>
                <td className="px-2 py-2 text-right">{msp && !a.builtin && <div className="flex justify-end gap-1">
                  <IconButton icon="edit" label="Bearbeiten" onClick={() => setEdit(a)} />
                  <IconButton icon="trash" label="Löschen" onClick={() => { if (confirm(`${a.cve} löschen?`)) void run(async () => { await api.del(`/advisories/${a.id}`); await data.reload(); }); }} />
                </div>}</td>
              </tr>))}</tbody>
          </table>
        )}
      </Card>
      {edit && (
        <Modal open wide onClose={() => setEdit(null)} title={edit.id ? edit.cve : "Neue Sicherheitsmeldung"}
          footer={<><Button variant="secondary" onClick={() => setEdit(null)}>Abbrechen</Button><Button disabled={busy || !edit.cve || !edit.title || !edit.affected_from} onClick={() => void run(async () => {
            const body = { ...edit, affected_to: edit.affected_to || null, fixed_in: edit.fixed_in || null, link: edit.link || null };
            if (edit.id) await api.put(`/advisories/${edit.id}`, body); else await api.post("/advisories", body);
            setEdit(null); await data.reload();
          })}>Speichern</Button></>}>
          <ErrorBox error={error} />
          <div className="grid gap-3 sm:grid-cols-2">
            <Input label="Kennung (CVE-…)" value={edit.cve} onChange={(e) => setEdit({ ...edit, cve: e.target.value })} />
            <Input label="Titel" value={edit.title} onChange={(e) => setEdit({ ...edit, title: e.target.value })} />
            <Select label="Betroffene Funktion" value={edit.function} onChange={(e) => setEdit({ ...edit, function: e.target.value })}>{(data.data?.functions ?? []).map((f) => <option key={f} value={f}>{FN_LABEL[f] ?? f}</option>)}</Select>
            <Select label="Schweregrad" value={edit.severity} onChange={(e) => setEdit({ ...edit, severity: e.target.value })}>{(data.data?.severities ?? []).map((s) => <option key={s} value={s}>{SEV_LABEL[s]}</option>)}</Select>
            <Input label="Betroffen ab Version" placeholder="z. B. 7.10" value={edit.affected_from} onChange={(e) => setEdit({ ...edit, affected_from: e.target.value })} />
            <Input label="Behoben in (erste sichere Version)" placeholder="z. B. 7.16.1" value={edit.fixed_in ?? ""} onChange={(e) => setEdit({ ...edit, fixed_in: e.target.value })} />
            <Input label="Betroffen bis (inklusive, optional)" value={edit.affected_to ?? ""} onChange={(e) => setEdit({ ...edit, affected_to: e.target.value })} />
            <Input label="Link (Quelle)" value={edit.link ?? ""} onChange={(e) => setEdit({ ...edit, link: e.target.value })} />
          </div>
          <div className="mt-3"><Textarea label="Beschreibung" rows={3} value={edit.description ?? ""} onChange={(e) => setEdit({ ...edit, description: e.target.value || null })} /></div>
          <div className="mt-3"><ToggleField label="Aktiv" checked={edit.enabled} onChange={(v) => setEdit({ ...edit, enabled: v })} /></div>
        </Modal>
      )}
    </>
  );
}
