import { useState } from "react";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtDate } from "../lib/format";
import { LA_STATUS, type LocalAccess } from "../lib/localAccess";
import type { Device } from "../lib/types";
import { useFetch } from "../lib/useFetch";
import { Button, Card, Checkbox, CodeBlock, ErrorBox, Input, Modal, Notice, Pill, Textarea, useAction } from "./ui";

/** Gerätedetail: Vor-Ort-Zugang (Break-Glass) – Status, Netze, Anlegen, Anzeige mit Begründung, Rotation. */
export default function LocalAccessCard({ device }: { device: Device }) {
  const { can } = useAuth();
  const q = useFetch<LocalAccess | null>(`/devices/${device.id}/local-access`);
  const [edit, setEdit] = useState(false);
  const [reveal, setReveal] = useState(false);
  const { busy, error, run } = useAction();
  const la = q.data;
  const [label, tone] = LA_STATUS[la?.status ?? "none"];
  const admin = can("admin");
  return (
    <Card title="Vor-Ort-Zugang" subtitle="Lokaler Notfall-Benutzer – nur aus LAN/Management bzw. Service-Port, nie aus dem WAN"
      actions={<Pill tone={la?.status === "active" && la.restricted ? "orange" : tone}>{la?.status === "active" && la.restricted ? "aktiv · eingeschränkt" : label}</Pill>}>
      <ErrorBox error={error ?? q.error} />
      {la?.status === "not_created" && <div className="mb-3"><Notice tone="orange" icon="alert" title="Nicht angelegt">{la.reason} {admin && "Über „Netze / Service-Port“ erlaubte Netze manuell angeben."}</Notice></div>}
      {la?.status === "error" && <div className="mb-3"><Notice tone="red" icon="alert" title="Fehler">{la.reason}</Notice></div>}
      {la?.status === "active" && la.reason && <div className="mb-3"><Notice tone="orange" icon="alert">{la.reason}</Notice></div>}
      {la?.status === "active" && la.restricted && (
        <div className="mb-3"><Notice tone="orange" icon="alert" title={`Vor-Ort-Zugang eingeschränkt: fehlt ${la.missing_policies.join("/")} – vollständig nur per Onboarding oder mit Terminal-Befehl`}>
          Der Zugang wurde nachträglich über den API-Benutzer angelegt; dieser darf keine Gruppe mit mehr Rechten anlegen, als er selbst hat
          (z. B. kein Konsolen-Login ohne <span className="font-mono">local</span>). Zum Nachrüsten den Befehl einmal im Terminal des Routers als Admin ausführen:
          {la.full_group_command && <div className="mt-2"><CodeBlock text={la.full_group_command} highlight={false} /></div>}
          <span className="mt-1 block text-xs">Danach „Erneut abgleichen“ – der Hinweis verschwindet.</span>
        </Notice></div>
      )}
      {!la && <p className="text-sm text-fg2">Für dieses Gerät ist noch kein Vor-Ort-Zugang angelegt.</p>}
      {la && (
        <dl className="grid grid-cols-[160px_1fr] gap-x-4 gap-y-1.5 text-sm">
          <dt className="text-fg3">Benutzer</dt><dd className="font-mono">{la.username}</dd>
          <dt className="text-fg3">Erlaubte Netze</dt><dd className="font-mono text-xs">{la.networks.join(", ") || "–"}</dd>
          <dt className="text-fg3">Interfaces</dt><dd className="font-mono text-xs">{la.interfaces.join(", ") || "–"}</dd>
          {la.manual_networks.length > 0 && <><dt className="text-fg3">Manuell</dt><dd className="font-mono text-xs">{la.manual_networks.join(", ")}</dd></>}
          <dt className="text-fg3">Service-Port</dt><dd>{la.service_port?.enabled ? <span className="font-mono text-xs">{la.service_port.interface} · {la.service_port.network}</span> : "aus"}</dd>
          <dt className="text-fg3">Passwort gesetzt</dt><dd>{fmtDate(la.password_set_at)}{la.viewed_at && <span className="text-fg3"> · zuletzt angezeigt {fmtDate(la.viewed_at)}</span>}</dd>
          {la.rotate_due_at && <><dt className="text-fg3">Rotation fällig</dt><dd>{fmtDate(la.rotate_due_at)}</dd></>}
        </dl>
      )}
      {admin && (
        <div className="mt-4 flex flex-wrap gap-2">
          {la?.status === "active" && la.restricted && <Button variant="secondary" icon="rotate" disabled={busy || device.status === "offline"} onClick={() => void run(async () => { await api.post(`/devices/${device.id}/local-access`, {}); await q.reload(); })}>Erneut abgleichen</Button>}
          {(!la || la.status !== "active") && <Button icon="plus" disabled={busy || device.status === "offline"} onClick={() => void run(async () => { await api.post(`/devices/${device.id}/local-access`, {}); await q.reload(); })}>{la ? "Erneut anlegen" : "Anlegen"}</Button>}
          {la?.has_password && <Button variant="secondary" icon="key" onClick={() => setReveal(true)}>Passwort anzeigen …</Button>}
          {la?.status === "active" && <Button variant="secondary" icon="rotate" disabled={busy} onClick={() => confirm("Neues Passwort für den Vor-Ort-Benutzer setzen?") && void run(async () => { await api.post(`/devices/${device.id}/local-access/rotate`); await q.reload(); })}>Rotieren</Button>}
          <Button variant="secondary" icon="settings" onClick={() => setEdit(true)}>Netze / Service-Port …</Button>
          {la && la.status !== "disabled" && <Button variant="ghost" icon="trash" disabled={busy} onClick={() => confirm("Vor-Ort-Zugang entfernen? MAC-WinBox und Dienste werden zurückgestellt.") && void run(async () => { await api.del(`/devices/${device.id}/local-access`); await q.reload(); })}>Entfernen</Button>}
        </div>
      )}
      {edit && <EditDialog device={device} la={la ?? null} onClose={() => setEdit(false)} onDone={() => { setEdit(false); void q.reload(); }} />}
      {reveal && <RevealDialog device={device} onClose={() => { setReveal(false); void q.reload(); }} />}
    </Card>
  );
}

