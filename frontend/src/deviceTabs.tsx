import type { ComponentType } from "react";
import type { Device } from "./lib/types";
import BackupsTab from "./tabs/BackupsTab";
import MetricsTab from "./tabs/MetricsTab";
import PoliciesTab from "./tabs/PoliciesTab";
import RemoteTab from "./tabs/RemoteTab";
import WanTab from "./tabs/WanTab";

export interface DeviceTab {
  key: string;
  label: string;
  pairedOnly?: boolean;
  component: ComponentType<{ device: Device }>;
}

/** Tabs späterer Phasen (WAN, Metriken, Backups, Remote-Zugriff, …). */
export const deviceTabs: DeviceTab[] = [
  { key: "metrics", label: "Metriken", pairedOnly: true, component: MetricsTab },
  { key: "wan", label: "WAN", pairedOnly: true, component: WanTab },
  { key: "policies", label: "Firewall", pairedOnly: true, component: PoliciesTab },
  { key: "remote", label: "Fernzugriff", pairedOnly: true, component: RemoteTab },
  { key: "backups", label: "Backups", pairedOnly: true, component: BackupsTab },
];
