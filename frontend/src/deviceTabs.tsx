import type { ComponentType } from "react";
import type { Device } from "./lib/types";

export interface DeviceTab {
  key: string;
  label: string;
  pairedOnly?: boolean;
  component: ComponentType<{ device: Device }>;
}

/** Tabs späterer Phasen (WAN, Metriken, Backups, Remote-Zugriff, …). */
export const deviceTabs: DeviceTab[] = [];
