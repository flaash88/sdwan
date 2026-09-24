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
