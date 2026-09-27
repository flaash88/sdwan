import type { Tone } from "../components/ui";
import { useFetch } from "./useFetch";

export interface DeviceAdvisory { id: string; cve: string; title: string; severity: "low" | "medium" | "high" | "critical"; function: string; fixed_in: string | null; status: "affected" | "possible"; link: string | null }

export const SEV_TONE: Record<string, Tone> = { critical: "red", high: "red", medium: "orange", low: "gray" };
export const SEV_LABEL: Record<string, string> = { critical: "kritisch", high: "hoch", medium: "mittel", low: "niedrig" };

/** Je Gerät die zutreffenden Sicherheitsmeldungen (Phase 23). */
export function useFleetAdvisories() {
  return useFetch<Record<string, DeviceAdvisory[]>>("/advisories/fleet");
}

/** Schwerste Meldung eines Geräts (betroffen vor möglich). */
export function worst(list: DeviceAdvisory[] | undefined): DeviceAdvisory | null {
  if (!list?.length) return null;
  const rank = { critical: 3, high: 2, medium: 1, low: 0 };
  return [...list].sort((a, b) => (a.status === b.status ? rank[b.severity] - rank[a.severity] : a.status === "affected" ? -1 : 1))[0];
}

/** Höchste Version (numerisch, z. B. 7.16.1 > 7.9). */
export function maxVersion(vs: (string | null | undefined)[]): string | null {
  const key = (v: string) => (v.match(/\d+/g) ?? []).slice(0, 3).map((n) => n.padStart(4, "0")).join(".");
  const list = vs.filter((v): v is string => !!v);
  return list.length ? list.sort((a, b) => (key(a) < key(b) ? -1 : 1))[list.length - 1] : null;
}
