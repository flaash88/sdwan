import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../lib/auth";
import { activeAlarmCount, firmwareUpdate, useDevices, useOpenAlerts, useSites } from "../lib/fleet";
import { useLiveConnected } from "../lib/live";
import { useMeta } from "../lib/meta";
import { useTheme, type ThemePref } from "../lib/theme";
import type { Device, Site } from "../lib/types";
import { NAV, NAV_ADMIN, type NavItem } from "../nav";
import AlertToasts from "./AlertToasts";
import { Icon, type IconName } from "./Icon";
import { cls, Segment } from "./ui";

const SIDEBAR_KEY = "fm.sidebar";
const ROLE_LABEL = { admin: "Admin", technician: "Techniker", readonly: "Nur lesen" } as const;

export function ThemeSwitch() {
  const [pref, setPref] = useTheme();
  const opts: { value: ThemePref; label: ReactNode; title: string }[] = [
    { value: "light", label: <Icon name="sun" />, title: "Hell" },
    { value: "dark", label: <Icon name="moon" />, title: "Dunkel" },
    { value: "system", label: <Icon name="monitor" />, title: "System" },
  ];
  return <Segment label="Darstellung" options={opts} value={pref} onChange={setPref} />;
}

/** Schließt ein Popover bei Klick außerhalb oder Esc. */
function usePopover() {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const click = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false); };
    const key = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", click);
    document.addEventListener("keydown", key);
    return () => { document.removeEventListener("mousedown", click); document.removeEventListener("keydown", key); };
  }, [open]);
  return { open, setOpen, ref };
}

function TenantSwitcher() {
  const { me, switchTenant } = useAuth();
  const { open, setOpen, ref } = usePopover();
  if (!me) return null;
  const current = me.tenants.find((t) => t.id === me.active_tenant_id);
  const label = current?.name ?? (me.user.is_superuser ? "Alle Mandanten" : me.tenants[0]?.name);
  const Chip = (
    <>
      <span className="flex h-[22px] w-[22px] items-center justify-center rounded-[5px] bg-sunken text-[13px] text-fg2"><Icon name="building" /></span>
      <span className="flex min-w-0 flex-col items-start leading-[1.15]">
        <span className="text-[10.5px] text-fg3">Mandant</span>
        <span className="max-w-[180px] truncate font-semibold">{label}</span>
      </span>
    </>
  );
  if (!me.user.is_superuser) return <div className="flex h-9 items-center gap-2.5 px-2">{Chip}</div>;
  return (
    <div ref={ref} className="relative">
      <button type="button" aria-haspopup="listbox" aria-expanded={open} onClick={() => setOpen(!open)}
        className="flex h-9 cursor-pointer items-center gap-2.5 rounded-[7px] border border-line bg-panel pl-2 pr-2.5 hover:border-line-strong">
        {Chip}
        <Icon name="chevDown" className="ml-1.5 text-[14px] text-fg3" />
      </button>
      {open && (
        <div role="listbox" className="absolute left-0 top-11 z-30 flex max-h-[70vh] w-[300px] flex-col gap-0.5 overflow-y-auto rounded-lg border border-line bg-panel p-1.5 shadow-[var(--shadow)]">
          <div className="px-2 pb-1 pt-1.5 text-[11px] font-semibold uppercase tracking-[.04em] text-fg3">Mandant wechseln</div>
          {me.tenants.map((t) => (
            <button key={t.id} type="button" role="option" aria-selected={t.id === me.active_tenant_id} onClick={() => switchTenant(t.id)}
              className={cls("flex cursor-pointer items-center gap-2.5 rounded-md px-2 py-[7px] text-left hover:bg-hover", t.id === me.active_tenant_id && "bg-hover")}>
              <span className="flex flex-1 flex-col leading-[1.3]"><span className="font-medium">{t.name}</span><span className="font-mono text-xs text-fg3">{t.slug}{t.is_active ? "" : " · deaktiviert"}</span></span>
              {t.id === me.active_tenant_id && <Icon name="check" className="text-[15px] text-blue-text" />}
            </button>
          ))}
          <div className="my-1 h-px bg-line" />
          <button type="button" onClick={() => switchTenant(null)} className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-[7px] text-left text-fg2 hover:bg-hover">
            <Icon name="grid" className="text-[14px]" />Alle Mandanten (MSP-Übersicht)
            {!me.active_tenant_id && <Icon name="check" className="ml-auto text-[15px] text-blue-text" />}
          </button>
        </div>
      )}
    </div>
  );
}

type Hit = { kind: "device" | "site"; id: string; title: string; sub: string; icon: IconName; to: string };

