import type { ComponentType } from "react";
import type { Device } from "./lib/types";
import BackupsTab from "./tabs/BackupsTab";
import FlowsTab from "./tabs/FlowsTab";
import LogTab from "./tabs/LogTab";
import MetricsTab from "./tabs/MetricsTab";
import NeighborsTab from "./tabs/NeighborsTab";
import PoliciesTab from "./tabs/PoliciesTab";
import RemoteTab from "./tabs/RemoteTab";
import VrrpTab from "./tabs/VrrpTab";
import WanTab from "./tabs/WanTab";
import WlanTab from "./tabs/WlanTab";

export interface DeviceTab {
  key: string;
  label: string;
  pairedOnly?: boolean;
  /** nur anzeigen, wenn das Gerät die Funktion hat (z. B. WLAN) */
  visible?: (d: Device) => boolean;
  component: ComponentType<{ device: Device }>;
}

/** Tabs späterer Phasen (WAN, Metriken, Backups, Remote-Zugriff, …). */
export const deviceTabs: DeviceTab[] = [
  { key: "wan", label: "WAN", pairedOnly: true, component: WanTab },
  { key: "wlan", label: "WLAN", pairedOnly: true, component: WlanTab, visible: (d) => !!d.facts?.wlan },
  { key: "vrrp", label: "VRRP", pairedOnly: true, component: VrrpTab },
  { key: "metrics", label: "Metriken", pairedOnly: true, component: MetricsTab },
  { key: "flows", label: "Top-Verbraucher", pairedOnly: true, component: FlowsTab },
  { key: "neighbors", label: "Nachbarn", pairedOnly: true, component: NeighborsTab, visible: (d) => Number(d.facts?.neighbor_count ?? 0) > 0 },
  { key: "backups", label: "Backups", pairedOnly: true, component: BackupsTab },
  { key: "policies", label: "Firewall", pairedOnly: true, component: PoliciesTab },
  { key: "remote", label: "Fernzugriff", pairedOnly: true, component: RemoteTab },
  { key: "log", label: "Log", pairedOnly: true, component: LogTab },
];
