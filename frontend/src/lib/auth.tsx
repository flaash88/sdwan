import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, session } from "./api";
import type { Role, Tenant, User } from "./types";

interface Me {
  user: User;
  active_tenant_id: string | null;
  role: Role;
  tenants: Tenant[];
}

interface AuthState {
  me: Me | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
  switchTenant: (id: string | null) => void;
  can: (role: Role) => boolean;
  reload: () => Promise<void>;
}

const Ctx = createContext<AuthState | null>(null);
const RANK: Record<Role, number> = { readonly: 0, technician: 1, admin: 2 };

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);

  const reload = useCallback(async () => {
    if (!session.token) {
      setMe(null);
      setLoading(false);
      return;
    }
    try {
      setMe(await api.get<Me>("/auth/me"));
    } catch {
      setMe(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const login = async (email: string, password: string) => {
    const r = await api.post<{ access_token: string; user: User }>("/auth/login", { email, password });
    session.token = r.access_token;
    session.tenant = r.user.is_superuser ? null : r.user.tenant_id;
    await reload();
  };

  const logout = () => {
    session.token = null;
    session.tenant = null;
    setMe(null);
    location.href = "/login";
  };

  const switchTenant = (id: string | null) => {
    session.tenant = id;
    location.reload();
  };

  const can = (role: Role) => !!me && RANK[me.role] >= RANK[role];

  return <Ctx.Provider value={{ me, loading, login, logout, switchTenant, can, reload }}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthState {
  const v = useContext(Ctx);
  if (!v) throw new Error("AuthProvider fehlt");
  return v;
}
