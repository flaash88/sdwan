import { useEffect, useState } from "react";
import { Button, Checkbox, ErrorBox, IconButton, Input, Modal, Notice, Segment, Select, Textarea, ToggleField, useAction } from "../ui";
import { api } from "../../lib/api";

export interface FormField { key: string; label_de: string; label_en: string; type: "text" | "email" | "tel" | "checkbox"; required: boolean; max_len: number }
export interface Portal {
  id: string; name: string; description: string | null; builtin: boolean; scope: "global" | "tenant"; login_type: "voucher" | "click" | "form";
  design: { primary?: string; background?: string; text?: string; logo?: string | null }; texts: Record<"de" | "en", Record<string, string>>;
  terms_required: boolean; form_fields: FormField[]; custom_files: string[]; version: number;
}
const TEXT_KEYS: [string, string, boolean][] = [["title", "Überschrift", false], ["welcome", "Begrüßung", true], ["button", "Button", false], ["code_label", "Beschriftung Code-Feld (Voucher)", false],
  ["terms_label", "Text der Pflicht-Checkbox", false], ["terms", "Nutzungsbedingungen", true], ["success", "Text nach der Anmeldung", true]];
export const LOGIN_TYPES: Record<string, string> = { voucher: "Voucher-Code", click: "Klick (Nutzungsbedingungen)", form: "Formular" };

