import { Link } from "react-router-dom";
import { SEV_LABEL, SEV_TONE, maxVersion, type DeviceAdvisory } from "../lib/advisories";
import type { Device } from "../lib/types";
import { useFetch } from "../lib/useFetch";
import { Card, Notice, Pill } from "./ui";

/** Gerätedetail: zutreffende Sicherheitsmeldungen; nur sichtbar, wenn es welche gibt. */
export default function DeviceAdvisories({ device }: { device: Device }) {
  const data = useFetch<{ version: string | null; advisories: (DeviceAdvisory & { description: string | null; affected_from: string })[] }>(`/devices/${device.id}/advisories`);
  const list = data.data?.advisories ?? [];
  if (!list.length) return null;
  const affected = list.some((a) => a.status === "affected");
  const fix = maxVersion(list.map((a) => a.fixed_in));
  return (
    <Card title="Sicherheitsmeldungen" subtitle={`RouterOS ${data.data?.version ?? "?"}`} actions={<Link to="/firmware" className="text-sm text-blue-text hover:underline">Firmware aktualisieren</Link>}>
      {affected && <div className="mb-3"><Notice tone="red" icon="alert">Dieses Gerät ist von bekannten Sicherheitsmeldungen betroffen{fix ? ` – behoben ab RouterOS ${fix}` : ""}. Funktionen mit schwerwiegenden Meldungen (Hotspot, WLAN …) lassen sich bis zum Update nicht neu ausrollen.</Notice></div>}
      <ul className="flex flex-col gap-2">
        {list.map((a) => (
          <li key={a.id} className="flex flex-wrap items-center gap-2 text-sm">
            <Pill tone={a.status === "affected" ? SEV_TONE[a.severity] : "gray"}>{SEV_LABEL[a.severity]}</Pill>
            <span className="font-mono">{a.link ? <a href={a.link} target="_blank" rel="noreferrer" className="hover:underline">{a.cve}</a> : a.cve}</span>
            <span>{a.title}</span>
            <span className="text-xs text-fg3">Funktion {a.function}{a.status === "possible" ? " – möglicherweise (Funktion nicht feststellbar)" : ""}{a.fixed_in ? ` · behoben in ${a.fixed_in}` : ""}</span>
          </li>
        ))}
      </ul>
    </Card>
  );
}
