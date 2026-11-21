import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

// The live feed: one EventSource on the server's /api/stream. Each event the server names makes the
// pages that read that data refetch. Events sent while the connection was down are lost, so when the
// connection opens again after an error every query is invalidated once.

export type LiveState = "connecting" | "open" | "closed" | "off";

const CALENDAR = [["calendar"], ["calendar-today"]];
const GARMIN = [["garmin-today"], ["garmin-days"]];
const ACTIVITIES = [["garmin-activities"], ["garmin-activity"]];
const PAPER = [["paper"], ["overview", "paper-runs"], ["overview", "paper-summary"]];
const RUNS = [["runs"], ["generator"],["overview", "runs"], ["overview", "runs-summary"]];
const DATASETS = [["overview", "datasets"]];
const CHAINS = [["chains"]];
const TODOS = [["todos"]];
const STRATEGIES =[["strategies"], ["overview", "strategies"]];

export const LIVE_EVENTS: Record<string, readonly (readonly string[])[]> = {
  calendar_synced: CALENDAR,
  todo_added: TODOS,
  todo_toggled: TODOS,
  todo_updated: TODOS,
  todo_deleted: TODOS,
  todos_reordered: TODOS,
  garmin_synced: GARMIN,
  garmin_activities_synced: ACTIVITIES,
  "paper.run_killed": PAPER,
  "paper.run_promoted": PAPER,
  "paper.run_admitted": [...PAPER, ["judge"], ["overview", "judge-summary"], ["overview", "verdicts"]],
  "research.run_created": RUNS,
  "research.run_started": RUNS,
  "research.run_completed": RUNS,
  "research.run_failed": RUNS,
  "research.run_cancelled": RUNS,
  "research.dataset_created": DATASETS,
  "research.dataset_status_changed": DATASETS,
  "research.chain_changed": CHAINS,
  "research.strategy_registered": STRATEGIES,
  "research.strategy_deleted": STRATEGIES,
};

export function useLiveFeed(): LiveState {
  const qc = useQueryClient();
  const [state, setState] = useState<LiveState>(typeof EventSource === "undefined" ? "off" : "connecting");

  useEffect(() => {
    if (typeof EventSource === "undefined") {
      setState("off");
      return;
    }
    const source = new EventSource("/api/stream");
    let failed = false;
    for (const [name, prefixes] of Object.entries(LIVE_EVENTS)) {
      source.addEventListener(name, () => {
        for (const queryKey of prefixes) void qc.invalidateQueries({ queryKey: [...queryKey] });
      });
    }
    source.onopen = () => {
      setState("open");
      if (failed) {
        failed = false;
        void qc.invalidateQueries();
      }
    };
    source.onerror = () => {
      failed = true;
      setState(source.readyState === EventSource.CLOSED ? "closed" : "connecting");
    };
    return () => source.close();
  }, [qc]);

  return state;
}
