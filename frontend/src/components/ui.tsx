import { useState, type ButtonHTMLAttributes, type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes, type TextareaHTMLAttributes } from "react";

export function cls(...c: (string | false | null | undefined)[]) {
  return c.filter(Boolean).join(" ");
}

export function Card({ title, actions, children, className }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <div className={cls("rounded-xl border border-slate-200 bg-white shadow-sm", className)}>
      {(title || actions) && (
        <div className="flex items-center justify-between border-b border-slate-100 px-5 py-3">
          <h3 className="font-semibold text-slate-800">{title}</h3>
          <div className="flex gap-2">{actions}</div>
        </div>
      )}
      <div className="p-5">{children}</div>
    </div>
  );
}

type Variant = "primary" | "secondary" | "danger" | "ghost";
export function Button({ variant = "primary", className, ...p }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  const v: Record<Variant, string> = {
    primary: "bg-brand-600 text-white hover:bg-brand-700",
    secondary: "bg-white text-slate-700 border border-slate-300 hover:bg-slate-50",
    danger: "bg-red-600 text-white hover:bg-red-700",
    ghost: "text-slate-600 hover:bg-slate-100",
  };
  return <button {...p} className={cls("inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium disabled:opacity-50", v[variant], className)} />;
}

export function Input({ label, className, ...p }: InputHTMLAttributes<HTMLInputElement> & { label?: string }) {
  return (
    <label className="block text-sm">
      {label && <span className="mb-1 block font-medium text-slate-700">{label}</span>}
      <input {...p} className={cls("w-full rounded-lg border border-slate-300 px-3 py-1.5 focus:border-brand-500 focus:outline-none", className)} />
    </label>
  );
}

export function Textarea({ label, className, ...p }: TextareaHTMLAttributes<HTMLTextAreaElement> & { label?: string }) {
  return (
    <label className="block text-sm">
      {label && <span className="mb-1 block font-medium text-slate-700">{label}</span>}
      <textarea {...p} className={cls("w-full rounded-lg border border-slate-300 px-3 py-1.5 font-mono text-xs focus:border-brand-500 focus:outline-none", className)} />
    </label>
  );
}

export function Select({ label, children, className, ...p }: SelectHTMLAttributes<HTMLSelectElement> & { label?: string }) {
  return (
    <label className="block text-sm">
      {label && <span className="mb-1 block font-medium text-slate-700">{label}</span>}
      <select {...p} className={cls("w-full rounded-lg border border-slate-300 bg-white px-3 py-1.5", className)}>
        {children}
      </select>
    </label>
  );
}

export function Checkbox({ label, checked, onChange }: { label: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="flex items-center gap-2 text-sm text-slate-700">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} className="h-4 w-4 accent-teal-600" />
      {label}
    </label>
  );
}

const badgeColors: Record<string, string> = {
  green: "bg-emerald-100 text-emerald-800",
  red: "bg-red-100 text-red-800",
  yellow: "bg-amber-100 text-amber-800",
  gray: "bg-slate-100 text-slate-700",
  blue: "bg-sky-100 text-sky-800",
};
export function Badge({ color = "gray", children }: { color?: keyof typeof badgeColors | string; children: ReactNode }) {
  return <span className={cls("inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium", badgeColors[color] ?? badgeColors.gray)}>{children}</span>;
}

export function StatusDot({ status }: { status: string }) {
  const c = status === "online" || status === "up" || status === "ok" ? "bg-emerald-500" : status === "offline" || status === "down" || status === "failed" ? "bg-red-500" : status === "degraded" ? "bg-amber-500" : "bg-slate-400";
  return (
    <span className="relative inline-flex h-2.5 w-2.5">
      {(status === "online" || status === "up") && <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-60" />}
      <span className={cls("relative inline-flex h-2.5 w-2.5 rounded-full", c)} />
    </span>
  );
}

export function StatusBadge({ status }: { status: string }) {
  const color = ({ online: "green", up: "green", ok: "green", success: "green", paired: "green", offline: "red", down: "red", failed: "red", error: "red", revoked: "red", pending: "yellow", running: "blue", queued: "gray", degraded: "yellow" } as Record<string, string>)[status] ?? "gray";
  return <Badge color={color}>{status}</Badge>;
}

export function Modal({ open, onClose, title, children, wide }: { open: boolean; onClose: () => void; title: string; children: ReactNode; wide?: boolean }) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-slate-900/40 p-4 pt-20" onClick={onClose}>
      <div className={cls("w-full rounded-xl bg-white shadow-xl", wide ? "max-w-3xl" : "max-w-lg")} onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b px-5 py-3">
          <h3 className="font-semibold">{title}</h3>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-700">✕</button>
        </div>
        <div className="p-5">{children}</div>
      </div>
    </div>
  );
}

export function Table({ head, children, empty }: { head: ReactNode[]; children: ReactNode; empty?: boolean }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
          <tr>{head.map((h, i) => <th key={i} className="px-3 py-2 font-medium">{h}</th>)}</tr>
        </thead>
        <tbody className="divide-y divide-slate-100">{children}</tbody>
      </table>
      {empty && <p className="py-8 text-center text-sm text-slate-400">Keine Einträge</p>}
    </div>
  );
}

export function Stat({ label, value, sub, tone }: { label: string; value: ReactNode; sub?: ReactNode; tone?: "green" | "red" | "yellow" }) {
  const t = tone === "green" ? "text-emerald-600" : tone === "red" ? "text-red-600" : tone === "yellow" ? "text-amber-600" : "text-slate-900";
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</div>
      <div className={cls("mt-1 text-2xl font-semibold", t)}>{value}</div>
      {sub && <div className="mt-0.5 text-xs text-slate-500">{sub}</div>}
    </div>
  );
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-2xl font-semibold text-slate-900">{title}</h1>
        {subtitle && <p className="mt-1 text-sm text-slate-500">{subtitle}</p>}
      </div>
      <div className="flex gap-2">{actions}</div>
    </div>
  );
}

export function ErrorBox({ error }: { error: string | null }) {
  if (!error) return null;
  return <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-700">{error}</div>;
}

export function CopyBox({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="relative">
      <pre className="overflow-x-auto whitespace-pre-wrap break-all rounded-lg bg-slate-900 p-3 pr-20 font-mono text-xs text-emerald-300">{text}</pre>
      <button
        className="absolute right-2 top-2 rounded bg-slate-700 px-2 py-1 text-xs text-white hover:bg-slate-600"
        onClick={() => {
          void navigator.clipboard.writeText(text);
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        }}
      >
        {copied ? "Kopiert" : "Kopieren"}
      </button>
    </div>
  );
}

/** Führt eine async Aktion aus und liefert busy/error-State. */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async <T,>(fn: () => Promise<T>): Promise<T | undefined> => {
    setBusy(true);
    setError(null);
    try {
      return await fn();
    } catch (e) {
      setError((e as Error).message);
      return undefined;
    } finally {
      setBusy(false);
    }
  };
  return { busy, error, setError, run };
}
