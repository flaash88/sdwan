import type { ComponentType } from "react";
import type { Device } from "./lib/types";
import MetricsTab from "./tabs/MetricsTab";
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
];
