import { useState } from "react";
import { useQueries, useQuery } from "@tanstack/react-query";
import { PageHeader, PhoneOff, Say } from "../../kit";
import type { PageProps } from "../../shell/sections";
import { chainsKeys, fetchChains, fetchStrategyOptions, fetchSweepParams } from "../../data/chainsApi";
import { fetchReserveSupply, strategiesKeys } from "../../data/strategiesApi";
import { fetchBatches, generatorKeys } from "../../data/generatorApi";
import { serverDetail } from "../../../lib/errorText";
import { dateToEpoch } from "../../../lib/format";
import { ChainSection } from "./chain/ChainSection";
import { ChainSteps, ChainYears } from "./chain/ChainIntro";

// The Chain page, round six (Tamer 2026-11-14, the look trial's lab-p-chain.js): the finding, what a chain does, the years
// it may use, then the Chain section (chains by group, the picked chain in full; mockup docs/design/rail_mockup_2026-10-10.html
// at 05ac8de4, research/CHAIN). The header carries the colour key of the stretches the picked chain's years band uses.
export default function Chains({ app, section }: PageProps) {
  const [startSignal, setStartSignal] = useState(0);
  const listQ = useQuery({ queryKey: chainsKeys.list, queryFn: fetchChains });
  const supplyQ = useQuery({ queryKey: strategiesKeys.supply, queryFn: () => fetchReserveSupply() });
  const batchesQ = useQuery({ queryKey: generatorKeys.batches, queryFn: fetchBatches });
  const optionsQ = useQuery({ queryKey: chainsKeys.strategies, queryFn: fetchStrategyOptions });
  const statusQ = useQueries({
    queries: (optionsQ.data ?? []).map((s) => ({ queryKey: chainsKeys.status(s.id), queryFn: () => fetchSweepParams(s.id) })),
  });
  const chains = listQ.data?.chains ?? [];

  // The first day of the years band: the earliest day a generator search or a chain uses. Unknown (no years drawn before the
  // cut) while the searches load or when they could not be read, so a later first day is never drawn as the first.
  const starts = batchesQ.isSuccess ? [
    ...(batchesQ.data ?? []).map((b) => dateToEpoch(b.data_start)).filter((t): t is number => t != null),
    ...chains.map((c) => c.screen_start_ts),
  ] : [];
  const start = starts.length ? Math.min(...starts) : null;

  // The finding, once the chains and every strategy's declared settings have answered.
  let say = null;
  const settingsRead = optionsQ.isSuccess && statusQ.every((q) => q.isSuccess);
  const settingsError = optionsQ.isError ? optionsQ.error : statusQ.find((q) => q.isError)?.error;
  if (listQ.isSuccess && (settingsRead || settingsError != null)) {
    const n = chains.length;
    const waiting = chains.filter((c) => c.group === "waiting-on-you").length;
    const running = chains.filter((c) => c.group === "active").length;
    const total = optionsQ.data?.length ?? 0;
    const declared = statusQ.filter((q) => (q.data?.sweep_params ?? []).length > 0).length;
    say = (
      <Say
        lead={n === 0 ? "No chain has run yet." : `${n} chain${n === 1 ? "" : "s"}. ${waiting ? `${waiting} wait${waiting === 1 ? "s" : ""} for you.` : running ? `${running} running.` : "None running."}`}
        more={<>
          {"A chain takes one strategy through six steps by itself and stops once, at the step where "}
          <em className="warn">you pick which result to carry on with</em>{". "}
          {settingsRead && total > 0 && (declared === 0
            ? `None of your ${total} strategies lists settings for a chain to try.`
            : `${declared} of your ${total} strategies list${declared === 1 ? "s" : ""} settings to try.`)}
        </>}
      />
    );
  }

  return (
    <div className="v2-page cn6">
      <PageHeader
        crumb={app.name}
        title={section.label}
        right={<PhoneOff><button type="button" className="btn" onClick={() => setStartSignal((n) => n + 1)}>Start a chain</button></PhoneOff>}
      />
      {say}
      {settingsError != null && <p className="rd" role="status">{`The strategies' settings could not be read: ${serverDetail(settingsError)}`}</p>}
      {batchesQ.isError && <p className="rd" role="status">{`The generator's searches could not be read, so the years before the cut are drawn without dates: ${serverDetail(batchesQ.error)}`}</p>}
      <ChainSteps chains={chains} />
      <ChainSection
        memoryKey="research/chains"
        startSignal={startSignal}
        years={
          <ChainYears cut={listQ.data?.reserve_cut_ts ?? null} cutError={listQ.isError ? serverDetail(listQ.error) : null}
            windows={supplyQ.data?.windows ?? null} start={start}
            error={supplyQ.isError ? serverDetail(supplyQ.error) : null} />
        }
      />
    </div>
  );
}
