import { useState } from "react";
import { ThemeSwitch } from "../components/Layout";
import { TwoFactorSetup } from "../components/TwoFactor";
import { Button, ErrorBox, Input, useAction } from "../components/ui";
import { api } from "../lib/api";
import { useAuth, type LoginStep } from "../lib/auth";
import { useMeta } from "../lib/meta";

export default function Login() {
  const { login, finishLogin } = useAuth();
  const [step, setStep] = useState<LoginStep | null>(null);
  const [code, setCode] = useState("");
  const meta = useMeta();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const { busy, error, run } = useAction();
  return (
    <div className="relative flex min-h-screen items-center justify-center bg-bg px-6 py-12 text-fg">
      <div className="absolute right-5 top-4"><ThemeSwitch /></div>
      <main className="flex w-full max-w-[380px] flex-col gap-6">
        <div className="flex flex-col gap-3.5">
          <img src="/favicon.svg" alt="" className="h-11 w-11 rounded-[10px]" />
          <div className="flex flex-col gap-1">
            <h1 className="text-[22px] font-semibold tracking-[-0.01em]">{meta?.product_name ?? " "}</h1>
            <p className="text-fg2">Melden Sie sich an, um Ihre Router-Flotte zu verwalten.</p>
          </div>
        </div>
        {step?.mfa_setup_required ? (
          <div className="flex flex-col gap-4 rounded-[10px] border border-line bg-panel p-6">
            <h2 className="font-semibold">Zwei-Faktor-Anmeldung einrichten</h2>
            <p className="text-sm text-fg2">Für Ihr Konto ist die Zwei-Faktor-Anmeldung Pflicht. Richten Sie sie jetzt ein, um fortzufahren.</p>
            <TwoFactorSetup setupToken={step.setup_token} onDone={(r) => void finishLogin(r as LoginStep).then(() => history.replaceState(null, "", "/"))} />
          </div>
        ) : step?.mfa_required ? (
          <form className="flex flex-col gap-4 rounded-[10px] border border-line bg-panel p-6" onSubmit={(e) => {
            e.preventDefault();
            void run(async () => {
              const r = await api.post<LoginStep>("/auth/login/2fa", { mfa_token: step.mfa_token, code });
              await finishLogin(r);
              history.replaceState(null, "", "/");
            });
          }}>
            <ErrorBox error={error} />
            <Input label="Code aus der Authenticator-App" hint="oder einen Wiederherstellungscode (xxxx-xxxx)" inputMode="numeric" autoComplete="one-time-code"
              value={code} onChange={(e) => setCode(e.target.value.trim())} autoFocus required className="h-9" />
            <Button className="h-[38px] w-full font-semibold" disabled={busy || code.length < 6}>{busy ? "Prüfe …" : "Bestätigen"}</Button>
            <button type="button" className="text-xs text-fg3 hover:underline" onClick={() => { setStep(null); setCode(""); }}>Zurück</button>
          </form>
        ) : (
        <form
          className="flex flex-col gap-4 rounded-[10px] border border-line bg-panel p-6"
          onSubmit={(e) => {
            e.preventDefault();
            void run(async () => {
              const r = await login(email, password);
              if (r.access_token) history.replaceState(null, "", "/");
              else setStep(r);
            });
          }}
        >
          <ErrorBox error={error} />
          <Input label="E-Mail" type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} autoFocus required className="h-9" />
          <Input label="Passwort" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required className="h-9" />
          <Button className="h-[38px] w-full font-semibold" disabled={busy}>{busy ? "Anmelden …" : "Anmelden"}</Button>
        </form>
        )}
        <p className="text-center text-xs text-fg3">
          {meta ? `Version ${meta.version} · ` : ""}selbst gehostet auf <span className="font-mono">{location.host}</span>
        </p>
      </main>
    </div>
  );
}
