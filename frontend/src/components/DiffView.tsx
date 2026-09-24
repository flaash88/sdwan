import { cls } from "./ui";

export default function DiffView({ lines }: { lines: string[] }) {
  if (!lines.length) return <p className="py-6 text-center text-sm text-slate-400">Keine Unterschiede</p>;
  return (
    <pre className="max-h-[32rem] overflow-auto rounded-lg border border-slate-200 bg-slate-50 p-2 font-mono text-xs leading-5">
      {lines.map((l, i) => (
        <div key={i} className={cls(
          "whitespace-pre-wrap break-all px-1",
          l.startsWith("+++") || l.startsWith("---") ? "text-slate-500" : l.startsWith("+") ? "bg-emerald-100 text-emerald-900" : l.startsWith("-") ? "bg-red-100 text-red-900" : l.startsWith("@@") ? "text-sky-700" : "text-slate-700",
        )}>{l || " "}</div>
      ))}
    </pre>
  );
}
