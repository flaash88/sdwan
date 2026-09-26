import { useState } from "react";
import { useDevices, useSites } from "../lib/fleet";
import { Button, Checkbox, ErrorBox, Input, Modal, Segment, useAction } from "./ui";

export interface Targets { device_ids: string[]; site_ids: string[]; tags: string[] }

/** Ziele wählen: einzelne Geräte, alle Geräte von Standorten oder mit Tags. */
export default function TargetsModal({ title, onClose, onSubmit, submitLabel = "Zuweisen" }: {
  title: string; onClose: () => void; onSubmit: (t: Targets) => Promise<void>; submitLabel?: string;
}) {
  const devices = useDevices();
  const sites = useSites();
  const [mode, setMode] = useState<"devices" | "sites" | "tags">("devices");
  const [sel, setSel] = useState<string[]>([]);
  const [tags, setTags] = useState("");
  const { busy, error, run } = useAction();
  const toggle = (id: string) => setSel(sel.includes(id) ? sel.filter((x) => x !== id) : [...sel, id]);
  const body = (): Targets => ({ device_ids: mode === "devices" ? sel : [], site_ids: mode === "sites" ? sel : [], tags: mode === "tags" ? tags.split(",").map((t) => t.trim()).filter(Boolean) : [] });
  return (
    <Modal open onClose={onClose} title={title}
      footer={<><Button variant="secondary" onClick={onClose}>Abbrechen</Button><Button disabled={busy} onClick={() => void run(async () => { await onSubmit(body()); onClose(); })}>{submitLabel}</Button></>}>
      <ErrorBox error={error} />
      <Segment label="Ziel" value={mode} onChange={(m) => { setMode(m); setSel([]); }} options={[{ value: "devices", label: "Geräte" }, { value: "sites", label: "Standorte" }, { value: "tags", label: "Tags" }]} />
      <div className="mt-3 max-h-72 overflow-y-auto">
        {mode === "devices" && (devices.data ?? []).filter((d) => d.pairing_status === "paired").map((d) => <Checkbox key={d.id} label={d.name} checked={sel.includes(d.id)} onChange={() => toggle(d.id)} />)}
        {mode === "sites" && (sites.data ?? []).map((s) => <Checkbox key={s.id} label={s.name} checked={sel.includes(s.id)} onChange={() => toggle(s.id)} />)}
        {mode === "tags" && <Input label="Tags (kommagetrennt)" value={tags} onChange={(e) => setTags(e.target.value)} />}
      </div>
    </Modal>
  );
}
