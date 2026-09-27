import type { ReactNode } from "react";
import Alerts from "./pages/Alerts";
import Compliance from "./pages/Compliance";
import ConfigSearch from "./pages/ConfigSearch";
import ContentFilter from "./pages/ContentFilter";
import Feeds from "./pages/Feeds";
import Firmware from "./pages/Firmware";
import FwObjects from "./pages/FwObjects";
import Mesh from "./pages/Mesh";
import Policies, { PolicyDetail } from "./pages/Policies";
import RemoteSessions from "./pages/RemoteSessions";
import Reports from "./pages/Reports";
import Scripts, { ScriptRunPage } from "./pages/Scripts";
import Maintenance from "./pages/Maintenance";
import OffboardingArchive from "./pages/OffboardingArchive";
import PlatformBackup from "./pages/PlatformBackup";
import Wlan from "./pages/Wlan";
import WlanPrint from "./pages/WlanPrint";
import Hotspot from "./pages/Hotspot";
import VoucherPrint from "./pages/VoucherPrint";
import ZeroTouch from "./pages/ZeroTouch";

/** Routen späterer Phasen (Mesh, Policies, Alerts, …). */
export const extraRoutes: { path: string; element: ReactNode }[] = [
  { path: "/mesh", element: <Mesh /> },
  { path: "/policies", element: <Policies /> },
  { path: "/policies/objects", element: <FwObjects /> },
  { path: "/policies/:id", element: <PolicyDetail /> },
  { path: "/ztp", element: <ZeroTouch /> },
  { path: "/feeds", element: <Feeds /> },
  { path: "/compliance", element: <Compliance /> },
  { path: "/config-search", element: <ConfigSearch /> },
  { path: "/scripts", element: <Scripts /> },
  { path: "/maintenance", element: <Maintenance /> },
  { path: "/offboarding", element: <OffboardingArchive /> },
  { path: "/platform/backup", element: <PlatformBackup /> },
  { path: "/wlan", element: <Wlan /> },
  { path: "/hotspot", element: <Hotspot /> },
  { path: "/scripts/runs/:id", element: <ScriptRunPage /> },
  { path: "/content-filter", element: <ContentFilter /> },
  { path: "/remote", element: <RemoteSessions /> },
  { path: "/firmware", element: <Firmware /> },
  { path: "/alerts", element: <Alerts /> },
  { path: "/reports", element: <Reports /> },
];

/** Druckansichten ohne Navigation (A4). */
export const printRoutes: { path: string; element: ReactNode }[] = [
  { path: "/print/wlan/:id", element: <WlanPrint /> },
  { path: "/print/vouchers/:id", element: <VoucherPrint /> },
];