function GlobalSearch({ devices, sites }: { devices: Device[] | null; sites: Site[] | null }) {
  const nav = useNavigate();
  const input = useRef<HTMLInputElement>(null);
  const { open, setOpen, ref } = usePopover();
  const [q, setQ] = useState("");
  const [idx, setIdx] = useState(0);
  const mac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); input.current?.focus(); input.current?.select(); setOpen(true); }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [setOpen]);

  const hits = useMemo<Hit[]>(() => {
    const s = q.trim().toLowerCase();
    if (!s) return [];
    const siteName = (id: string | null) => sites?.find((x) => x.id === id)?.name ?? "";
    const dev = (devices ?? []).filter((d) => [d.name, d.identity, d.tunnel_ip, d.mesh_ip, d.serial, d.model, siteName(d.site_id), ...d.tags].some((v) => v?.toLowerCase().includes(s)))
      .slice(0, 8).map<Hit>((d) => ({ kind: "device", id: d.id, title: d.name, sub: [siteName(d.site_id), d.tunnel_ip, d.serial].filter(Boolean).join(" · "), icon: "router", to: `/devices/${d.id}` }));
    const st = (sites ?? []).filter((x) => [x.name, x.address, ...x.lan_subnets].some((v) => v?.toLowerCase().includes(s)))
      .slice(0, 4).map<Hit>((x) => ({ kind: "site", id: x.id, title: x.name, sub: x.lan_subnets.join(", ") || "Standort", icon: "pin", to: `/sites?site=${x.id}` }));
    return [...dev, ...st];
  }, [q, devices, sites]);

  const go = (h: Hit) => { nav(h.to); setOpen(false); setQ(""); input.current?.blur(); };
  return (
    <div ref={ref} className="relative min-w-0 max-w-[460px] flex-1">
      <div className="flex h-[34px] items-center gap-2 rounded-[7px] border border-line bg-panel2 px-2.5 text-fg3 focus-within:border-blue">
        <Icon name="search" className="text-[15px]" />
        <input ref={input} value={q} role="combobox" aria-expanded={open && hits.length > 0} aria-label="Suche"
          placeholder="Gerät, IP, Standort oder Seriennummer …"
          onFocus={() => setOpen(true)}
          onChange={(e) => { setQ(e.target.value); setIdx(0); setOpen(true); }}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") { e.preventDefault(); setIdx((i) => Math.min(i + 1, hits.length - 1)); }
            else if (e.key === "ArrowUp") { e.preventDefault(); setIdx((i) => Math.max(i - 1, 0)); }
            else if (e.key === "Enter" && hits[idx]) go(hits[idx]);
            else if (e.key === "Escape") { setOpen(false); input.current?.blur(); }
          }}
          className="min-w-0 flex-1 border-0 bg-transparent text-fg outline-none placeholder:text-fg3 focus-visible:outline-none" />
        <kbd className="hidden rounded border border-line px-[5px] py-px font-mono text-[11px] font-medium text-fg3 sm:inline">{mac ? "⌘K" : "Strg K"}</kbd>
      </div>
      {open && q.trim() && (
        <div role="listbox" className="absolute left-0 right-0 top-10 z-30 overflow-hidden rounded-lg border border-line bg-panel p-1.5 shadow-[var(--shadow)]">
          {hits.length === 0 && <div className="px-2 py-3 text-fg3">Keine Treffer für „{q}“</div>}
          {hits.map((h, i) => (
            <button key={h.kind + h.id} type="button" role="option" aria-selected={i === idx} onMouseEnter={() => setIdx(i)} onClick={() => go(h)}
              className={cls("flex w-full cursor-pointer items-center gap-2.5 rounded-md px-2 py-1.5 text-left", i === idx && "bg-hover")}>
              <span className="flex h-7 w-7 items-center justify-center rounded-md bg-sunken text-[14px] text-fg2"><Icon name={h.icon} /></span>
              <span className="flex min-w-0 flex-col leading-tight"><span className="truncate font-medium">{h.title}</span><span className="truncate font-mono text-[11.5px] text-fg3">{h.sub}</span></span>
              <span className="ml-auto text-[11px] text-fg3">{h.kind === "device" ? "Gerät" : "Standort"}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function UserMenu() {
  const { me, logout } = useAuth();
  const live = useLiveConnected();
  const { open, setOpen, ref } = usePopover();
  if (!me) return null;
  const name = me.user.full_name || me.user.email;
  const initials = (me.user.full_name || me.user.email).split(/[\s@._-]+/).filter(Boolean).slice(0, 2).map((s) => s[0]?.toUpperCase()).join("");
  const role = me.user.is_superuser ? "MSP-Admin" : ROLE_LABEL[me.role];
  return (
    <div ref={ref} className="relative">
      <button type="button" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen(!open)} className="flex cursor-pointer items-center gap-2.5 rounded-[7px] px-1.5 py-1 hover:bg-hover">
        <span className="relative flex h-[30px] w-[30px] items-center justify-center rounded-full border border-line bg-sunken text-xs font-semibold text-fg2">
          {initials}
          <span title={live ? "Live verbunden" : "Live getrennt"} className={cls("absolute -bottom-px -right-px h-2.5 w-2.5 rounded-full border-2 border-panel", live ? "bg-green" : "bg-gray")} />
        </span>
        <span className="hidden flex-col text-left leading-[1.2] xl:flex"><span className="max-w-[160px] truncate font-medium">{name}</span><span className="text-[11.5px] text-fg3">{role}</span></span>
        <Icon name="chevDown" className="hidden text-[14px] text-fg3 xl:block" />
      </button>
      {open && (
        <div role="menu" className="absolute right-0 top-11 z-30 w-64 rounded-lg border border-line bg-panel p-1.5 shadow-[var(--shadow)]">
          <div className="px-2 py-1.5">
            <div className="truncate font-medium">{name}</div>
            <div className="truncate text-xs text-fg3">{me.user.email} · {role}</div>
            <div className="mt-1 flex items-center gap-1.5 text-xs text-fg2"><span className={cls("h-2 w-2 rounded-full", live ? "bg-green" : "bg-gray")} />{live ? "Live-Updates verbunden" : "Live-Updates getrennt"}</div>
          </div>
          <div className="my-1 h-px bg-line" />
          <div className="flex items-center justify-between px-2 py-1.5 sm:hidden"><span className="text-fg2">Darstellung</span><ThemeSwitch /></div>
          <button role="menuitem" type="button" onClick={logout} className="flex w-full cursor-pointer items-center gap-2 rounded-md px-2 py-[7px] text-left text-fg hover:bg-hover">
            <Icon name="logout" className="text-[15px] text-fg3" />Abmelden
          </button>
        </div>
      )}
    </div>
  );
}

function SideNav({ collapsed, badges, onNavigate }: { collapsed: boolean; badges: Record<string, number>; onNavigate?: () => void }) {
  const { me, can } = useAuth();
  const loc = useLocation();
  const visible = (n: NavItem) => (!n.superuser || me?.user.is_superuser) && (!n.role || can(n.role));
  const item = (n: NavItem) => {
    const active = n.to === "/" ? loc.pathname === "/" : loc.pathname.startsWith(n.to);
    const count = n.badge ? badges[n.badge] : 0;
    return (
      <NavLink key={n.to} to={n.to} end={n.to === "/"} title={n.label} onClick={onNavigate}
        className={cls("flex h-8 items-center gap-2.5 rounded-md", collapsed ? "justify-center" : "px-2.5",
          active ? "bg-blue-bg font-semibold text-blue-text" : "font-medium text-fg2 hover:bg-hover hover:text-fg")}>
        <Icon name={n.icon} className={cls("text-[16px]", active ? "text-blue-text" : "text-fg3")} />
        {!collapsed && <span className="flex-1 truncate">{n.label}</span>}
        {!collapsed && count > 0 && (
          <span aria-label={`${count} ${n.badge === "alarms" ? "aktive Alarme" : "Updates"}`}
            className={cls("flex h-[18px] min-w-[18px] items-center justify-center rounded-full px-[5px] text-[11px] font-semibold", n.badge === "alarms" ? "bg-red-bg text-red-text" : "bg-sunken text-fg2")}>{count}</span>
        )}
        {collapsed && count > 0 && <span className="sr-only">{count}</span>}
      </NavLink>
    );
  };
  const admin = NAV_ADMIN.filter(visible);
  return (
    <nav aria-label="Hauptnavigation" className="flex flex-1 flex-col gap-0.5 overflow-y-auto px-2 py-2.5">
      {NAV.filter(visible).map(item)}
      {admin.length > 0 && (
        <>
          <div className="mx-1 my-2 h-px bg-line" />
          {!collapsed && <div className="px-2.5 py-1 text-[11px] font-semibold uppercase tracking-[.04em] text-fg3">Verwaltung</div>}
          {admin.map(item)}
        </>
      )}
    </nav>
  );
}

function Brand({ collapsed }: { collapsed: boolean }) {
  const meta = useMeta();
  return (
    <div className="flex h-14 shrink-0 items-center gap-2.5 border-b border-line px-3.5">
      <img src="/favicon.svg" alt="" className="h-[30px] w-[30px] shrink-0 rounded-[7px]" />
      {!collapsed && (
        <div className="flex min-w-0 flex-col leading-[1.2]">
          <span className="truncate font-semibold" title={meta?.product_name}>{meta?.product_name ?? " "}</span>
          <span className="text-[11px] text-fg3">Konfiguration & Flotte</span>
        </div>
      )}
    </div>
  );
}

export default function Layout({ children }: { children: ReactNode }) {
  const { me } = useAuth();
  const nav = useNavigate();
  const loc = useLocation();
  const [collapsed, setCollapsed] = useState(() => { try { return localStorage.getItem(SIDEBAR_KEY) === "1"; } catch { return false; } });
  const [mobile, setMobile] = useState(false);
  const alerts = useOpenAlerts();
  const devices = useDevices();
  const sites = useSites();
  useEffect(() => setMobile(false), [loc.pathname]);
  useEffect(() => {
    if (!mobile) return;
    const k = (e: KeyboardEvent) => e.key === "Escape" && setMobile(false);
    document.addEventListener("keydown", k);
    return () => document.removeEventListener("keydown", k);
  }, [mobile]);
  useEffect(() => { try { localStorage.setItem(SIDEBAR_KEY, collapsed ? "1" : "0"); } catch { /* ignore */ } }, [collapsed]);
  if (!me) return null;
  const alarmCount = activeAlarmCount(alerts.data);
  const badges = { alarms: alarmCount, firmware: devices.data?.filter((d) => firmwareUpdate(d)?.update_available).length ?? 0 };

  return (
    <div className="flex min-h-screen bg-bg text-fg">
      <a href="#main" className="sr-only focus:not-sr-only focus:fixed focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-panel focus:px-3 focus:py-2">Zum Inhalt springen</a>
      {/* Seitenleiste ab 1024 px */}
      <aside className={cls("sticky top-0 hidden h-screen shrink-0 flex-col border-r border-line bg-panel lg:flex", collapsed ? "w-[60px]" : "w-[232px]")}>
        <Brand collapsed={collapsed} />
        <SideNav collapsed={collapsed} badges={badges} />
        <div className="border-t border-line p-2">
          <button type="button" onClick={() => setCollapsed(!collapsed)} aria-label={collapsed ? "Seitenleiste ausklappen" : "Seitenleiste einklappen"}
            className={cls("flex h-8 w-full cursor-pointer items-center gap-2.5 rounded-md text-fg3 hover:bg-hover hover:text-fg", collapsed ? "justify-center" : "px-2.5")}>
            <Icon name={collapsed ? "chevsRight" : "chevsLeft"} className="text-[16px]" />{!collapsed && <span>Einklappen</span>}
          </button>
        </div>
      </aside>
      {/* Overlay-Menü unter 1024 px */}
      {mobile && (
        <div className="fixed inset-0 z-40 lg:hidden" role="dialog" aria-modal="true" aria-label="Navigation">
          <div className="absolute inset-0 bg-[var(--overlay)]" onClick={() => setMobile(false)} />
          <aside className="absolute inset-y-0 left-0 flex w-[260px] flex-col border-r border-line bg-panel shadow-[var(--shadow)]">
            <Brand collapsed={false} />
            <SideNav collapsed={false} badges={badges} onNavigate={() => setMobile(false)} />
          </aside>
        </div>
      )}

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 flex h-14 shrink-0 items-center gap-3 border-b border-line bg-panel px-3 sm:px-5">
          <button type="button" className="flex h-[34px] w-[34px] cursor-pointer items-center justify-center rounded-[7px] border border-line text-[17px] text-fg2 lg:hidden" aria-label="Menü öffnen" onClick={() => setMobile(true)}>
            <Icon name="menu" />
          </button>
          <div className="hidden lg:block"><TenantSwitcher /></div>
          <GlobalSearch devices={devices.data} sites={sites.data} />
          <div className="flex-1" />
          <button type="button" onClick={() => nav("/alerts")} title="Alarme" aria-label={`Alarme – ${alarmCount} aktiv`}
            className="relative flex h-[34px] w-[34px] shrink-0 cursor-pointer items-center justify-center rounded-[7px] border border-line bg-panel text-[17px] text-fg2 hover:border-line-strong hover:text-fg">
            <Icon name="bell" />
            {alarmCount > 0 && <span className="absolute -right-[7px] -top-1.5 flex h-[18px] min-w-[18px] items-center justify-center rounded-full border-2 border-panel bg-red px-1 text-[11px] font-semibold text-white">{alarmCount}</span>}
          </button>
          <div className="hidden sm:block"><ThemeSwitch /></div>
          <div className="hidden h-6 w-px bg-line sm:block" />
          <UserMenu />
        </header>
        <main id="main" className="flex min-w-0 flex-1 flex-col p-4 sm:p-6">
          <div className="mb-4 lg:hidden"><TenantSwitcher /></div>
          {children}
        </main>
      </div>
      <AlertToasts />
    </div>
  );
}
