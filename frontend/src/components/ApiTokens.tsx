import { useState } from "react";
import { api } from "../lib/api";
import { fmtDate } from "../lib/format";
import { useFetch } from "../lib/useFetch";
import { Button, CodeBlock, ErrorBox, Input, Modal, Notice, Pill, Segment, Table, useAction } from "./ui";

interface Token { id: string; name: string; prefix: string; scope: "role" | "read"; expires_at: string | null; created_at: string; last_used_at: string | null; last_used_ip: string | null; state: "active" | "expired" | "revoked" }

const STATE: Record<Token["state"], ["green" | "gray" | "red", string]> = { active: ["green", "aktiv"], expired: ["gray", "abgelaufen"], revoked: ["red", "widerrufen"] };

/** Profil → API-Tokens: erstellen (Klartext nur einmal), letzte Nutzung, widerrufen. */
export function ApiTokensDialog({ onClose }: { onClose: () => void }) {
  const q = useFetch<Token[]>("/auth/api-tokens");
  const [f, setF] = useState({ name: "", scope: "read" as "read" | "role", expires_in_days: 90 });
  const [created, setCreated] = useState<string | null>(null);
  const { busy, error, run } = useAction();
  return (
    <Modal open onClose={onClose} title="API-Tokens" size="lg" footer={<Button onClick={onClose}>Schließen</Button>}>
      <ErrorBox error={error ?? q.error} />
      <div className="flex flex-col gap-4">
        <p className="text-sm text-fg2">Für Skripte und Automatisierung (<span className="font-mono">Authorization: Bearer sdw_…</span>, Dokumentation unter <a href="/docs" target="_blank" rel="noreferrer" className="text-blue-text hover:underline">/docs</a>).
          Tokens haben höchstens die Rechte Ihrer Rolle; Vor-Ort-Passwörter, Zwei-Faktor und Token-Verwaltung sind per Token nie möglich. Schreibende Aufrufe stehen im Audit-Log.</p>
        {created && <Notice tone="green" title="Token erstellt – wird nur jetzt angezeigt"><CodeBlock text={created} highlight={false} /></Notice>}
        <div className="grid grid-cols-[1fr_auto_120px_auto] items-end gap-3">
          <Input label="Name" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder="z. B. Monitoring" />
          <Segment label="Rechte" value={f.scope} onChange={(v) => setF({ ...f, scope: v })} options={[{ value: "read", label: "nur lesen" }, { value: "role", label: "wie Rolle" }]} />
          <Input label="Gültig (Tage)" type="number" min={1} max={365} value={f.expires_in_days} onChange={(e) => setF({ ...f, expires_in_days: Number(e.target.value) })} />
          <Button disabled={busy || !f.name.trim()} onClick={() => void run(async () => {
            const r = await api.post<Token & { token: string }>("/auth/api-tokens", f); setCreated(r.token); setF({ ...f, name: "" }); await q.reload();
          })}>Erstellen</Button>
        </div>
        <Table head={["Name", "Präfix", "Rechte", "Gültig bis", "Zuletzt genutzt", "Status", ""]} empty={q.data?.length === 0} emptyText="Noch keine Tokens">
          {q.data?.map((t) => (
            <tr key={t.id}>
              <td className="px-3 py-2 font-medium">{t.name}</td>
              <td className="px-3 py-2 font-mono text-xs">{t.prefix}…</td>
              <td className="px-3 py-2">{t.scope === "read" ? "nur lesen" : "wie Rolle"}</td>
              <td className="px-3 py-2 text-fg2">{fmtDate(t.expires_at)}</td>
              <td className="px-3 py-2 text-fg2">{t.last_used_at ? <>{fmtDate(t.last_used_at)}<div className="font-mono text-[11px] text-fg3">{t.last_used_ip}</div></> : "nie"}</td>
              <td className="px-3 py-2"><Pill tone={STATE[t.state][0]}>{STATE[t.state][1]}</Pill></td>
              <td className="px-3 py-2 text-right">{t.state === "active" && <Button variant="ghost" onClick={() => confirm(`Token „${t.name}“ widerrufen?`) && void run(async () => { await api.del(`/auth/api-tokens/${t.id}`); await q.reload(); })}>Widerrufen</Button>}</td>
            </tr>
          ))}
        </Table>
      </div>
    </Modal>
  );
}
