import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";
import { useAuth } from "../lib/auth";
import { useLiveConnected } from "../lib/live";
import { NAV } from "../nav";
import { cls } from "./ui";

export default function Layout({ children }: { children: ReactNode }) {
  const { me, logout, switchTenant, can } = useAuth();
  const live = useLiveConnected();
  if (!me) return null;
  const items = NAV.filter((n) => (!n.superuser || me.user.is_superuser) && (!n.role || can(n.role)));
  return (
    <div className="flex min-h-screen">
      <aside className="flex w-60 shrink-0 flex-col bg-slate-900 text-slate-300">
        <div className="flex items-center gap-2 px-5 py-5">
          <img src="/favicon.svg" className="h-7 w-7" alt="" />
          <div>
            <div className="font-semibold text-white">SD-WAN Control</div>
            <div className="text-xs text-slate-500">MikroTik Fleet</div>
          </div>
        </div>
        {me.user.is_superuser ? (
          <div className="px-4 pb-4">
            <select
              value={me.active_tenant_id ?? ""}
              onChange={(e) => switchTenant(e.target.value || null)}
              className="w-full rounded-lg border border-slate-700 bg-slate-800 px-2 py-1.5 text-sm text-white"
            >
              <option value="">Alle Mandanten (MSP)</option>
              {me.tenants.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </select>
          </div>
        ) : (
          <div className="px-5 pb-4 text-sm text-slate-400">{me.tenants[0]?.name}</div>
        )}
        <nav className="flex-1 space-y-0.5 px-3">
          {items.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.to === "/"}
              className={({ isActive }) => cls("flex items-center gap-3 rounded-lg px-3 py-2 text-sm", isActive ? "bg-brand-700 text-white" : "hover:bg-slate-800 hover:text-white")}
            >
              <span className="w-4 text-center">{n.icon}</span>
              {n.label}
            </NavLink>
          ))}
        </nav>
        <div className="border-t border-slate-800 px-5 py-4 text-xs">
          <div className="flex items-center gap-2">
            <span className={cls("h-2 w-2 rounded-full", live ? "bg-emerald-400" : "bg-slate-600")} />
            {live ? "Live verbunden" : "Live getrennt"}
          </div>
          <div className="mt-2 truncate text-slate-400">{me.user.email}</div>
          <div className="text-slate-500">{me.user.is_superuser ? "MSP-Admin" : me.role}</div>
          <button onClick={logout} className="mt-2 text-slate-400 hover:text-white">
            Abmelden
          </button>
        </div>
      </aside>
      <main className="min-w-0 flex-1 p-8">{children}</main>
    </div>
  );
}
