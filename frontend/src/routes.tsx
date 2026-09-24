import type { ReactNode } from "react";
import Alerts from "./pages/Alerts";
import ContentFilter from "./pages/ContentFilter";
import Firmware from "./pages/Firmware";
import Mesh from "./pages/Mesh";
import Policies, { PolicyDetail } from "./pages/Policies";
import RemoteSessions from "./pages/RemoteSessions";
import Reports from "./pages/Reports";
import ZeroTouch from "./pages/ZeroTouch";

/** Routen späterer Phasen (Mesh, Policies, Alerts, …). */
export const extraRoutes: { path: string; element: ReactNode }[] = [
  { path: "/mesh", element: <Mesh /> },
  { path: "/policies", element: <Policies /> },
  { path: "/policies/:id", element: <PolicyDetail /> },
  { path: "/ztp", element: <ZeroTouch /> },
  { path: "/content-filter", element: <ContentFilter /> },
  { path: "/remote", element: <RemoteSessions /> },
  { path: "/firmware", element: <Firmware /> },
  { path: "/alerts", element: <Alerts /> },
  { path: "/reports", element: <Reports /> },
];
