import { useParams } from "react-router-dom";
import { ErrorBox, Loading } from "../components/ui";
import { useFetch } from "../lib/useFetch";

interface Cred { name: string; ssid: string; passphrase: string; hidden: boolean; is_guest: boolean; qr_svg: string }

/** Druckansicht A4: WLAN-Zugang mit QR-Code (ohne Navigation). */
export default function WlanPrint() {
  const { id } = useParams();
  const c = useFetch<Cred>(`/wlan/profiles/${id}/credentials`);
  if (!c.data) return <div className="p-8">{c.error ? <ErrorBox error={c.error} /> : <Loading rows={3} />}</div>;
  const d = c.data;
  return (
    <div className="min-h-screen bg-white text-black">
      <div className="mx-auto flex max-w-[180mm] flex-col items-center gap-6 p-10 text-center print:p-0">
        <div className="flex w-full justify-end print:hidden"><button type="button" className="rounded-md border px-3 py-1.5 text-sm" onClick={() => window.print()}>Drucken</button></div>
        <h1 className="text-4xl font-semibold">{d.is_guest ? "Gäste-WLAN" : "WLAN"} · {d.is_guest ? "Guest Wi-Fi" : "Wi-Fi"}</h1>
        <div className="h-72 w-72 [&_svg]:h-full [&_svg]:w-full" dangerouslySetInnerHTML={{ __html: d.qr_svg }} />
        <p className="text-lg">QR-Code mit der Kamera scannen · Scan with your camera</p>
        <table className="text-left text-2xl"><tbody>
          <tr><td className="pr-6 text-neutral-500">Netzwerk / SSID</td><td className="font-mono font-semibold">{d.ssid}{d.hidden && " (versteckt)"}</td></tr>
          <tr><td className="pr-6 text-neutral-500">Passwort</td><td className="font-mono font-semibold">{d.passphrase}</td></tr>
        </tbody></table>
      </div>
    </div>
  );
}
