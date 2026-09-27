import { useEffect, useState } from "react";
import { api, session } from "./api";

/** Öffentlich: Version, Produktname, Mindestversion. Betriebsdaten nur angemeldet über /meta/full (AUDIT-025). */
export interface Meta {
  version: string;
  simulator?: boolean;
  grafana_url?: string;
  hub_endpoint?: string;
  management_network?: string;
  smtp_configured?: boolean;
  product_name: string;
  product_short: string;
  onboarding_min_routeros?: string;
}

let cache: Promise<Meta> | null = null;
let cachedFor: string | null = null;

export function useMeta(): Meta | null {
  const [m, setM] = useState<Meta | null>(null);
  useEffect(() => {
    const tok = session.token;
    if (cachedFor !== tok) { cache = null; cachedFor = tok; }
    cache ??= tok ? api.get<Meta>("/meta/full").catch(() => api.get<Meta>("/meta")) : api.get<Meta>("/meta");
    cache.then((x) => { setM(x); if (x.product_name) document.title = x.product_name; }).catch(() => undefined);
  }, []);
  return m;
}
