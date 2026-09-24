import type { ReactNode } from "react";
import ContentFilter from "./pages/ContentFilter";
import Mesh from "./pages/Mesh";
import Policies, { PolicyDetail } from "./pages/Policies";
import RemoteSessions from "./pages/RemoteSessions";
import ZeroTouch from "./pages/ZeroTouch";

/** Routen späterer Phasen (Mesh, Policies, Alerts, …). */
export const extraRoutes: { path: string; element: ReactNode }[] = [
  { path: "/mesh", element: <Mesh /> },
  { path: "/policies", element: <Policies /> },
  { path: "/policies/:id", element: <PolicyDetail /> },
  { path: "/ztp", element: <ZeroTouch /> },
  { path: "/content-filter", element: <ContentFilter /> },
  { path: "/remote", element: <RemoteSessions /> },
];
