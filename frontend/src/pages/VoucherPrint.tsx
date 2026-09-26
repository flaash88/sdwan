import { useParams } from "react-router-dom";
import { ErrorBox, Loading } from "../components/ui";
import { useFetch } from "../lib/useFetch";

interface Data {
  instance: string; note: string | null; login_url: string; title: string | null; design: { primary?: string; logo?: string | null };
  profile: { name: string; validity_min: number; data_limit_mb: number | null };
  vouchers: { code: string; qr_svg: string }[];
}
const dur = (m: number) => m % 1440 === 0 ? `${m / 1440} Tag(e) / day(s)` : m % 60 === 0 ? `${m / 60} h` : `${m} min`;

/** A4-Druckansicht: 10 Voucher-Karten je Seite mit Code und QR-Code (Login-URL mit Code). */
export default function VoucherPrint() {
  const { id } = useParams();
  const d = useFetch<Data>(`/hotspot/batches/${id}/print`);
  if (!d.data) return <div className="p-8">{d.error ? <ErrorBox error={d.error} /> : <Loading rows={3} />}</div>;
  const x = d.data;
  return (
    <div className="min-h-screen bg-white text-black">
      <style>{"@page{size:A4;margin:10mm}@media print{.card{break-inside:avoid}}"}</style>
      <div className="mx-auto max-w-[190mm] p-6 print:p-0">
        <div className="mb-4 flex items-center justify-between print:hidden">
          <span className="text-sm">{x.vouchers.length} Voucher · {x.instance} · {x.profile.name}</span>
          <button type="button" className="rounded-md border px-3 py-1.5 text-sm" onClick={() => window.print()}>Drucken</button>
        </div>
        <div className="grid grid-cols-2 gap-[4mm]">
          {x.vouchers.map((v) => (
            <div key={v.code} className="card flex h-[52mm] items-center gap-4 rounded-lg border-2 p-4" style={{ borderColor: x.design.primary ?? "#333" }}>
              <div className="h-[40mm] w-[40mm] shrink-0 [&_svg]:h-full [&_svg]:w-full" dangerouslySetInnerHTML={{ __html: v.qr_svg }} />
              <div className="flex min-w-0 flex-col gap-1">
                {x.design.logo ? <img src={x.design.logo} alt="" className="max-h-8 max-w-[40mm] object-contain object-left" /> : <span className="text-sm font-semibold">{x.title ?? "WLAN"}</span>}
                <span className="text-xs text-neutral-500">Code</span>
                <span className="font-mono text-2xl font-bold tracking-wider">{v.code}</span>
                <span className="text-xs">{dur(x.profile.validity_min)}{x.profile.data_limit_mb ? ` · ${x.profile.data_limit_mb} MB` : ""}</span>
                <span className="truncate text-[10px] text-neutral-500">{x.login_url}</span>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
