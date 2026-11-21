import { useState } from "react";
import type { ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Panel, Table, Window } from "../../../kit";
import { fetchTemplates, generatorKeys, saveTemplate } from "../../../data/generatorApi";
import type { GenBatch, GenTemplate } from "../../../data/generatorApi";
import { serverDetail } from "../../../../lib/errorText";
import { fmtDmy } from "../../../../lib/format";
import { PhoneOff } from "../../../kit/phone";

// Section 4: the saved templates, with ADD TEMPLATE. Mockup: docs/design/rail_mockup_2026-10-10.html
// at 05ac8de4, genPage, TEMPLATES. USE picks the template in the start section.

const SLOTS_HOVER = "The template list does not send its letters yet; the preview shows them";

/** How many theories the batches made from this template: each batch makes the template's theory count. */
export function theoriesMade(t: GenTemplate, batches: GenBatch[]): number {
  return batches.filter((b) => b.template_sha256 === t.sha256).length * t.theory_count;
}

export function AddTemplateWindow({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const [note, setNote] = useState<ReactNode>(null);
  const save = useMutation({
    mutationFn: () => saveTemplate(text),
    onSuccess: (saved) => {
      if (saved.created) {
        void qc.invalidateQueries({ queryKey: generatorKeys.templates });
        onClose();
      } else {
        setNote(<div className="gn-note">{`This text is already saved as ${saved.display_name}`}</div>);
      }
    },
    onError: (err) => setNote(<div className="gn-fail">{serverDetail(err)}</div>),
  });
  const press = () => {
    setNote(null);
    save.mutate();
  };
  return (
    <Window label="Add a template" size="l" onClose={onClose}>
      <div className="h2">ADD A TEMPLATE</div>
      <textarea
        className="fin gn-text"
        aria-label="Template text"
        rows={18}
        value={text}
        onChange={(e) => setText(e.target.value)}
      />
      {note}
      <div className="gn-win-actions">
        <button type="button" className="btn q" onClick={onClose}>CANCEL</button>
        <button type="button" className="btn" disabled={text.trim() === "" || save.isPending} onClick={press}>SAVE</button>
      </div>
    </Window>
  );
}

export function Templates({ batches, onUse }: { batches: GenBatch[]; onUse: (sha: string) => void }) {
  const [adding, setAdding] = useState(false);
  const q = useQuery({ queryKey: generatorKeys.templates, queryFn: fetchTemplates });
  const add = (
    <PhoneOff><button type="button" className="btn" onClick={() => setAdding(true)}>ADD TEMPLATE</button></PhoneOff>
  );
  const templates = q.data;
  return (
    <>
      <Panel
        title={<span className="gn-hd">TEMPLATES{templates && <span className="num">{templates.length}</span>}</span>}
        note={add}
      >
        {q.isError ? (
          <div className="gn-fail">{serverDetail(q.error)}</div>
        ) : templates == null ? null : templates.length === 0 ? (
          <div className="gn-empty">No template saved yet.</div>
        ) : (
          <div className="gn-tpl">
            <Table
              head={["Template", "Slots", "Theories made", "Saved", ""]}
              rows={templates.map((t) => [
                { v: t.display_name, cls: "gn-name" },
                <span key="s" title={SLOTS_HOVER}>-</span>,
                String(theoriesMade(t, batches)),
                fmtDmy(t.saved_at),
                <PhoneOff key="u"><button type="button" className="btn q gn-use" onClick={() => onUse(t.sha256)}>USE</button></PhoneOff>,
              ])}
            />
          </div>
        )}
      </Panel>
      {adding && <AddTemplateWindow onClose={() => setAdding(false)} />}
    </>
  );
}
