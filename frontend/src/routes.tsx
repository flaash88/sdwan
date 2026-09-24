import type { ReactNode } from "react";
import Mesh from "./pages/Mesh";
import Policies, { PolicyDetail } from "./pages/Policies";

/** Routen späterer Phasen (Mesh, Policies, Alerts, …). */
export const extraRoutes: { path: string; element: ReactNode }[] = [
  { path: "/mesh", element: <Mesh /> },
  { path: "/policies", element: <Policies /> },
  { path: "/policies/:id", element: <PolicyDetail /> },
];
