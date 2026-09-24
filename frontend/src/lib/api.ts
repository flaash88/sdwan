const TOKEN_KEY = "sdwan.token";
const TENANT_KEY = "sdwan.tenant";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export const session = {
  get token() {
    return localStorage.getItem(TOKEN_KEY);
  },
  set token(v: string | null) {
    if (v) localStorage.setItem(TOKEN_KEY, v);
    else localStorage.removeItem(TOKEN_KEY);
  },
  get tenant() {
    return localStorage.getItem(TENANT_KEY);
  },
  set tenant(v: string | null) {
    if (v) localStorage.setItem(TENANT_KEY, v);
    else localStorage.removeItem(TENANT_KEY);
  },
};

async function request<T>(method: string, path: string, body?: unknown, raw = false): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (session.token) headers["Authorization"] = `Bearer ${session.token}`;
  if (session.tenant) headers["X-Tenant-ID"] = session.tenant;
  const res = await fetch(`/api/v1${path}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) {
    session.token = null;
    if (!location.pathname.startsWith("/login")) location.href = "/login";
  }
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, msg);
  }
  if (res.status === 204) return undefined as T;
  if (raw) return (await res.blob()) as T;
  const ct = res.headers.get("content-type") ?? "";
  return (ct.includes("json") ? await res.json() : await res.text()) as T;
}

export const api = {
  get: <T,>(p: string) => request<T>("GET", p),
  post: <T,>(p: string, b?: unknown) => request<T>("POST", p, b ?? {}),
  put: <T,>(p: string, b?: unknown) => request<T>("PUT", p, b ?? {}),
  patch: <T,>(p: string, b?: unknown) => request<T>("PATCH", p, b ?? {}),
  del: <T,>(p: string) => request<T>("DELETE", p),
  blob: (p: string) => request<Blob>("GET", p, undefined, true),
};

export function wsUrl(): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const q = new URLSearchParams({ token: session.token ?? "" });
  if (session.tenant) q.set("tenant", session.tenant);
  return `${proto}://${location.host}/api/v1/ws?${q}`;
}

export async function download(path: string, filename: string) {
  const blob = await api.blob(path);
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}
