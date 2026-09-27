// Audit B.8 – statische Prüfungen des Frontends (Token-Speicher, HTML-Einbettung, Links, eval).
// Aufruf: node frontend/audit/static_checks.mjs   → Ausgabe je Treffer; Exit 0 (reiner Bericht).
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const root = new URL("../src/", import.meta.url).pathname;
const checks = [
  ["Token im localStorage (XSS-lesbar)", /localStorage\.(get|set)Item\(\s*TOKEN_KEY/],
  ["dangerouslySetInnerHTML", /dangerouslySetInnerHTML/],
  ["target=_blank ohne rel=noopener", /target="_blank"(?![^>]*rel=)/],
  ["eval/new Function", /\beval\(|new Function\(/],
  ["Token in URL-Query", /[?&](token|access_token)=/],
];
const files = [];
(function walk(d) {
  for (const f of readdirSync(d)) {
    const p = join(d, f);
    if (statSync(p).isDirectory()) walk(p);
    else if (/\.(tsx?|jsx?)$/.test(f)) files.push(p);
  }
})(root);
for (const [name, re] of checks) {
  const hits = [];
  for (const f of files) readFileSync(f, "utf8").split("\n").forEach((l, i) => { if (re.test(l)) hits.push(`${relative(root, f)}:${i + 1}`); });
  console.log(`${hits.length ? "TREFFER" : "ok     "} ${name}${hits.length ? ": " + hits.join(", ") : ""}`);
}
