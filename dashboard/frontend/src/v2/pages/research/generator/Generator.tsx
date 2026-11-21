import { useState } from "react";
import type { ReactNode } from "react";
import { useQueries, useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Block, PageHeader, Say, State, Window } from "../../../kit";
import type { PageProps } from "../../../shell/sections";
import {
  fetchBatches, fetchTemplates, fetchTree, fetchTrialsPerDay, fetchWorker, generatorKeys,
} from "../../../data/generatorApi";
import type { GenTree } from "../../../data/generatorApi";
import { serverDetail } from "../../../../lib/errorText";
import { Searches } from "./Searches";
import { StartTheory } from "./StartTheory";
import { AddTemplateWindow, Templates } from "./Templates";
import { TrialDays, nearestOf } from "./Closeness";
import { DaysNote, NeedsYou, SearchTiles, TheoryResults, YearsUsed, dayIso } from "./GeneratorBlocks";
import type { Stopper } from "./GeneratorBlocks";
import "./generator.css";
import { PhoneOff } from "../../../kit/phone";

// The Generator page, after the look trial's lab-p-generator.js (2026-11-14): the finding, what stops it, the searches as tiles,
// every theory as a table of written figures, settings tried per day, and the years the searches use. Starting a theory, the
// templates and a search's controls open in windows, so nothing the page could do before is gone.

const num = (n: number) => n.toLocaleString("en-GB");

