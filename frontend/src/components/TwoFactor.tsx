import { useState } from "react";
import { api } from "../lib/api";
import { Button, CopyBox, ErrorBox, Input, Modal, Notice, useAction } from "./ui";

interface Setup { secret: string; otpauth_uri: string; qr_svg: string }

/** Einrichtung: QR scannen, Code bestätigen, Wiederherstellungscodes sichern. ``setupToken`` = erzwungene Einrichtung. */
export function TwoFactorSetup({ setupToken, onDone }: { setupToken?: string; onDone: (result: { access_token?: string; user?: unknown }) => void }) {
  const [setup, setSetup] = useState<Setup | null>(null);
  const [code, setCode] = useState("");
  const [codes, setCodes] = useState<string[] | null>(null);
  const [result, setResult] = useState<{ access_token?: string; user?: unknown } | null>(null);
  const { busy, error, run } = useAction();
  if (codes) return (
    <div className="flex flex-col gap-3">
      <Notice tone="orange" icon="alert" title="Wiederherstellungscodes sichern">Jeder Code funktioniert genau einmal, falls das Handy fehlt. Jetzt ausdrucken oder im Passwort-Manager ablegen – sie werden nicht erneut angezeigt.</Notice>
      <div className="grid grid-cols-2 gap-2 rounded-md bg-panel2 p-3 font-mono text-sm">{codes.map((c) => <span key={c}>{c}</span>)}</div>
      <div className="flex gap-2">
        <Button variant="secondary" icon="download" onClick={() => { const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([codes.join("\n") + "\n"], { type: "text/plain" })); a.download = "wiederherstellungscodes.txt"; a.click(); }}>Als Datei</Button>
        <Button onClick={() => onDone(result ?? {})}>Gesichert – weiter</Button>
      </div>
    </div>
  );
  return (
    <div className="flex flex-col gap-3">
      <ErrorBox error={error} />
      {!setup ? (
        <>
          <p className="text-fg2">Zwei-Faktor-Anmeldung mit einer Authenticator-App (z. B. Aegis, Google/Microsoft Authenticator, 1Password).</p>
          <Button disabled={busy} onClick={() => void run(async () => setSetup(await api.post<Setup>("/auth/2fa/setup", { setup_token: setupToken ?? null })))}>Einrichtung starten</Button>
        </>
      ) : (
        <form className="flex flex-col gap-3" onSubmit={(e) => { e.preventDefault(); void run(async () => {
          const r = await api.post<{ recovery_codes: string[]; access_token?: string; user?: unknown }>("/auth/2fa/enable", { code, setup_token: setupToken ?? null });
          setResult(r); setCodes(r.recovery_codes);
        }); }}>
          <p className="text-fg2">1. QR-Code mit der App scannen:</p>
          <div className="mx-auto h-44 w-44 rounded bg-white p-1 [&_svg]:h-full [&_svg]:w-full" dangerouslySetInnerHTML={{ __html: setup.qr_svg }} />
          <div className="text-xs text-fg3">oder Schlüssel manuell eingeben:</div><CopyBox text={setup.secret} />
          <Input label="2. Angezeigten 6-stelligen Code eingeben" inputMode="numeric" autoComplete="one-time-code" maxLength={6} value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))} autoFocus />
          <Button disabled={busy || code.length !== 6}>Aktivieren</Button>
        </form>
      )}
    </div>
  );
}

/** Profil: 2FA einrichten, Wiederherstellungscodes neu erzeugen, deaktivieren (nur ohne Pflicht). */
export function TwoFactorDialog({ enabled, required, left, onClose, onChanged }: { enabled: boolean; required: boolean; left: number | null | undefined; onClose: () => void; onChanged: () => Promise<void> }) {
  const [code, setCode] = useState("");
  const [codes, setCodes] = useState<string[] | null>(null);
  const { busy, error, run } = useAction();
  return (
    <Modal open onClose={onClose} title="Zwei-Faktor-Anmeldung">
      {!enabled ? <TwoFactorSetup onDone={() => void onChanged().then(onClose)} /> : codes ? (
        <div className="flex flex-col gap-3"><Notice tone="orange" title="Neue Wiederherstellungscodes – alte sind ungültig" />
          <div className="grid grid-cols-2 gap-2 rounded-md bg-panel2 p-3 font-mono text-sm">{codes.map((c) => <span key={c}>{c}</span>)}</div>
          <Button onClick={onClose}>Gesichert</Button></div>
      ) : (
        <div className="flex flex-col gap-3">
          <ErrorBox error={error} />
          <Notice tone="green" icon="checkCircle" title="2FA ist aktiv">{left != null && `${left} Wiederherstellungscodes übrig.`} {required && "Für dieses Konto ist 2FA Pflicht."}</Notice>
          <Input label="Code aus der App (zur Bestätigung)" inputMode="numeric" value={code} onChange={(e) => setCode(e.target.value.trim())} />
          <div className="flex flex-wrap gap-2">
            <Button variant="secondary" disabled={busy || code.length < 6} onClick={() => void run(async () => { setCodes((await api.post<{ recovery_codes: string[] }>("/auth/2fa/recovery-codes", { code })).recovery_codes); })}>Wiederherstellungscodes neu erzeugen</Button>
            {!required && <Button variant="danger-outline" disabled={busy || code.length < 6} onClick={() => void run(async () => { await api.post("/auth/2fa/disable", { code }); await onChanged(); onClose(); })}>2FA deaktivieren</Button>}
          </div>
        </div>
      )}
    </Modal>
  );
}
