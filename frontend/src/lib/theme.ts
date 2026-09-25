import { useEffect, useState } from "react";

export type ThemePref = "light" | "dark" | "system";
const KEY = "fm.theme";
const mq = () => window.matchMedia?.("(prefers-color-scheme: dark)");

function read(): ThemePref {
  try {
    const v = localStorage.getItem(KEY);
    return v === "light" || v === "dark" ? v : "system";
  } catch {
    return "system";
  }
}

function apply(pref: ThemePref) {
  const dark = pref === "dark" || (pref === "system" && !!mq()?.matches);
  document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
}

const subs = new Set<(p: ThemePref) => void>();
let current: ThemePref = typeof window !== "undefined" ? read() : "system";

export function setTheme(pref: ThemePref) {
  current = pref;
  try {
    localStorage.setItem(KEY, pref);
  } catch {
    /* privater Modus */
  }
  apply(pref);
  subs.forEach((s) => s(pref));
}

// "System" folgt der Betriebssystem-Einstellung live
mq()?.addEventListener?.("change", () => current === "system" && apply("system"));

export function useTheme(): [ThemePref, (p: ThemePref) => void] {
  const [p, setP] = useState(current);
  useEffect(() => {
    subs.add(setP);
    return () => void subs.delete(setP);
  }, []);
  return [p, setTheme];
}
