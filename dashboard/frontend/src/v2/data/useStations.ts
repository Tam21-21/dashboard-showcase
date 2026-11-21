import { useQuery } from "@tanstack/react-query";
import { serverDetail } from "../../lib/errorText";
import {
  fetchDatasets, fetchJudgeSummary, fetchPaperSummary, fetchRuns, fetchRunsSummary, fetchStrategies, overviewKeys,
} from "./overviewApi";
import { stations, type Station, type StationId } from "./stations";

/** The six stations as the Research overview reads them, for any page that shows their words (the phone's list of pages). */
export function useStations(): Station[] {
  const datasetsQ = useQuery({ queryKey: overviewKeys.datasets, queryFn: fetchDatasets });
  const strategiesQ = useQuery({ queryKey: overviewKeys.strategies, queryFn: fetchStrategies });
  const runsQ = useQuery({ queryKey: overviewKeys.runs, queryFn: fetchRuns });
  const summaryQ = useQuery({ queryKey: overviewKeys.runsSummary, queryFn: fetchRunsSummary });
  const judgeQ = useQuery({ queryKey: overviewKeys.judgeSummary, queryFn: fetchJudgeSummary });
  const paperQ = useQuery({ queryKey: overviewKeys.paperSummary, queryFn: fetchPaperSummary });
  // A station whose route failed says so on hover, in the server's words.
  const failedRead: Partial<Record<StationId, unknown>> = {
    data: datasetsQ.error, strategies: strategiesQ.error ?? runsQ.error, runs: runsQ.error ?? summaryQ.error, judge: judgeQ.error, paper: paperQ.error,
  };
  const sum = summaryQ.data;
  return stations({
    datasets: datasetsQ.data, strategies: strategiesQ.data, runs: runsQ.data, runsTotal: sum?.total,
    runsFailed: sum ? (sum.by_status.failed ?? 0) : undefined, judge: judgeQ.data, paperRunning: paperQ.data?.running_count,
    now: Math.floor(Date.now() / 1000),
  }).map((s) => (failedRead[s.id] ? { ...s, word: "Could not read", why: serverDetail(failedRead[s.id]) } : s));
}
