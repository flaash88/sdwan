import type { ReactNode } from "react";

/** Routen späterer Phasen (Mesh, Policies, Alerts, …). */
export const extraRoutes: { path: string; element: ReactNode }[] = [];
