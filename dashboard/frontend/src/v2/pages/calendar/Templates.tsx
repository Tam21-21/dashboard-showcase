import type { DragEvent, FormEvent } from "react";
import { KIT_ACCENTS, toAccent } from "./accent";
import { serverDetail } from "../../../lib/errorText";
import { State, usePageMemory } from "../../kit";
import type { EventTemplate } from "../../../types";
import { TEMPLATE_LENGTH, useCreateTemplate, useDeleteTemplate, useTemplates } from "./data";
import type { Drag } from "./data";

// The templates list: drag a row onto the calendar to make a one hour event, add one, delete one.

export default function Templates({
  setDrag, onError,
}: {
  setDrag: (d: Drag | null) => void;
  onError: (text: string | null) => void;
}) {
  const q = useTemplates();
  const create = useCreateTemplate();
  const remove = useDeleteTemplate();
  const [tpl, setTpl, clearTpl] = usePageMemory<{ adding: boolean; title: string; color: string }>(
    "calendar/template", { adding: false, title: "", color: KIT_ACCENTS[0] },
  );
  const { adding, title, color } = tpl;
  const [open, setOpen] = usePageMemory<boolean>("calendar/templates-open", false);
  const setAdding = (v: boolean) => setTpl((t) => ({ ...t, adding: v }));
  const setTitle = (v: string) => setTpl((t) => ({ ...t, title: v }));
  const setColor = (v: string) => setTpl((t) => ({ ...t, color: v }));
  const fail = (err: unknown) => onError(serverDetail(err));

  const start = (t: EventTemplate, de: DragEvent<HTMLDivElement>) => {
    de.dataTransfer.setData("template-id", String(t.id));
    de.dataTransfer.effectAllowed = "copy";
    setDrag({ kind: "template", id: t.id, title: t.title, color: t.color, length: TEMPLATE_LENGTH });
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!title.trim()) return onError("Give the template a title.");
    onError(null);
    create.mutate(
      { title: title.trim(), color },
      { onSuccess: clearTpl, onError: fail },
    );
  };

  const saved = q.data?.length ?? 0;
  return (
    <div className="ctpl">
      <div className="cal-foot">
        <div>
          <button type="button" className="tfold" aria-expanded={open} onClick={() => setOpen(!open)}>
            <b title="Drag one onto the calendar">Templates</b>
          </button>
          <State tone={saved ? "run" : "dim"}>{saved ? `${saved} saved` : "None saved"}</State>
          <span className="xn">A template is an event you add often, such as a long run. Saved ones list here and drag onto a day.</span>
        </div>
        <button type="button" className="btn tadd" onClick={() => { setOpen(true); setAdding(true); }}>Add template</button>
      </div>
      {open && <div className="tpbody">
      {q.isError && <div className="ferr" role="alert">{serverDetail(q.error)}</div>}
      {(q.data ?? []).map((t) => (
        <div
          key={t.id}
          className="tp"
          draggable
          onDragStart={(de) => start(t, de)}
          onDragEnd={() => setDrag(null)}
        >
          <span className="grip">⋮⋮</span>
          <i style={{ background: toAccent(t.color) }} />
          <span className="tt">{t.title}</span>
          <button
            type="button"
            className="tdel"
            aria-label={`Delete ${t.title}`}
            onClick={() => { onError(null); remove.mutate(t.id, { onError: fail }); }}
          >
            Delete
          </button>
        </div>
      ))}
      {adding ? (
        <form className="tform" onSubmit={submit}>
          <input aria-label="Template title" value={title} onChange={(e) => setTitle(e.target.value)} autoFocus />
          <div className="tcol">
            {KIT_ACCENTS.map((c) => (
              <button
                key={c}
                type="button"
                aria-label={`Colour ${c}`}
                aria-pressed={c === color}
                className={c === color ? "on" : ""}
                style={{ background: c }}
                onClick={() => setColor(c)}
              />
            ))}
          </div>
          <div className="mact">
            <button type="button" className="btn q" onClick={() => setAdding(false)}>Cancel</button>
            <button type="submit" className="btn" disabled={create.isPending}>Save</button>
          </div>
        </form>
      ) : null}
      </div>}
    </div>
  );
}
