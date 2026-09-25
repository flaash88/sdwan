import { useState } from "react";
import { useLive } from "../lib/live";
import { Icon } from "./Icon";
import { cls } from "./ui";

interface Toast { id: number; title: string; text: string; tone: "red" | "green" | "orange" }

/** Live-Benachrichtigungen für neue und behobene Alarme. */
export default function AlertToasts() {
  const [toasts, setToasts] = useState<Toast[]>([]);
  useLive((e) => {
    const d = e.data as { message: string; severity?: string; rule?: string };
    const t: Toast = { id: Date.now() + Math.random(), title: e.type === "alert.firing" ? `Alarm: ${d.rule}` : "Behoben", text: d.message, tone: e.type === "alert.resolved" ? "green" : d.severity === "critical" ? "red" : "orange" };
    setToasts((x) => [...x.slice(-3), t]);
    setTimeout(() => setToasts((x) => x.filter((y) => y.id !== t.id)), 8000);
  }, ["alert.firing", "alert.resolved"]);
  return (
    <div className="fixed bottom-4 right-4 z-50 space-y-2" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} role="status" className={cls("flex w-80 gap-2.5 rounded-lg border border-line border-l-4 bg-panel p-3 shadow-[var(--shadow)]", t.tone === "red" ? "border-l-red" : t.tone === "green" ? "border-l-green" : "border-l-orange")}>
          <Icon name={t.tone === "green" ? "checkCircle" : t.tone === "red" ? "octagon" : "alert"} className={cls("mt-0.5 text-[15px]", t.tone === "red" ? "text-red-text" : t.tone === "green" ? "text-green-text" : "text-orange-text")} />
          <div className="min-w-0 flex-1">
            <div className="font-semibold">{t.title}</div>
            <div className="text-fg2">{t.text}</div>
          </div>
          <button type="button" aria-label="Schließen" onClick={() => setToasts((x) => x.filter((y) => y.id !== t.id))} className="h-5 cursor-pointer text-fg3 hover:text-fg"><Icon name="x" /></button>
        </div>
      ))}
    </div>
  );
}
