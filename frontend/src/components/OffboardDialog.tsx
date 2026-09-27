import { useEffect, useState } from "react";
import { api } from "../lib/api";
import type { Device } from "../lib/types";
import { Button, ErrorBox, Input, Loading, Modal, Notice, Pill } from "./ui";

interface Preview { online: boolean; counts: Record<string, number>; final: Record<string, number>; warnings: string[]; defconf_disabled: number; error: string | null }
interface Step { step: number; label: string; ok: boolean; detail: unknown }

/** Gerät entfernen: Router bereinigen (Standard, nur online) oder nur aus der Plattform entfernen. */
export default function OffboardDialog({ device, onClose, onDone }: { device: Device; onClose: () => void; onDone: () => void }) {
  const [pre, setPre] = useState<Preview | null>(null);
  const [mode, setMode] = useState<"clean" | "platform_only">("clean");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [steps, setSteps] = useState<Step[] | null>(null);
  const [done, setDone] = useState(false);
  useEffect(() => {
    void api.get<Preview>(`/devices/${device.id}/offboarding/preview`).then((p) => { setPre(p); if (!p.online) setMode("platform_only"); }).catch((e: Error) => setError(e.message));
  }, [device.id]);
  const run = async () => {
    setBusy(true); setError(null);
    try {
      const r = await api.post<{ steps: Step[] }>(`/devices/${device.id}/offboard`, { mode, confirm_name: name });
      setSteps(r.steps); setDone(true);
    } catch (e) {
      // Abbruch: Detail kommt als JSON {message, steps}
      let msg = (e as Error).message;
      try {
        const d = JSON.parse(msg) as { message?: string; steps?: Step[] };
        if (d.steps) setSteps(d.steps);
        msg = d.message ?? msg;
      } catch { /* einfacher Text */ }
      setError(msg);
    } finally { setBusy(false); }
  };
  return (
    <Modal open size="lg" onClose={done ? onDone : onClose} title={`Gerät „${device.name}“ entfernen`}
      footer={done ? <Button onClick={onDone}>Fertig</Button> : <>
        <Button variant="secondary" onClick={onClose}>Abbrechen</Button>
        <Button variant="danger" icon="trash" disabled={busy || !pre || name !== device.name} onClick={() => void run()}>
          {busy ? "Läuft …" : mode === "clean" ? "Router bereinigen und entfernen" : "Nur aus der Plattform entfernen"}</Button>
      </>}>
      <ErrorBox error={error} />
      {!pre ? <Loading rows={3} /> : steps ? (
        <div className="flex flex-col gap-2">
          {done && <Notice tone="green" title="Gerät entfernt">{mode === "clean" ? "Der Router entfernt API-Benutzer, WAN-/Tunnel-Objekte und den Management-Tunnel in etwa einer Minute selbst." : "Der Router wurde nicht verändert."} Das Backup liegt 90 Tage im Offboarding-Archiv.</Notice>}
          {steps.map((s) => (
            <div key={`${s.step}-${s.label}`} className="flex items-start gap-2 text-sm">
              <Pill tone={s.ok ? "green" : "red"}>{s.step || "–"}</Pill>
              <div><div className="font-medium">{s.label}</div>
                {s.detail != null && <pre className="mt-0.5 whitespace-pre-wrap font-mono text-[11px] text-fg3">{typeof s.detail === "string" ? s.detail : JSON.stringify(s.detail)}</pre>}</div>
            </div>
          ))}
        </div>
      ) : (
        <div className="flex flex-col gap-3">
          <label className={`flex cursor-pointer gap-3 rounded-md border p-3 ${mode === "clean" ? "border-blue" : "border-line"} ${!pre.online ? "opacity-50" : ""}`}>
            <input type="radio" name="mode" checked={mode === "clean"} disabled={!pre.online} onChange={() => setMode("clean")} />
            <div><div className="font-medium">Router bereinigen und entfernen <span className="text-fg3">(Standard)</span></div>
              <p className="text-sm text-fg2">{pre.online ? "Backup → defconf-Regeln wieder aktivieren → verwaltete Objekte entfernen → Dienste zurückstellen → Fernzugriffs-Benutzer entfernen → zuletzt API-Benutzer und Management-Tunnel." : "Nur möglich, wenn das Gerät online ist."}</p>
              {pre.online && Object.keys(pre.counts).length > 0 && <p className="mt-1 text-xs text-fg3">Entfernt: {Object.entries(pre.counts).map(([k, v]) => `${k} (${v})`).join(", ")}{Object.keys(pre.final).length > 0 && `; zum Schluss auf dem Router: ${Object.entries(pre.final).map(([k, v]) => `${k} (${v})`).join(", ")}`}</p>}
              {pre.defconf_disabled > 0 && <p className="mt-1 text-xs text-fg3">{pre.defconf_disabled} von der Plattform deaktivierte defconf-Regeln werden zuerst wieder aktiviert.</p>}
            </div>
          </label>
          {mode === "clean" && pre.warnings.map((w) => <Notice key={w} tone="orange" icon="alert">{w}</Notice>)}
          <label className={`flex cursor-pointer gap-3 rounded-md border p-3 ${mode === "platform_only" ? "border-red" : "border-line"}`}>
            <input type="radio" name="mode" checked={mode === "platform_only"} onChange={() => setMode("platform_only")} />
            <div><div className="font-medium">Nur aus der Plattform entfernen</div>
              <p className="text-sm text-fg2">Der Router bleibt unverändert.</p></div>
          </label>
          {mode === "platform_only" && <Notice tone="red" icon="alert" title="Verwaltete Konfiguration bleibt auf dem Router">
            Alle Objekte mit „sdwan:“ (Firewall, WAN, VRRP, WLAN, Hotspot …), der API-Benutzer und der Management-Tunnel bleiben bestehen – ebenso von der Plattform
            deaktivierte defconf-Regeln{pre.defconf_disabled > 0 ? ` (${pre.defconf_disabled})` : ""}. Niemand verwaltet diese Konfiguration danach mehr.</Notice>}
          {pre.error && <Notice tone="orange">Vorschau unvollständig: {pre.error}</Notice>}
          <Input label={`Zur Bestätigung den Gerätenamen „${device.name}“ eintippen`} value={name} onChange={(e) => setName(e.target.value)} autoComplete="off" />
        </div>
      )}
    </Modal>
  );
}
