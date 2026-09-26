import { useFetch } from "./useFetch";

/** Firewall-Editor (Phase 14): Typen und Katalog. */
export interface FwObject { id: string; name: string; slug: string; kind: "host" | "network" | "range" | "group" | "feed"; values: string[]; members: string[]; scope: "global" | "tenant"; builtin: boolean; description: string | null }
export interface FwService { id: string; name: string; slug: string; entries: { protocol: string; ports: string }[]; members: string[]; scope: "global" | "tenant"; builtin: boolean; description: string | null }
export interface FwZone { id: string; name: string; slug: string; source: "manual" | "wan"; management: boolean; scope: "global" | "tenant"; builtin: boolean; description: string | null }
export interface FwBlockParam { key: string; type: "zone" | "object" | "service"; label: string; optional?: boolean; default?: string }
export interface FwBlock { id: string; name: string; description: string | null; params: FwBlockParam[]; rules: unknown[]; nat: unknown[]; scope: "global" | "tenant"; builtin: boolean }
export interface FwCatalog { objects: FwObject[]; services: FwService[]; zones: FwZone[]; blocks: FwBlock[] }

export interface SpecRule { id: string; enabled: boolean; src_zone: string | null; src: string[]; dst_zone: string | null; dst: string[]; services: string[]; action: "accept" | "drop" | "reject"; log: boolean; comment: string; block?: string }
export interface SpecNat { id: string; type: "masquerade" | "portforward" | "redirect"; enabled: boolean; comment?: string; zone?: string | null; in_zone?: string | null; protocol?: string; ext_port?: string; host?: string; int_port?: string; port?: string }
export interface Spec { options: { baseline: boolean; default_drop: boolean }; rules: SpecRule[]; nat: SpecNat[]; raw: { address_lists: unknown[]; filter: Record<string, string>[]; nat: Record<string, string>[] } }
export interface LintIssue { level: "error" | "warn" | "info"; code: string; message: string; rule_id: string | null }
export interface Preview { commands: string[]; diff: { added: number; removed: number; lines: string[] }; deployed_version: number | null; lint: LintIssue[]; placement: string }
export interface Hit { packets: number; bytes: number; last_hit_at: string | null; tracking_since: string; devices: number }

export const ROUTER = "router";
export const ACTION_LABEL: Record<SpecRule["action"], string> = { accept: "erlauben", drop: "verwerfen", reject: "ablehnen" };
export const KIND_LABEL: Record<FwObject["kind"], string> = { host: "Host", network: "Netz", range: "Bereich", group: "Gruppe", feed: "Threat-Feed" };

export const emptySpec = (): Spec => ({ options: { baseline: true, default_drop: true }, rules: [], nat: [], raw: { address_lists: [], filter: [], nat: [] } });
export const newId = () => Math.random().toString(16).slice(2, 10).padEnd(8, "0");

export function useFwCatalog() {
  return useFetch<FwCatalog>("/fw/catalog");
}

export function serviceText(s: FwService, all: FwService[]): string {
  if (s.members.length) return s.members.map((m) => all.find((x) => x.id === m)?.name ?? "?").join(", ");
  return s.entries.map((e) => (e.protocol || "alle") + (e.ports ? ` ${e.ports}` : "")).join(", ");
}
