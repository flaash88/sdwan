import { useEffect, useState } from "react";
import { api } from "./api";

export interface Meta {
  version: string;
  simulator: boolean;
  grafana_url: string;
  hub_endpoint: string;
  management_network: string;
  smtp_configured: boolean;
}

let cache: Promise<Meta> | null = null;

export function useMeta(): Meta | null {
  const [m, setM] = useState<Meta | null>(null);
  useEffect(() => {
    cache ??= api.get<Meta>("/meta");
    cache.then(setM).catch(() => undefined);
  }, []);
  return m;
}
