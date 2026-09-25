export function fmtDate(v: string | null | undefined): string {
  if (!v) return "–";
  return new Date(v).toLocaleString("de-DE", { dateStyle: "short", timeStyle: "medium" });
}

export function fmtAgo(v: string | null | undefined): string {
  if (!v) return "nie";
  const s = Math.round((Date.now() - new Date(v).getTime()) / 1000);
  if (s < 60) return `vor ${s}s`;
  if (s < 3600) return `vor ${Math.round(s / 60)} min`;
  if (s < 86400) return `vor ${Math.round(s / 3600)} h`;
  return `vor ${Math.round(s / 86400)} d`;
}

export function fmtBytes(n: number | null | undefined): string {
  if (n == null) return "–";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
}

export function fmtBps(n: number | null | undefined): string {
  if (n == null) return "–";
  const u = ["bit/s", "kbit/s", "Mbit/s", "Gbit/s"];
  let i = 0;
  let v = n;
  while (v >= 1000 && i < u.length - 1) {
    v /= 1000;
    i++;
  }
  return `${v.toFixed(1)} ${u[i]}`;
}

/** Dauer menschenlesbar: "45 s", "38 min", "2 h 14 min", "1 T 4 h". */
export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds == null || !isFinite(seconds)) return "–";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  if (s < 86400) { const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60); return m ? `${h} h ${m} min` : `${h} h`; }
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
  return h ? `${d} T ${h} h` : `${d} T`;
}

/** Dauer seit ``from`` bis ``to`` (Standard: jetzt). */
export function fmtSince(from: string | null | undefined, to?: string | null): string {
  if (!from) return "–";
  return fmtDuration(((to ? new Date(to).getTime() : Date.now()) - new Date(from).getTime()) / 1000);
}

/** Kurzform "25.09. 07:48" (heute nur Uhrzeit mit Sekunden, wenn ``seconds``). */
export function fmtShort(v: string | null | undefined): string {
  if (!v) return "–";
  const d = new Date(v);
  return `${d.toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit" })} ${d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" })}`;
}

/** "25.09.2026, 09:24:11" */
export function fmtFull(v: string | null | undefined): string {
  if (!v) return "–";
  return new Date(v).toLocaleString("de-DE", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/** RouterOS-Uptime "27d3h47m53s" bzw. "1w2d3h" -> "27 T 3 h". */
export function fmtUptime(v: string | null | undefined): string {
  if (!v) return "–";
  const m = { w: 604800, d: 86400, h: 3600, m: 60, s: 1 } as Record<string, number>;
  let s = 0;
  for (const [, n, u] of v.matchAll(/(\d+)([wdhms])/g)) s += Number(n) * m[u];
  return s ? fmtDuration(s) : v;
}