/** Portal-Designer: Logo, Farben, Texte DE/EN, Nutzungsbedingungen mit Pflicht-Checkbox, Formularfelder, Live-Vorschau, eigene Login-Seiten. */
export default function PortalDesigner({ portal, onClose, onSaved }: { portal: Portal; onClose: () => void; onSaved: () => Promise<void> }) {
  const [p, setP] = useState<Portal>(portal);
  const [lang, setLang] = useState<"de" | "en">("de");
  const [page, setPage] = useState("login.html");
  const [html, setHtml] = useState("");
  const { busy, error, run } = useAction();
  const body = { name: p.name, description: p.description, login_type: p.login_type, design: p.design, texts: p.texts, terms_required: p.terms_required, form_fields: p.form_fields };
  const key = JSON.stringify({ ...body, page });
  useEffect(() => {
    const t = setTimeout(() => { void api.post<string>("/hotspot/preview", { ...body, page }).then(setHtml).catch(() => undefined); }, 350);
    return () => clearTimeout(t);
  }, [key]); // eslint-disable-line react-hooks/exhaustive-deps
  const setText = (k: string, v: string) => setP({ ...p, texts: { ...p.texts, [lang]: { ...(p.texts[lang] ?? {}), [k]: v } } });
  const setField = (i: number, f: Partial<FormField>) => setP({ ...p, form_fields: p.form_fields.map((x, j) => (j === i ? { ...x, ...f } : x)) });
  const logo = (file: File) => {
    if (file.size > 150_000) { alert("Logo max. 150 KB"); return; }
    const r = new FileReader();
    r.onload = () => setP({ ...p, design: { ...p.design, logo: String(r.result) } });
    r.readAsDataURL(file);
  };
  const upload = (files: FileList) => void run(async () => {
    const out: Record<string, string> = {};
    for (const f of Array.from(files)) out[f.name] = await f.text();
    await api.put(`/hotspot/portals/${p.id}/files`, out);
    await onSaved();
    setP({ ...p, custom_files: Object.keys(out).sort() });
  });
  return (
    <Modal open size="xl" onClose={onClose} title={`Portal „${portal.name}“`}
      footer={<><Button variant="secondary" onClick={onClose}>Abbrechen</Button><Button disabled={busy || !p.name} onClick={() => void run(async () => { await api.put(`/hotspot/portals/${p.id}`, body); await onSaved(); onClose(); })}>Speichern</Button></>}>
      <ErrorBox error={error} />
      <div className="grid gap-5 lg:grid-cols-2">
        <div className="flex flex-col gap-3">
          <Input label="Name" value={p.name} onChange={(e) => setP({ ...p, name: e.target.value })} />
          <Select label="Anmeldeart" value={p.login_type} onChange={(e) => setP({ ...p, login_type: e.target.value as Portal["login_type"] })}>
            {Object.entries(LOGIN_TYPES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </Select>
          <div className="flex flex-wrap items-end gap-4">
            {(["primary", "background", "text"] as const).map((k) => (
              <label key={k} className="flex flex-col gap-1 text-sm font-medium">{{ primary: "Akzent", background: "Hintergrund", text: "Schrift" }[k]}
                <input type="color" className="h-9 w-16 cursor-pointer rounded border border-line" value={p.design[k] ?? "#000000"} onChange={(e) => setP({ ...p, design: { ...p.design, [k]: e.target.value } })} />
              </label>
            ))}
            <label className="flex flex-col gap-1 text-sm font-medium">Logo (PNG/JPEG/WebP)
              <input type="file" accept="image/png,image/jpeg,image/webp" className="text-xs" onChange={(e) => e.target.files?.[0] && logo(e.target.files[0])} />
            </label>
            {p.design.logo && <Button size="sm" variant="ghost" onClick={() => setP({ ...p, design: { ...p.design, logo: null } })}>Logo entfernen</Button>}
          </div>
          <Segment size="sm" label="Sprache" value={lang} onChange={setLang} options={[{ value: "de", label: "Deutsch" }, { value: "en", label: "English" }]} />
          {TEXT_KEYS.filter(([k]) => k !== "code_label" || p.login_type === "voucher").map(([k, l, long]) => long
            ? <Textarea key={k} label={l} rows={k === "terms" ? 4 : 2} value={p.texts[lang]?.[k] ?? ""} onChange={(e) => setText(k, e.target.value)} />
            : <Input key={k} label={l} value={p.texts[lang]?.[k] ?? ""} onChange={(e) => setText(k, e.target.value)} />)}
          <ToggleField label="Pflicht-Checkbox für die Nutzungsbedingungen" checked={p.terms_required} onChange={(v) => setP({ ...p, terms_required: v })} />
          {p.login_type === "form" && (
            <div className="flex flex-col gap-2 rounded-md border border-line p-3">
              <div className="text-sm font-medium">Formularfelder <span className="font-normal text-fg3">– nur diese Felder werden gespeichert</span></div>
              {p.form_fields.map((f, i) => (
                <div key={i} className="grid grid-cols-[1fr_1fr_1fr_auto] items-end gap-2">
                  <Input label="Schlüssel" value={f.key} onChange={(e) => setField(i, { key: e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, "") })} />
                  <Input label="Bezeichnung DE" value={f.label_de} onChange={(e) => setField(i, { label_de: e.target.value })} />
                  <Input label="Bezeichnung EN" value={f.label_en} onChange={(e) => setField(i, { label_en: e.target.value })} />
                  <IconButton icon="trash" label="Feld entfernen" onClick={() => setP({ ...p, form_fields: p.form_fields.filter((_, j) => j !== i) })} />
                  <Select aria-label="Typ" value={f.type} onChange={(e) => setField(i, { type: e.target.value as FormField["type"] })}>
                    <option value="text">Text</option><option value="email">E-Mail</option><option value="tel">Telefon</option><option value="checkbox">Checkbox</option>
                  </Select>
                  <Input aria-label="max. Länge" type="number" min={1} max={500} value={f.max_len} onChange={(e) => setField(i, { max_len: Number(e.target.value) })} />
                  <Checkbox label="Pflichtfeld" checked={f.required} onChange={(v) => setField(i, { required: v })} />
                </div>
              ))}
              {p.form_fields.length < 10 && <Button size="sm" variant="ghost" icon="plus" onClick={() => setP({ ...p, form_fields: [...p.form_fields, { key: `feld${p.form_fields.length + 1}`, label_de: "", label_en: "", type: "text", required: false, max_len: 100 }] })}>Feld</Button>}
            </div>
          )}
          <Notice tone="blue" icon="info" title="Verantwortung des Betreibers">Nutzungsbedingungen und Datenschutzhinweis verantwortet der Betreiber des Hotspots. Nur die Formularfelder werden gespeichert und nach der Aufbewahrungsfrist des Mandanten automatisch gelöscht.</Notice>
          <div className="rounded-md border border-line p-3 text-sm">
            <div className="font-medium">Eigene Login-Seiten</div>
            <p className="text-fg3">Ersetzen die erzeugten Dateien gleichen Namens (mind. login.html; RouterOS-Variablen wie <span className="font-mono">$(link-login-only)</span>). Nach dem Hochladen gilt der Stand sofort als Version des Portals.</p>
            {p.custom_files.length > 0 && <p className="mt-1 font-mono text-xs">{p.custom_files.join(", ")}</p>}
            <div className="mt-2 flex items-center gap-2">
              <input type="file" multiple accept=".html,.css,.js,.txt" className="text-xs" onChange={(e) => e.target.files?.length && upload(e.target.files)} />
              {p.custom_files.length > 0 && <Button size="sm" variant="ghost" onClick={() => void run(async () => { await api.put(`/hotspot/portals/${p.id}/files`, {}); await onSaved(); setP({ ...p, custom_files: [] }); })}>Zurück zu erzeugten Seiten</Button>}
            </div>
          </div>
        </div>
        <div className="flex flex-col gap-2">
          <Segment size="sm" label="Vorschau" value={page} onChange={setPage} options={[{ value: "login.html", label: "Anmeldung" }, { value: "status.html", label: "Status" }, { value: "logout.html", label: "Abgemeldet" }]} />
          <iframe title="Vorschau" sandbox="allow-scripts" srcDoc={html} className="h-[640px] w-full rounded-md border border-line bg-white" />
          <p className="text-xs text-fg3">Vorschau mit Beispielwerten. {p.custom_files.length > 0 && "Eigene Seiten werden erst nach dem Speichern berücksichtigt."}</p>
        </div>
      </div>
    </Modal>
  );
}
