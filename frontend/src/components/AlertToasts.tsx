import { useState } from "react";
import { useLive } from "../lib/live";
import { cls } from "./ui";

interface Toast { id: number; title: string; text: string; tone: "red" | "green" | "yellow" }

/** Live-Benachrichtigungen für neue und behobene Alarme. */
export default function AlertToasts() {
  const [toasts, setToasts] = useState<Toast[]>([]);
  useLive((e) => {
    const d = e.data as { message: string; severity?: string; rule?: string };
    const t: Toast = { id: Date.now() + Math.random(), title: e.type === "alert.firing" ? `Alarm: ${d.rule}` : "Behoben", text: d.message, tone: e.type === "alert.resolved" ? "green" : d.severity === "critical" ? "red" : "yellow" };
    setToasts((x) => [...x.slice(-3), t]);
    setTimeout(() => setToasts((x) => x.filter((y) => y.id !== t.id)), 8000);
  }, ["alert.firing", "alert.resolved"]);
  return (
    <div className="fixed bottom-4 right-4 z-50 space-y-2">
      {toasts.map((t) => (
        <div key={t.id} className={cls("w-80 rounded-lg border-l-4 bg-white p-3 text-sm shadow-lg", t.tone === "red" ? "border-red-500" : t.tone === "green" ? "border-emerald-500" : "border-amber-500")}>
          <div className="font-semibold">{t.title}</div>
          <div className="text-slate-600">{t.text}</div>
        </div>
      ))}
    </div>
  );
}
