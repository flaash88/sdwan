import type { CSSProperties } from "react";

/** SVG-Icons (Pfade aus dem Design-Prototyp, Lucide-Stil). */
const P = {
  grid:["M3 3h7v7H3z","M14 3h7v7h-7z","M14 14h7v7h-7z","M3 14h7v7H3z"],
  router:["M3 14h18v6H3z","M6.5 17h.01","M10 17h.01","M15 10v4","M17.84 7.17a4 4 0 0 0-5.66 0","M20.66 4.34a8 8 0 0 0-11.31 0"],
  pin:["M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z","M15 10a3 3 0 1 1-6 0a3 3 0 1 1 6 0"],
  network:["M9 2h6v6H9z","M2 16h6v6H2z","M16 16h6v6h-6z","M5 16v-3a1 1 0 0 1 1-1h12a1 1 0 0 1 1 1v3","M12 12V8"],
  shield:["M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"],
  package:["M21 8 12 3 3 8v8l9 5 9-5z","M3 8l9 5 9-5","M12 13v8"],
  filter:["M22 3H2l8 9.46V19l4 2v-8.54z"],
  cpu:["M6 6h12v12H6z","M9 9h6v6H9z","M9 2v4","M15 2v4","M9 18v4","M15 18v4","M2 9h4","M2 15h4","M18 9h4","M18 15h4"],
  bell:["M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9","M10.3 21a1.94 1.94 0 0 0 3.4 0"],
  chart:["M3 3v18h18","M18 17V9","M13 17V5","M8 17v-3"],
  terminal:["M4 17l6-6-6-6","M12 19h8"],
  list:["M8 6h13","M8 12h13","M8 18h13","M3 6h.01","M3 12h.01","M3 18h.01"],
  users:["M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2","M13 7a4 4 0 1 1-8 0a4 4 0 1 1 8 0","M22 21v-2a4 4 0 0 0-3-3.87","M16 3.13a4 4 0 0 1 0 7.75"],
  building:["M6 22V4a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v18Z","M6 12H4a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2h2","M18 9h2a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-2","M10 6h4","M10 10h4","M10 14h4","M10 18h4"],
  search:["M19 11a8 8 0 1 1-16 0a8 8 0 1 1 16 0","M21 21l-4.3-4.3"],
  sun:["M16 12a4 4 0 1 1-8 0a4 4 0 1 1 8 0","M12 2v2","M12 20v2","M4.93 4.93l1.41 1.41","M17.66 17.66l1.41 1.41","M2 12h2","M20 12h2","M6.34 17.66l-1.41 1.41","M19.07 4.93l-1.41 1.41"],
  moon:["M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"],
  monitor:["M2 4h20v13H2z","M8 21h8","M12 17v4"],
  chevDown:["M6 9l6 6 6-6"], chevRight:["M9 18l6-6-6-6"],
  chevsLeft:["M11 17l-5-5 5-5","M18 17l-5-5 5-5"], chevsRight:["M13 17l5-5-5-5","M6 17l5-5-5-5"],
  check:["M20 6 9 17l-5-5"],
  checkCircle:["M22 12a10 10 0 1 1-20 0a10 10 0 1 1 20 0","M8.5 12l2.5 2.5 4.5-5"],
  alert:["M21.73 18l-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3","M12 9v4","M12 17h.01"],
  xCircle:["M22 12a10 10 0 1 1-20 0a10 10 0 1 1 20 0","M15 9l-6 6","M9 9l6 6"],
  octagon:["M7.86 2h8.28L22 7.86v8.28L16.14 22H7.86L2 16.14V7.86z","M12 8v4","M12 16h.01"],
  minusCircle:["M22 12a10 10 0 1 1-20 0a10 10 0 1 1 20 0","M8 12h8"],
  info:["M22 12a10 10 0 1 1-20 0a10 10 0 1 1 20 0","M12 16v-4","M12 8h.01"],
  clock:["M22 12a10 10 0 1 1-20 0a10 10 0 1 1 20 0","M12 6v6l4 2"],
  copy:["M8 8h13v13H8z","M4 16a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2"],
  play:["M6 4l14 8-14 8z"],
  archive:["M2 3h20v5H2z","M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8","M10 12h4"],
  power:["M12 2v10","M18.4 6.6a9 9 0 1 1-12.77.04"],
  rotate:["M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8","M3 3v5h5"],
  plus:["M12 5v14","M5 12h14"],
  pause:["M9 5v14","M15 5v14"],
  signal:["M2 20h.01","M7 20v-4","M12 20v-8","M17 20V8","M22 4v16"],
  cable:["M4 9a2 2 0 0 1-2-2V5h6v2a2 2 0 0 1-2 2Z","M3 5V3","M7 5V3","M19 15V6.5a3.5 3.5 0 0 0-7 0v11a3.5 3.5 0 0 1-7 0V9","M17 21v-2","M21 21v-2","M22 19h-6v-2a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2Z"],
  x:["M18 6 6 18","M6 6l12 12"],
  lock:["M5 11h14v10H5z","M7 11V7a5 5 0 0 1 10 0v4"],
  download:["M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4","M7 10l5 5 5-5","M12 15V3"],
  upload:["M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4","M17 8l-5-5-5 5","M12 3v12"],
  arrowRight:["M5 12h14","M12 5l7 7-7 7"],
  loader:["M21 12a9 9 0 1 1-6.22-8.56"],
  activity:["M22 12h-4l-3 9L9 3l-3 9H2"],
  globe:["M22 12a10 10 0 1 1-20 0a10 10 0 1 1 20 0","M2 12h20","M12 2a15 15 0 0 1 0 20a15 15 0 0 1 0-20"],
  menu:["M4 6h16","M4 12h16","M4 18h16"],
  logout:["M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4","M16 17l5-5-5-5","M21 12H9"],
  edit:["M12 20h9","M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"],
  trash:["M3 6h18","M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6","M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"],
  external:["M15 3h6v6","M10 14 21 3","M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"],
  settings:["M4 21v-7","M4 10V3","M12 21v-9","M12 8V3","M20 21v-5","M20 12V3","M1 14h6","M9 8h6","M17 16h6"],
  user:["M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2","M16 7a4 4 0 1 1-8 0a4 4 0 1 1 8 0"],
  file:["M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z","M14 2v6h6"],
  chevUp:["M18 15l-6-6-6 6"],
  key:["M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.78 7.78 5.5 5.5 0 0 1 7.78-7.78Zm0 0L15.5 7.5m0 0 3 3L22 7l-3-3m-3.5 3.5L19 4"],
} as const;

export type IconName = keyof typeof P;

/** Inline-SVG-Icon, erbt Farbe (currentColor) und Größe (1em). */
export function Icon({ name, className, style, title }: { name: IconName; className?: string; style?: CSSProperties; title?: string }) {
  return (
    <svg width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round"
      className={className} style={{ display: "block", flex: "none", ...style }} aria-hidden={title ? undefined : true} role={title ? "img" : undefined}>
      {title && <title>{title}</title>}
      {P[name].map((d, k) => <path key={k} d={d} />)}
    </svg>
  );
}
