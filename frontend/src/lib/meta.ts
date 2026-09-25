import { useEffect, useState } from "react";
import { api } from "./api";

export interface Meta {
  version: string;
  simulator: boolean;
  grafana_url: string;
  hub_endpoint: string;
  management_network: string;
  smtp_configured: boolean;
  product_name: string;
  product_short: string;
}

let cache: Promise<Meta> | null = null;

export function useMeta(): Meta | null {
  const [m, setM] = useState<Meta | null>(null);
  useEffect(() => {
    cache ??= api.get<Meta>("/meta");
    cache.then((x) => { setM(x); if (x.product_name) document.title = x.product_name; }).catch(() => undefined);
  }, []);
  return m;
}
