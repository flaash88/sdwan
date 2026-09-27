import type { PairingInfo } from "../lib/types";
import { fmtDate } from "../lib/format";
import { CopyBox } from "./ui";

export default function PairingBox({ pairing }: { pairing: PairingInfo }) {
  return (
    <div className="space-y-3 text-sm">
      <p>
        Im RouterOS-Terminal (Winbox → New Terminal oder SSH) diesen <b>einen Befehl</b> ausführen. Der Router baut daraufhin
        selbstständig den WireGuard-Tunnel zur Cloud auf – kein Port-Forwarding und keine öffentliche IP nötig.
      </p>
      <CopyBox text={pairing.command} />
      <p className="text-xs text-slate-500">
        Token gültig bis {fmtDate(pairing.expires_at)} · einmalig verwendbar · Voraussetzung: RouterOS ≥ {pairing.min_routeros ?? "7.19"} (Download mit
        Zertifikatsprüfung über den eingebauten Zertifikatsspeicher), ausgehend HTTPS und UDP erlaubt. Ältere Versionen brechen mit einem Hinweis ab – dann zuerst RouterOS aktualisieren.
      </p>
    </div>
  );
}
