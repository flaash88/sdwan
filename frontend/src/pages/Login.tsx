import { useState } from "react";
import { ThemeSwitch } from "../components/Layout";
import { Button, ErrorBox, Input, useAction } from "../components/ui";
import { useAuth } from "../lib/auth";
import { useMeta } from "../lib/meta";

export default function Login() {
  const { login } = useAuth();
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
        <form
          className="flex flex-col gap-4 rounded-[10px] border border-line bg-panel p-6"
          onSubmit={(e) => {
            e.preventDefault();
            void run(async () => {
              await login(email, password);
              history.replaceState(null, "", "/");
            });
          }}
        >
          <ErrorBox error={error} />
          <Input label="E-Mail" type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} autoFocus required className="h-9" />
          <Input label="Passwort" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required className="h-9" />
          <Button className="h-[38px] w-full font-semibold" disabled={busy}>{busy ? "Anmelden …" : "Anmelden"}</Button>
        </form>
        <p className="text-center text-xs text-fg3">
          {meta ? `Version ${meta.version} · ` : ""}selbst gehostet auf <span className="font-mono">{location.host}</span>
        </p>
      </main>
    </div>
  );
}
