import type { ReactNode } from "react";
import Mesh from "./pages/Mesh";

/** Routen späterer Phasen (Mesh, Policies, Alerts, …). */
export const extraRoutes: { path: string; element: ReactNode }[] = [
  { path: "/mesh", element: <Mesh /> },
];