function EditDialog({ device, la, onClose, onDone }: { device: Device; la: LocalAccess | null; onClose: () => void; onDone: () => void }) {
  const [nets, setNets] = useState((la?.manual_networks ?? []).join("\n"));
  const [sp, setSp] = useState({ enabled: !!la?.service_port?.enabled, interface: la?.service_port?.interface ?? "", network: la?.service_port?.network ?? "192.168.254.0/29" });
  const { busy, error, run } = useAction();
  return (
    <Modal open onClose={onClose} title="Vor-Ort-Zugang: Netze und Service-Port" size="lg"
      footer={<><Button variant="secondary" onClick={onClose}>Abbrechen</Button>
        <Button disabled={busy} onClick={() => void run(async () => {
          await api.post(`/devices/${device.id}/local-access`, { manual_networks: nets.split(/[\s,]+/).filter(Boolean), service_port: sp });
          onDone();
        })}>{busy ? "Übernehme …" : "Speichern und anlegen"}</Button></>}>
      <ErrorBox error={error} />
      <div className="flex flex-col gap-4">
        <Textarea label="Zusätzlich erlaubte Netze (CIDR, je Zeile)" rows={3} value={nets} onChange={(e) => setNets(e.target.value)} className="font-mono"
          hint="Nur nötig, wenn keine lokalen Netze ermittelt werden (Zonen Management/LAN oder defconf-Liste LAN). Nicht erlaubt: 0.0.0.0/0 und Netze der WAN-Interfaces." />
        <div className="rounded-md border border-line p-3">
          <Checkbox label="Service-Port einrichten (optional)" checked={sp.enabled} onChange={(v) => setSp({ ...sp, enabled: v })} />
          <p className="mt-1 text-xs text-fg3">Der gewählte Ethernet-Port wird aus der Bridge genommen und bekommt ein eigenes kleines Netz mit DHCP – dort angeschlossene Notebooks erreichen den Router per WinBox/SSH, auch wenn das LAN gestört ist. Standard-Netz ist ein änderbarer Vorschlag.</p>
          {sp.enabled && <div className="mt-3 grid grid-cols-2 gap-3">
            <Input label="Ethernet-Port" placeholder="z. B. ether5" value={sp.interface} onChange={(e) => setSp({ ...sp, interface: e.target.value })} />
            <Input label="Netz" value={sp.network} onChange={(e) => setSp({ ...sp, network: e.target.value })} />
          </div>}
        </div>
      </div>
    </Modal>
  );
}

function RevealDialog({ device, onClose }: { device: Device; onClose: () => void }) {
  const [reason, setReason] = useState("");
  const [res, setRes] = useState<{ username: string; password: string; networks: string[]; rotate_due_at: string | null } | null>(null);
  const { busy, error, run } = useAction();
  return (
    <Modal open onClose={onClose} title={`Vor-Ort-Passwort – ${device.name}`}
      footer={res ? <Button onClick={onClose}>Schließen</Button> : <><Button variant="secondary" onClick={onClose}>Abbrechen</Button>
        <Button disabled={busy || reason.trim().length < 5} onClick={() => void run(async () => setRes(await api.post(`/devices/${device.id}/local-access/reveal`, { reason })))}>Anzeigen</Button></>}>
      <ErrorBox error={error} />
      {res ? (
        <div className="flex flex-col gap-3">
          <div className="text-sm">Benutzer <span className="font-mono">{res.username}</span> · nur aus {res.networks.join(", ")}</div>
          <CodeBlock text={res.password} highlight={false} />
          {res.rotate_due_at && <Notice tone="blue">Das Passwort wird am {new Date(res.rotate_due_at).toLocaleString("de-DE")} automatisch rotiert.</Notice>}
        </div>
      ) : (
        <div className="flex flex-col gap-3">
          <Notice tone="orange" icon="alert">Die Anzeige wird mit Begründung im Audit-Log protokolliert und per Webhook gemeldet.</Notice>
          <Input label="Begründung" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="z. B. Vor-Ort-Einsatz, Ticket 1234" autoFocus />
        </div>
      )}
    </Modal>
  );
}
