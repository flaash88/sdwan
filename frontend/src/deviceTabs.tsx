import type { ComponentType } from "react";
import type { Device } from "./lib/types";
import WanTab from "./tabs/WanTab";

export interface DeviceTab {
  key: string;
  label: string;
  pairedOnly?: boolean;
  component: ComponentType<{ device: Device }>;
}

/** Tabs späterer Phasen (WAN, Metriken, Backups, Remote-Zugriff, …). */
export const deviceTabs: DeviceTab[] = [
  { key: "wan", label: "WAN", pairedOnly: true, component: WanTab },
];
