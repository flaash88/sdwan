import {
  useEffect,
  useId,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";
import { Icon, type IconName } from "./Icon";

export function cls(...c: (string | false | null | undefined)[]) {
  return c.filter(Boolean).join(" ");
}

/* ----------------------------------------------------------------------------- Karten & Seiten */
export function Card({ title, subtitle, actions, children, className, flush, bodyClassName }: {
  title?: ReactNode; subtitle?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string;
  /** ohne Innenabstand (Tabellen/Listen bis zum Rand) */ flush?: boolean; bodyClassName?: string;
}) {
  return (
    <section className={cls("min-w-0 overflow-hidden rounded-lg border border-line bg-panel", className)}>
      {(title || actions) && (
        <header className="flex min-h-12 flex-wrap items-center gap-2 border-b border-line px-4 py-2.5">
          <h3 className="font-semibold text-fg">{title}</h3>
          {subtitle && <span className="text-xs text-fg3">{subtitle}</span>}
          <div className="flex-1" />
          {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={cls(!flush && "p-4", bodyClassName)}>{children}</div>
    </section>
  );
}

export function PageHeader({ title, subtitle, actions }: { title: ReactNode; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-5 flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        <h1 className="text-xl font-semibold tracking-[-0.01em] text-fg">{title}</h1>
        {subtitle && <div className="text-fg2">{subtitle}</div>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

/* ----------------------------------------------------------------------------- Buttons & Formulare */
type Variant = "primary" | "secondary" | "danger" | "ghost" | "danger-outline";
export function Button({ variant = "primary", size = "md", icon, className, children, ...p }: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant; size?: "sm" | "md"; icon?: IconName;
}) {
  const v: Record<Variant, string> = {
    primary: "border-blue bg-blue text-white hover:brightness-110",
    secondary: "border-line-strong bg-panel text-fg hover:bg-hover",
    "danger-outline": "border-line-strong bg-panel text-red-text hover:bg-hover",
    danger: "border-red bg-red text-white hover:brightness-110",
    ghost: "border-transparent bg-transparent text-fg2 hover:bg-hover hover:text-fg",
  };
  return (
    <button
      {...p}
      className={cls(
        "inline-flex shrink-0 cursor-pointer items-center justify-center gap-1.5 whitespace-nowrap rounded-md border font-medium disabled:cursor-not-allowed disabled:opacity-50",
        size === "sm" ? "h-7 px-2.5 text-[12.5px]" : "h-8 px-3",
        v[variant],
        className,
      )}
    >
      {icon && <Icon name={icon} className="text-[14px]" />}
      {children}
    </button>
  );
}

export function IconButton({ icon, label, className, ...p }: ButtonHTMLAttributes<HTMLButtonElement> & { icon: IconName; label: string }) {
  return (
    <button {...p} title={label} aria-label={label}
      className={cls("inline-flex h-7 w-7 cursor-pointer items-center justify-center rounded-md text-[14px] text-fg3 hover:bg-hover hover:text-fg disabled:opacity-40", className)}>
      <Icon name={icon} />
    </button>
  );
}

const control = "w-full rounded-md border border-line-strong bg-panel px-2.5 text-fg placeholder:text-fg3 focus:border-blue focus:outline-none focus:ring-2 focus:ring-blue-bg disabled:opacity-60";

function Field({ label, hint, children }: { label?: ReactNode; hint?: ReactNode; children: ReactNode }) {
  if (!label && !hint) return <>{children}</>;
  return (
    <label className="flex min-w-0 flex-col gap-1.5">
      {label && <span className="font-medium text-fg">{label}</span>}
      {children}
      {hint && <span className="text-xs text-fg3">{hint}</span>}
    </label>
  );
}

export function Input({ label, hint, className, ...p }: InputHTMLAttributes<HTMLInputElement> & { label?: ReactNode; hint?: ReactNode }) {
  return <Field label={label} hint={hint}><input {...p} className={cls(control, "h-[34px]", className)} /></Field>;
}

export function Textarea({ label, hint, className, ...p }: TextareaHTMLAttributes<HTMLTextAreaElement> & { label?: ReactNode; hint?: ReactNode }) {
  return <Field label={label} hint={hint}><textarea {...p} className={cls(control, "py-2 font-mono text-xs leading-relaxed", className)} /></Field>;
}

export function Select({ label, hint, children, className, ...p }: SelectHTMLAttributes<HTMLSelectElement> & { label?: ReactNode; hint?: ReactNode }) {
  return <Field label={label} hint={hint}><select {...p} className={cls(control, "h-[34px] cursor-pointer pr-7", className)}>{children}</select></Field>;
}

export function Checkbox({ label, checked, onChange, disabled }: { label: ReactNode; checked: boolean; onChange: (v: boolean) => void; disabled?: boolean }) {
  return (
    <label className="flex cursor-pointer items-center gap-2 text-fg2">
      <input type="checkbox" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} className="h-4 w-4 cursor-pointer" />
      {label}
    </label>
  );
}

/** Schalter (z. B. Regel aktiv) – als Button mit role="switch" tastaturbedienbar. */
export function Toggle({ checked, onChange, label, disabled }: { checked: boolean; onChange: (v: boolean) => void; label: string; disabled?: boolean }) {
  return (
    <button type="button" role="switch" aria-checked={checked} aria-label={label} title={label} disabled={disabled} onClick={() => onChange(!checked)}
      className={cls("relative block h-[18px] w-[30px] shrink-0 cursor-pointer rounded-full transition-colors disabled:opacity-50", checked ? "bg-blue" : "bg-line-strong")}>
      <span className={cls("absolute top-[2px] h-[14px] w-[14px] rounded-full bg-white shadow transition-[left]", checked ? "left-[14px]" : "left-[2px]")} />
    </button>
  );
}

/* ----------------------------------------------------------------------------- Badges */
export type Tone = "green" | "red" | "orange" | "blue" | "gray" | "neutral";
const toneCls: Record<Tone, string> = {
  green: "bg-green-bg text-green-text",
  red: "bg-red-bg text-red-text",
  orange: "bg-orange-bg text-orange-text",
  blue: "bg-blue-bg text-blue-text",
  gray: "bg-gray-bg text-gray-text",
  neutral: "bg-sunken text-fg2",
};
export const toneDot: Record<Tone, string> = { green: "bg-green", red: "bg-red", orange: "bg-orange", blue: "bg-blue", gray: "bg-gray", neutral: "bg-gray" };

/** Status-Pille: immer Icon + Text, nie nur Farbe. */
export function Pill({ tone = "neutral", icon, children, title, className }: { tone?: Tone; icon?: IconName; children: ReactNode; title?: string; className?: string }) {
  return (
    <span title={title} className={cls("inline-flex h-[22px] max-w-full items-center gap-[5px] whitespace-nowrap rounded-[5px] px-2 text-xs font-medium", toneCls[tone], className)}>
      {icon && <Icon name={icon} className="text-[13px]" />}
      <span className="truncate">{children}</span>
    </span>
  );
}

const legacy: Record<string, Tone> = { green: "green", red: "red", yellow: "orange", orange: "orange", gray: "neutral", blue: "blue" };
/** Kompatibel zu älteren Aufrufen (color="green" …). */
export function Badge({ color = "gray", children, icon }: { color?: string; children: ReactNode; icon?: IconName }) {
  return <Pill tone={legacy[color] ?? "neutral"} icon={icon}>{children}</Pill>;
}

const STATUS: Record<string, [string, Tone, IconName]> = {
  online: ["Online", "green", "checkCircle"], offline: ["Offline", "red", "xCircle"], unknown: ["Unbekannt", "gray", "minusCircle"],
  up: ["Up", "green", "checkCircle"], down: ["Down", "red", "xCircle"], degraded: ["Degradiert", "orange", "alert"],
  ok: ["OK", "green", "checkCircle"], success: ["Erfolgreich", "green", "checkCircle"], synced: ["Synchron", "green", "checkCircle"],
  paired: ["Verbunden", "green", "checkCircle"], pending: ["Ausstehend", "gray", "clock"], revoked: ["Gesperrt", "red", "xCircle"],
  failed: ["Fehlgeschlagen", "red", "xCircle"], error: ["Fehler", "red", "xCircle"], running: ["Läuft", "blue", "loader"],
  queued: ["Wartend", "gray", "clock"], cancelled: ["Abgebrochen", "gray", "minusCircle"], skipped: ["Übersprungen", "gray", "minusCircle"],
  paused: ["Pausiert", "orange", "pause"], disabled: ["Deaktiviert", "gray", "minusCircle"], no_endpoint: ["Kein Endpoint", "orange", "alert"],
  updating: ["Aktualisiert", "blue", "loader"], rebooting: ["Neustart", "blue", "loader"], active: ["Aktiv", "green", "checkCircle"],
  closed: ["Beendet", "gray", "minusCircle"], expired: ["Abgelaufen", "gray", "clock"],
  staged: ["Vorbereitet", "gray", "clock"], connected: ["Verbunden", "blue", "loader"], provisioning: ["Wird eingerichtet", "blue", "loader"],
  provisioned: ["Provisioniert", "green", "checkCircle"], master: ["Master", "orange", "alert"], backup: ["Backup", "neutral", "pause"],
};
export function statusInfo(status: string): [string, Tone, IconName] {
  return STATUS[status] ?? [status, "gray", "info"];
}
export function StatusBadge({ status, label }: { status: string; label?: string }) {
  const [l, t, i] = statusInfo(status);
  return <Pill tone={t} icon={i}>{label ?? l}</Pill>;
}

const SEV: Record<string, [string, Tone, IconName]> = {
  critical: ["Kritisch", "red", "octagon"], warning: ["Warnung", "orange", "alert"], info: ["Info", "gray", "info"],
};
export const severityLabel = (s: string) => SEV[s]?.[0] ?? s;
export function SeverityBadge({ severity, resolved }: { severity: string; resolved?: boolean }) {
  if (resolved) return <Pill tone="green" icon="checkCircle">Behoben</Pill>;
  const [l, t, i] = SEV[severity] ?? [severity, "gray", "info"];
  return <Pill tone={t} icon={i}>{l}</Pill>;
}

export function StatusDot({ status, tone }: { status?: string; tone?: Tone }) {
  const t: Tone = tone ?? (status === "online" || status === "up" || status === "ok" ? "green" : status === "offline" || status === "down" || status === "failed" ? "red" : status === "degraded" ? "orange" : "gray");
  const ring: Record<Tone, string> = { green: "shadow-[0_0_0_3px_var(--green-bg)]", red: "shadow-[0_0_0_3px_var(--red-bg)]", orange: "shadow-[0_0_0_3px_var(--orange-bg)]", blue: "shadow-[0_0_0_3px_var(--blue-bg)]", gray: "shadow-[0_0_0_3px_var(--gray-bg)]", neutral: "shadow-[0_0_0_3px_var(--gray-bg)]" };
  return <span aria-hidden className={cls("inline-block h-2 w-2 shrink-0 rounded-full", toneDot[t], ring[t])} />;
}

/* ----------------------------------------------------------------------------- Kennzahlen */
export function KpiTile({ label, icon, value, unit, sub, subIcon, tone, onClick, valueTone }: {
  label: string; icon?: IconName; value: ReactNode; unit?: ReactNode; sub?: ReactNode; subIcon?: IconName;
  tone?: Tone; valueTone?: Tone; onClick?: () => void;
}) {
  const fg: Partial<Record<Tone, string>> = { green: "text-green-text", red: "text-red-text", orange: "text-orange-text", blue: "text-blue-text" };
  const Tag = onClick ? "button" : "div";
  return (
    <Tag onClick={onClick} className={cls("flex min-w-0 flex-col gap-1.5 rounded-lg border border-line bg-panel px-4 py-3.5 text-left", onClick && "cursor-pointer hover:border-line-strong")}>
      <div className="flex items-center gap-[7px] text-xs font-medium text-fg2">{icon && <Icon name={icon} className="text-[14px] text-fg3" />}{label}</div>
      <div className="flex items-baseline gap-1.5">
        <span className={cls("text-[28px] font-semibold leading-[1.1] tracking-[-0.02em]", (valueTone && fg[valueTone]) || "text-fg")}>{value}</span>
        {unit && <span className="text-[13px] text-fg3">{unit}</span>}
      </div>
      {sub && (
        <div className={cls("flex min-w-0 items-center gap-[5px] text-xs", (tone && fg[tone]) || "text-fg3")}>
          {subIcon && <Icon name={subIcon} className="text-[13px]" />}<span className="truncate">{sub}</span>
        </div>
      )}
    </Tag>
  );
}

/** Kompatibel zu älteren Aufrufen. */
export function Stat({ label, value, sub, tone }: { label: string; value: ReactNode; sub?: ReactNode; tone?: "green" | "red" | "yellow" }) {
  return <KpiTile label={label} value={value} sub={sub} valueTone={tone === "yellow" ? "orange" : tone} />;
}

/* ----------------------------------------------------------------------------- Segment & Tabs */
export function Segment<T extends string>({ options, value, onChange, label, size = "md" }: {
  options: { value: T; label: ReactNode; count?: number; icon?: IconName; title?: string }[];
  value: T; onChange: (v: T) => void; label?: string; size?: "sm" | "md";
}) {
  return (
    <div role="radiogroup" aria-label={label} className="inline-flex gap-0.5 rounded-[7px] border border-line bg-sunken p-0.5">
      {options.map((o) => {
        const on = o.value === value;
        return (
          <button key={o.value} type="button" role="radio" aria-checked={on} title={o.title} aria-label={o.title}
            onClick={() => onChange(o.value)}
            className={cls("flex cursor-pointer items-center gap-1.5 rounded-[5px] px-2.5 text-xs font-medium",
              size === "sm" ? "h-[26px]" : "h-[26px]",
              on ? "bg-panel text-fg shadow-[0_0_0_1px_var(--border),0_1px_2px_rgba(16,24,40,.06)]" : "text-fg2 hover:text-fg")}>
            {o.icon && <Icon name={o.icon} className="text-[15px]" />}
            {o.label}
            {o.count != null && <span className="text-fg3">{o.count}</span>}
          </button>
        );
      })}
    </div>
  );
}

export function Tabs<T extends string>({ tabs, value, onChange, className }: {
  tabs: { key: T; label: ReactNode; count?: number | null }[]; value: T; onChange: (v: T) => void; className?: string;
}) {
  return (
    <div role="tablist" className={cls("flex gap-1 overflow-x-auto", className)}>
      {tabs.map((t) => {
        const on = t.key === value;
        return (
          <button key={t.key} role="tab" aria-selected={on} type="button" onClick={() => onChange(t.key)}
            className={cls("flex h-[42px] shrink-0 cursor-pointer items-center gap-1.5 px-2.5 font-medium", on ? "text-fg shadow-[inset_0_-2px_0_var(--blue)]" : "text-fg2 hover:text-fg")}>
            {t.label}
            {t.count != null && <span className="flex h-[18px] items-center rounded-full bg-sunken px-1.5 text-[11px] text-fg3">{t.count}</span>}
          </button>
        );
      })}
    </div>
  );
}

/* ----------------------------------------------------------------------------- Tabellen */
/** Tabelle mit Kopfzeile im Design (panel2, 12 px). Zeilen/Zellen liefert der Aufrufer (<tr><td className="px-3 py-2">). */
export function Table({ head, children, empty, emptyText, className }: { head: ReactNode[]; children: ReactNode; empty?: boolean; emptyText?: ReactNode; className?: string }) {
  return (
    <div className={cls("overflow-x-auto", className)}>
      <table className="w-full border-collapse text-left">
        <thead className="bg-panel2 text-xs text-fg3">
          <tr className="border-b border-line">{head.map((h, i) => <th key={i} className="whitespace-nowrap px-3 py-2 font-medium">{h}</th>)}</tr>
        </thead>
        <tbody className="[&>tr]:border-b [&>tr]:border-line [&>tr:last-child]:border-b-0 [&>tr:hover]:bg-hover">{children}</tbody>
      </table>
      {empty && <EmptyState compact title={emptyText ?? "Keine Einträge"} />}
    </div>
  );
}

/** Auswahl-Checkbox für Tabellen (Klick stoppt Zeilen-Navigation). */
export function RowCheck({ checked, onChange, label, indeterminate }: { checked: boolean; onChange: (v: boolean) => void; label: string; indeterminate?: boolean }) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => { if (ref.current) ref.current.indeterminate = !!indeterminate; }, [indeterminate]);
  return <input ref={ref} type="checkbox" aria-label={label} checked={checked} onClick={(e) => e.stopPropagation()} onChange={(e) => onChange(e.target.checked)} className="h-4 w-4 cursor-pointer" />;
}

/** Sammelaktionsleiste, sobald Zeilen ausgewählt sind. */
export function SelectionBar({ count, noun = ["Gerät", "Geräte"], onClear, children }: { count: number; noun?: [string, string]; onClear: () => void; children: ReactNode }) {
  if (!count) return null;
  return (
    <div className="flex flex-wrap items-center gap-2.5 rounded-lg border border-blue bg-blue-bg py-2 pl-3.5 pr-2">
      <span className="font-semibold text-blue-text">{count} {count === 1 ? noun[0] : noun[1]} ausgewählt</span>
      <div className="flex-1" />
      {children}
      <button type="button" onClick={onClear} className="h-[30px] cursor-pointer px-2.5 font-medium text-blue-text hover:underline">Auswahl aufheben</button>
    </div>
  );
}

/* ----------------------------------------------------------------------------- Zustände */
export function EmptyState({ icon = "info", title, text, action, compact }: { icon?: IconName; title: ReactNode; text?: ReactNode; action?: ReactNode; compact?: boolean }) {
  return (
    <div className={cls("flex flex-col items-center justify-center gap-2 text-center text-fg3", compact ? "py-8" : "py-14")}>
      {!compact && <span className="flex h-10 w-10 items-center justify-center rounded-lg bg-sunken text-[18px] text-fg2"><Icon name={icon} /></span>}
      <div className={cls("font-medium", compact ? "text-fg3" : "text-fg")}>{title}</div>
      {text && <div className="max-w-md text-fg2">{text}</div>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

export function Spinner({ className }: { className?: string }) {
  return <Icon name="loader" className={cls("animate-spin", className)} />;
}

export function Loading({ label = "Lade …", rows }: { label?: string; rows?: number }) {
  if (rows) {
    return (
      <div className="space-y-2 p-4" aria-busy="true" aria-label={label}>
        {Array.from({ length: rows }, (_, i) => <div key={i} className="h-8 animate-pulse rounded-md bg-sunken" />)}
      </div>
    );
  }
  return <div className="flex items-center gap-2 p-6 text-fg3" aria-busy="true"><Spinner />{label}</div>;
}

export function Notice({ tone = "blue", icon, title, children }: { tone?: Tone; icon?: IconName; title?: ReactNode; children?: ReactNode }) {
  const border: Record<Tone, string> = { green: "border-green", red: "border-red", orange: "border-orange", blue: "border-blue", gray: "border-line-strong", neutral: "border-line-strong" };
  const def: Record<Tone, IconName> = { green: "checkCircle", red: "xCircle", orange: "alert", blue: "info", gray: "info", neutral: "info" };
  return (
    <div role={tone === "red" ? "alert" : "status"} className={cls("flex gap-2.5 rounded-lg border px-3.5 py-2.5", border[tone], toneCls[tone])}>
      <Icon name={icon ?? def[tone]} className="mt-0.5 text-[15px]" />
      <div className="min-w-0 text-fg">{title && <div className="font-semibold">{title}</div>}{children}</div>
    </div>
  );
}

export function ErrorBox({ error }: { error: string | null | undefined }) {
  if (!error) return null;
  return <div className="mb-4"><Notice tone="red">{error}</Notice></div>;
}

/* ----------------------------------------------------------------------------- Dialog */
/**
 * Dialog: max. Höhe mit scrollendem Inhalt, Footer immer sichtbar, Esc schließt, Fokus bleibt im Dialog.
 * ``footer`` optional – ältere Aufrufe legen Buttons in ``children``.
 */
export function Modal({ open, onClose, title, subtitle, children, footer, wide, size }: {
  open: boolean; onClose: () => void; title: ReactNode; subtitle?: ReactNode; children: ReactNode; footer?: ReactNode; wide?: boolean; size?: "sm" | "md" | "lg" | "xl";
}) {
  const ref = useRef<HTMLDivElement>(null);
  const id = useId();
  useEffect(() => {
    if (!open) return;
    const prev = document.activeElement as HTMLElement | null;
    const el = ref.current;
    const focusables = () => [...(el?.querySelectorAll<HTMLElement>('a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])') ?? [])];
    (focusables().find((f) => f.tagName !== "BUTTON" || !f.dataset.close) ?? el)?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.stopPropagation(); onClose(); }
      if (e.key === "Tab") {
        const f = focusables();
        if (!f.length) return;
        if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f[f.length - 1].focus(); }
        else if (!e.shiftKey && document.activeElement === f[f.length - 1]) { e.preventDefault(); f[0].focus(); }
      }
    };
    document.addEventListener("keydown", onKey);
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.removeEventListener("keydown", onKey); document.body.style.overflow = overflow; prev?.focus?.(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
  if (!open) return null;
  const w = size ?? (wide ? "lg" : "md");
  const maxW = { sm: "max-w-md", md: "max-w-lg", lg: "max-w-[640px]", xl: "max-w-4xl" }[w];
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-[var(--overlay)] p-4 pt-[6vh]" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div ref={ref} role="dialog" aria-modal="true" aria-labelledby={id} tabIndex={-1}
        className={cls("flex max-h-[88vh] w-full flex-col rounded-[10px] border border-line bg-panel shadow-[var(--shadow)] outline-none", maxW)}>
        <div className="flex shrink-0 items-start gap-2.5 border-b border-line px-5 py-4">
          <div className="min-w-0 flex-1">
            <h2 id={id} className="text-base font-semibold text-fg">{title}</h2>
            {subtitle && <div className="text-[12.5px] text-fg2">{subtitle}</div>}
          </div>
          <IconButton icon="x" label="Schließen" data-close="1" onClick={onClose} className="h-[30px] w-[30px] text-[16px]" />
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
        {footer && <div className="flex shrink-0 flex-wrap justify-end gap-2 rounded-b-[10px] border-t border-line bg-panel2 px-5 py-3.5">{footer}</div>}
      </div>
    </div>
  );
}
export const Dialog = Modal;

/* ----------------------------------------------------------------------------- Code */
/** Codeblock mit Kopieren-Button; Leerzeilen zwischen Befehlen werden entfernt. */
export function CodeBlock({ text, highlight = true, className }: { text: string; highlight?: boolean; className?: string }) {
  const [copied, setCopied] = useState(false);
  const lines = text.split("\n").filter((l) => l.trim() !== "");
  const clean = lines.join("\n");
  return (
    <div className={cls("relative rounded-[7px] border border-line bg-code py-3 pl-3.5 pr-28 font-mono text-xs leading-[1.65] text-fg", className)}>
      <pre className="m-0 whitespace-pre-wrap break-all font-mono">
        {lines.map((l, i) => {
          if (!highlight) return <div key={i}>{l}</div>;
          if (l.trimStart().startsWith("#")) return <div key={i} className="text-fg3">{l}</div>;
          const m = l.match(/^(\s*)(\/[\w\-/ ]*?\b(?:fetch|import|add|set|print|remove|run|enable|disable)\b|:[a-z]+|\/[\w-]+(?:\s\/?[\w-]+)*)(.*)$/);
          return m ? <div key={i}>{m[1]}<span className="text-blue-text">{m[2]}</span>{m[3]}</div> : <div key={i}>{l}</div>;
        })}
      </pre>
      <button type="button"
        onClick={() => { void navigator.clipboard?.writeText(clean); setCopied(true); setTimeout(() => setCopied(false), 2000); }}
        className={cls("absolute right-2 top-2 inline-flex h-7 cursor-pointer items-center gap-1.5 rounded-md border border-line-strong bg-panel px-2.5 font-sans text-[12.5px] font-medium hover:bg-hover", copied ? "text-green-text" : "text-fg")}>
        <Icon name={copied ? "check" : "copy"} className="text-[13px]" />{copied ? "Kopiert" : "Kopieren"}
      </button>
    </div>
  );
}
/** Kompatibel zu älteren Aufrufen. */
export function CopyBox({ text }: { text: string }) {
  return <CodeBlock text={text} />;
}

/* ----------------------------------------------------------------------------- Aktionen */
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
