import { useState } from "react";
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
    <div className="flex min-h-screen items-center justify-center bg-gradient-to-br from-slate-900 to-brand-900 p-4">
      <form
        className="w-full max-w-sm space-y-4 rounded-2xl bg-panel p-8 shadow-2xl"
        onSubmit={(e) => {
          e.preventDefault();
          void run(async () => {
            await login(email, password);
            history.replaceState(null, "", "/");
          });
        }}
      >
        <div className="flex items-center gap-3">
          <img src="/favicon.svg" className="h-9 w-9" alt="" />
          <div>
            <h1 className="text-lg font-semibold">{meta?.product_name ?? "\u00a0"}</h1>
            <p className="text-xs text-slate-500">Konfigurations- & Flottenmanagement für MikroTik-Router</p>
          </div>
        </div>
        <ErrorBox error={error} />
        <Input label="E-Mail" type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoFocus required />
        <Input label="Passwort" type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        <Button className="w-full justify-center py-2" disabled={busy}>
          {busy ? "Anmelden …" : "Anmelden"}
        </Button>
      </form>
    </div>
  );
}
