import { Link } from "react-router-dom";
import { SEV_LABEL, SEV_TONE, worst, type DeviceAdvisory } from "../lib/advisories";
import { Pill } from "./ui";

/** Kompakte Anzeige der schwersten Sicherheitsmeldung eines Geräts. */
export default function AdvisoryPill({ list }: { list: DeviceAdvisory[] | undefined }) {
  const w = worst(list);
  if (!w) return null;
  const n = list!.filter((a) => a.status === "affected").length;
  const title = list!.map((a) => `${a.cve} (${SEV_LABEL[a.severity]}, ${a.status === "affected" ? "betroffen" : "möglicherweise"})${a.fixed_in ? ` – behoben in ${a.fixed_in}` : ""}`).join("\n");
  return (
    <Link to="/advisories" onClick={(e) => e.stopPropagation()} title={title}>
      <Pill tone={w.status === "affected" ? SEV_TONE[w.severity] : "gray"} icon="alert">{w.status === "affected" ? `${n} Sicherheitsmeldung${n > 1 ? "en" : ""}` : "möglicherweise betroffen"}</Pill>
    </Link>
  );
}