export default function Generator({ app, section }: PageProps) {
  const navigate = useNavigate();
  const [starting, setStarting] = useState(false);
  const [sha, setSha] = useState("");
  const [adding, setAdding] = useState(false);
  const [searchId, setSearchId] = useState<number | null>(null);
  const templates = useQuery({ queryKey: generatorKeys.templates, queryFn: fetchTemplates });
  const batches = useQuery({ queryKey: generatorKeys.batches, queryFn: fetchBatches });
  const trials = useQuery({ queryKey: generatorKeys.trials, queryFn: fetchTrialsPerDay });
  const worker = useQuery({ queryKey: generatorKeys.worker, queryFn: fetchWorker, refetchInterval: 60_000 });
  const batchList = batches.data ?? [];
  const trees = useQueries({
    queries: batchList.map((b) => ({ queryKey: generatorKeys.tree(b.id), queryFn: () => fetchTree(b.id) })),
  });

  const treesLoading = batches.isPending || trees.some((t) => t.isPending);
  const treeFailed = batches.isError || trees.some((t) => t.isError);
  const loaded: GenTree[] = trees.flatMap((t) => (t.data ? [t.data] : []));

  const theories = loaded.flatMap((t) => t.theories);
  const kept = theories.reduce((a, th) => a + th.saved.length, 0);
  const found = theories.filter((th) => th.saved.length > 0).length;
  const days = trials.data?.days ?? [];
  const lastIdx = days.map((d) => d.trials > 0).lastIndexOf(true);
  const last = lastIdx >= 0 ? days[lastIdx] : null;
  const quietSince = last != null && lastIdx < days.length - 1;
  const online = worker.data?.status === "online";
  const offline = worker.data?.status === "offline";
  const stopped = theories.filter((th) => { const n = nearestOf(th); return n != null && n !== "odd" && n.answer === "no"; });
  // "After its first round" only when every stopped search ran one round; otherwise the sentence does not count rounds.
  const afterFirst = stopped.every((th) => (th.search?.rounds?.rounds?.length ?? 0) === 1);

  // What waits for Tamer: a template no search was made from.
  const used = new Set(batchList.map((b) => b.template_sha256));
  const waiting = (templates.data ?? []).filter((t) => !used.has(t.sha256));

  const stoppers: Stopper[] = [];
  if (offline) {
    const withWork = days.filter((d) => d.trials > 0).length;
    stoppers.push({
      tone: "bad", word: "Broken", title: "The worker is offline",
      detail: `The worker is the program that runs the searches. ${last ? `Nothing has run since ${dayIso(last.day)}, ${days.length - 1 - lastIdx} ${days.length - 1 - lastIdx === 1 ? "day" : "days"} ago.` : "Nothing has run yet."}`,
      share: [["run", withWork], ["dim", days.length - withWork]], caption: `${withWork} of ${days.length} days had any work`,
      button: "Open Operations", act: () => navigate("/app/research/operations"),
    });
  }
  if (theories.length > 0 && found === 0) {
    const missed = theories.flatMap((th) => { const n = nearestOf(th); return n != null && n !== "odd" && !Number.isNaN(n.sharpe) ? [n.failed.length] : []; });
    const lo = missed.length ? Math.min(...missed) : null;
    const hi = missed.length ? Math.max(...missed) : null;
    stoppers.push({
      tone: "bad", word: "Stopped", title: "No theory found a strategy to keep",
      detail: lo === null ? "No theory has a best attempt to read yet." : `Each theory's best attempt missed ${lo === hi ? lo : `${lo} or ${hi}`} of the 5 targets, so every search stopped${afterFirst ? " after its first round" : ""}.`,
      share: [["bad", theories.length - found], ["good", found]], caption: `${found} of ${theories.length} theories found one`,
      button: "See how close", act: () => document.getElementById("close")?.scrollIntoView?.({ block: "start" }),
    });
  }
  if (waiting.length > 0) {
    stoppers.push({
      tone: "warn", word: "Waits for you", title: `${waiting.length} ${waiting.length === 1 ? "template has" : "templates have"} not been started`,
      detail: `${waiting.map((t) => t.display_name).join(", ")}. Approve one and the generator starts searching with it.`,
      share: [["warn", waiting.length], ["dim", (templates.data ?? []).length - waiting.length]], caption: `${waiting.length} of ${(templates.data ?? []).length} templates wait`,
      button: "Start a theory", act: () => { setSha(waiting[0].sha256); setStarting(true); },
    });
  }

  const header = (
    <div className="gn-right">
        {worker.isError ? <span className="gn-dash" title={serverDetail(worker.error)}>-</span>
          : worker.data ? <State tone={online ? "good" : "bad"} hover="The laptop that runs the searches">{`Worker ${online ? "online" : "offline"}`}</State> : null}
        <PhoneOff><button type="button" className="btn" onClick={() => setStarting(true)}>Start a theory</button></PhoneOff>
      </div>
  );

  let say: ReactNode = null;
  if (!treesLoading && !treeFailed && batchList.length > 0) {
    say = (
      <Say
        lead={`${batchList.length} search${batchList.length === 1 ? "" : "es"}, ${theories.length} theor${theories.length === 1 ? "y" : "ies"}, ${kept ? `${kept} kept` : "nothing kept"}.`}
        more={<>
          {"A theory is one trading idea with a range of settings to try. "}
          {stopped.length > 0 && <><em className="bad">{`${stopped.length === theories.length ? "Every theory" : `${stopped.length} ${stopped.length === 1 ? "theory" : "theories"}`} stopped${afterFirst ? " after its first round" : ""}`}</em>{": its best attempt missed at least one target. "}</>}
          {last && `${num(last.trials)} settings were tried on ${dayIso(last.day)}${quietSince ? " and none since" : ""}.`}
        </>}
      />
    );
  } else if (!treesLoading && !treeFailed && batchList.length === 0) {
    say = <Say lead="No searches yet." more="Start a theory and the generator tries its settings on the early years." />;
  }

  return (
    <div className="v2-page gn">
      <PageHeader crumb={app.name} title={section.label} right={header} />
      {say}
      {treeFailed && (
        <p className="rd" role="status">
          {`A search could not be read, so the finding and the blocks below leave it out: ${serverDetail(batches.error ?? trees.find((t) => t.isError)?.error)}`}
        </p>
      )}
      <div className="gn-cq">
      <NeedsYou stoppers={stoppers} loading={treesLoading && !treeFailed} />
      <SearchTiles trees={loaded} onOpen={setSearchId} onAddTemplate={() => setAdding(true)} />
      {loaded.length > 0 && <TheoryResults trees={loaded} />}
      <Block className="sx-fams" title="Settings tried per day">
        {trials.isError ? <p className="gn-fail">{serverDetail(trials.error)}</p>
          : !trials.data ? <p className="empty">Reading the trial counts.</p>
          : trials.data.days.length === 0 ? <p className="empty" title="No search has run yet">-</p>
          : <><TrialDays days={trials.data.days} /><DaysNote days={trials.data.days} /></>}
      </Block>
      {batchList.length > 0 && <YearsUsed batches={batchList} />}
      </div>
      {starting && (
        <Window label="Start a theory" size="xl" className="sx-win" onClose={() => setStarting(false)}>
          <div className="sx-wh"><h2>Start a theory</h2><button type="button" className="btn q" onClick={() => setStarting(false)}>CLOSE</button></div>
          <StartTheory sha={sha} onPick={setSha} />
          <Templates batches={batchList} onUse={setSha} />
        </Window>
      )}
      {searchId !== null && (
        <Window label="Search controls" size="xl" className="sx-win" onClose={() => setSearchId(null)}>
          <div className="sx-wh"><h2>{loaded.find((t) => t.batch.id === searchId)?.template.display_name ?? "Search"}</h2><button type="button" className="btn q" onClick={() => setSearchId(null)}>CLOSE</button></div>
          <Searches initialBatch={searchId} />
        </Window>
      )}
      {adding && <AddTemplateWindow onClose={() => setAdding(false)} />}
    </div>
  );
}
