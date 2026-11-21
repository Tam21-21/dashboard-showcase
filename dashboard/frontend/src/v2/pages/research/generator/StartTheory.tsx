import { useState } from "react";
import type { ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { DatePick, Facts, Panel, Segmented, ShareBar, StateWord, Table, Window, YearsBand } from "../../../kit";
import type { Stage } from "../../../kit";
import { bandAxis } from "../../../kit/YearsBand";
import {
  approveTemplate, fetchStockGroups, fetchTemplates, generatorKeys, groupsKey, previewTemplate,
} from "../../../data/generatorApi";
import type { GenPreview, PreviewBody } from "../../../data/generatorApi";
import { serverDetail } from "../../../../lib/errorText";
import { AddTemplateWindow } from "./Templates";
import { PhoneOff } from "../../../kit/phone";

// Section 1, START A THEORY: pick a template and a stock group, PREVIEW what an approve would make, then
// APPROVE AND RUN. Mockup: docs/design/rail_mockup_2026-10-10.html at 05ac8de4, genNew.

const BLUE = "#4d9bff";
const GREY = "#3a424d";
const DAY = 86400;
const STAGES: Stage[] = ["screen", "optimise", "walkforward", "later"];

const epoch = (iso: string) => Date.parse(`${iso}T00:00:00Z`) / 1000;
const orNull = (iso: string) => (iso === "" ? null : iso);

/** The seed as the server wants it: digits become a number, empty is null, anything else goes as typed. */
function seedValue(text: string): number | string | null {
  const t = text.trim();
  if (t === "") return null;
  return /^\d+$/.test(t) ? Number(t) : t;
}

/** Every combination of the first round's values, summed over the theories that have search settings. */
export function combinations(preview: GenPreview): number {
  return preview.theories.reduce((sum, t) => {
    if (t.round_one == null) return sum;
    return sum + Object.values(t.round_one).reduce((product, r) => product * r.count, 1);
  }, 0);
}

type Shown = { key: string; preview: GenPreview } | { key: string; error: string };

function Dash({ hover }: { hover: string }) {
  return <span title={hover}>-</span>;
}

function LettersTable({ preview }: { preview: GenPreview }) {
  const rows = preview.theories.flatMap((t) =>
    t.letters.map((letter, i) => {
      const range = t.round_one?.[letter];
      let values: ReactNode;
      if (t.round_one == null) values = <Dash hover="This theory has no search settings, so its search will be refused" />;
      else if (range == null) values = <Dash hover="This letter is not searched in the first round" />;
      else values = String(range.count);
      return [
        { v: i === 0 ? t.display_name : "", cls: "gn-name" },
        { v: letter, cls: "gn-l" },
        { v: <Dash hover="The preview does not say what a letter is yet" />, cls: "gn-l" },
        values,
        range == null ? "-" : `${range.from} to ${range.to}, step ${range.step}`,
      ];
    }),
  );
  return (
    <div className="gn-letters">
      <Table head={["Theory", "Letter", "What it is", "Values", "Range"]} rows={rows} />
    </div>
  );
}

function ApproveWindow({
  sha, preview, body, groupLabel, dataStart, onClose,
}: {
  sha: string; preview: GenPreview; body: PreviewBody; groupLabel: string; dataStart: string; onClose: () => void;
}) {
  const qc = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<{ started: number } | { refused: string } | null>(null);

  async function go() {
    if (busy || done != null) return;
    setBusy(true);
    try {
      const made = await approveTemplate(sha, {
        ...body, group_label: groupLabel, data_start: dataStart, calendar_id: `gen-${groupLabel.toLowerCase()}-${Date.now()}`,
      });
      setDone({ started: made.run_ids.length });
    } catch (err) {
      setDone({ refused: serverDetail(err) });
    } finally {
      setBusy(false);
      // A refused search can leave its batch saved, so the page reads the batches again after either answer.
      void qc.invalidateQueries({ queryKey: generatorKeys.all });
    }
  }

  return (
    <Window label="Approve and run" size="s" onClose={onClose}>
      <div className="h2">APPROVE AND RUN</div>
      <div className="gn-lines">
        <div className="gn-hi">{preview.template.display_name}</div>
        <div>{`${preview.theories.length} theories`}</div>
        <div>{`${preview.stocks.searched.length} searched, ${preview.stocks.kept_back.length} kept back`}</div>
        <div>{groupLabel}</div>
      </div>
      {done != null && "started" in done && (
        <div className="gn-result">
          <StateWord word="STARTED" tone="g" />
          <span>{done.started === 1 ? "1 search started" : `${done.started} searches started`}</span>
        </div>
      )}
      {done != null && "refused" in done && <div className="gn-fail">{done.refused}</div>}
      <div className="gn-win-actions">
        {done == null ? (
          <>
            <button type="button" className="btn q" onClick={onClose}>CANCEL</button>
            <button type="button" className="btn" disabled={busy} onClick={() => void go()}>APPROVE AND RUN</button>
          </>
        ) : (
          <button type="button" className="btn q" onClick={onClose}>CLOSE</button>
        )}
      </div>
    </Window>
  );
}

export function StartTheory({ sha, onPick }: { sha: string; onPick: (sha: string) => void }) {
  const templates = useQuery({ queryKey: generatorKeys.templates, queryFn: fetchTemplates });
  const groups = useQuery({ queryKey: groupsKey, queryFn: fetchStockGroups });
  const [adding, setAdding] = useState(false);
  const [group, setGroup] = useState("");
  const named = (groups.data ?? []).filter((g) => g.display_name !== null);
  const unnamed = (groups.data ?? []).length - named.length;
  const [seed, setSeed] = useState("");
  const [optFrom, setOptFrom] = useState("");
  const [optTo, setOptTo] = useState("");
  const [laterFrom, setLaterFrom] = useState("");
  const [laterTo, setLaterTo] = useState("");
  const [busy, setBusy] = useState(false);
  const [shown, setShown] = useState<Shown | null>(null);
  const [groupName, setGroupName] = useState("");
  const [dataFrom, setDataFrom] = useState("");
  const [approving, setApproving] = useState(false);
  const [open, setOpen] = useState<string | null>(null);

  const body: PreviewBody | null = group === "" ? null : {
    universe_id: Number(group),
    seed: seedValue(seed),
    optimise_window: [orNull(optFrom), orNull(optTo)],
    later_window: [orNull(laterFrom), orNull(laterTo)],
  };
  // Any change to what a preview was made from takes the preview away.
  const key = JSON.stringify([sha, body]);
  const current = shown != null && shown.key === key ? shown : null;
  const canPreview = sha !== "" && body != null && !busy;

  async function press() {
    if (!canPreview || body == null) return;
    setBusy(true);
    try {
      setShown({ key, preview: await previewTemplate(sha, body) });
    } catch (err) {
      setShown({ key, error: serverDetail(err) });
    } finally {
      setBusy(false);
    }
  }

  const options = (templates.data ?? []).map((t) => ({ label: t.display_name, value: t.sha256 }));
  const date = (label: string, value: string, set: (v: string) => void) => (
    <div className="gn-date">
      <span className="kl">{label.toUpperCase()}</span>
      <DatePick value={value} onChange={set} ariaLabel={label} open={open === label} onOpenChange={(o) => setOpen(o ? label : null)} />
    </div>
  );

  const preview = current != null && "preview" in current ? current.preview : null;
  let previewCard;
  if (preview == null) {
    previewCard = (
      <Panel title="PREVIEW">
        {current != null && "error" in current
          ? <div className="gn-fail">{current.error}</div>
          : <div className="gn-dash" title="Press PREVIEW to see what an approve would make">-</div>}
      </Panel>
    );
  } else {
    const y = preview.years;
    const stretches = STAGES.map((stage) => ({ stage, from: epoch(y[stage][0]), to: epoch(y[stage][1]) }));
    const cut = epoch(y.walkforward[1]) + DAY;
    const axis = bandAxis(stretches, cut);
    previewCard = (
      <Panel title={<span className="gn-hd">PREVIEW<span className="num">{`${combinations(preview).toLocaleString("en-US")} combinations`}</span></span>}>
        <Facts items={[
          ["THEORIES", String(preview.theories.length)],
          ["STOCKS SEARCHED", String(preview.stocks.searched.length), undefined, preview.stocks.searched.join(", ")],
          ["KEPT BACK", String(preview.stocks.kept_back.length), undefined, preview.stocks.kept_back.join(", ")],
          ["TIME", "-", undefined, "The server does not estimate run time yet"],
        ]} />
        <div className="gn-block">
          <span className="kl">STOCKS</span>
          <ShareBar parts={[
            [preview.stocks.searched.length, BLUE, "Searched"],
            [preview.stocks.kept_back.length, GREY, "Kept back for later"],
          ]} />
        </div>
        <div className="gn-block">
          <span className="kl">THE FOUR STRETCHES</span>
          <YearsBand stretches={stretches} cut={cut} from={axis.from} to={axis.to} />
        </div>
        <div id="gs1-approve" className="gn-bar">
          <input className="fin gn-group" aria-label="Group name" placeholder="GROUP_D" value={groupName} onChange={(e) => setGroupName(e.target.value)} />
          {date("Data from", dataFrom, setDataFrom)}
          <span className="gn-grow" />
          <PhoneOff>
            <button type="button" className="btn" disabled={groupName.trim() === "" || dataFrom === ""} onClick={() => setApproving(true)}>
              APPROVE AND RUN
            </button>
          </PhoneOff>
        </div>
      </Panel>
    );
  }

  return (
    <div className="gn-start">
      <Panel title="NEW THEORY">
        <div className="gn-name-row">
          <span className="kl">NAME</span>
          <Dash hover="A theory takes its template's name; the server cannot name a new theory yet (G1)" />
        </div>
        <div className="gn-pills">
          {templates.isError
            ? <div className="gn-fail">{serverDetail(templates.error)}</div>
            : <Segmented options={options} value={sha} onChange={onPick} />}
          <PhoneOff><button type="button" className="btn q" onClick={() => setAdding(true)}>ADD TEMPLATE</button></PhoneOff>
        </div>
        <div className="gn-fields">
          <select className="fin" aria-label="Stock group" value={group} onChange={(e) => setGroup(e.target.value)}>
            <option value="">Pick a stock group</option>
            {named.map((g) => (
              <option key={g.id} value={String(g.id)}>
                {`${g.display_name}, ${g.member_count ?? "-"} stocks${g.needs_name ? ", NEEDS A NAME" : ""}`}
              </option>
            ))}
            {unnamed > 0 && <option disabled value="">{`Not listed, no name yet: ${unnamed}. Name them on the Universes tab.`}</option>}
          </select>
          <input className="fin" aria-label="Seed" placeholder="Seed" value={seed} onChange={(e) => setSeed(e.target.value)} />
        </div>
        {groups.isError && <div className="gn-fail">{serverDetail(groups.error)}</div>}
        <div className="gn-dates">
          {date("Optimise from", optFrom, setOptFrom)}
          {date("Optimise to", optTo, setOptTo)}
          {date("Later from", laterFrom, setLaterFrom)}
          {date("Later to", laterTo, setLaterTo)}
        </div>
        {preview == null
          ? <div className="gn-dash" title="The letters come with the preview">-</div>
          : <LettersTable preview={preview} />}
        <div className="gn-end">
          <PhoneOff><button type="button" className="btn" data-preview-go disabled={!canPreview} onClick={() => void press()}>PREVIEW</button></PhoneOff>
        </div>
      </Panel>
      {previewCard}
      {adding && <AddTemplateWindow onClose={() => setAdding(false)} />}
      {approving && preview != null && body != null && (
        <ApproveWindow
          sha={sha} preview={preview} body={body} groupLabel={groupName.trim()} dataStart={dataFrom}
          onClose={() => setApproving(false)}
        />
      )}
    </div>
  );
}
