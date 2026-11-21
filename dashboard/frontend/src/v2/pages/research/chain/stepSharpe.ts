import type { ChainDetail, ChainStep, SweepCell } from "../../../../types/research";

// The Sharpe of each of the six chain steps. The server holds a Sharpe only for the optimise step (the best
// cell of the optimise run) and the chosen cell; every other step gets a reason instead of a number.

export const STEP_ORDER: ChainStep[] = ["screen", "optimise", "choose", "replay", "walkforward", "judge"];

export type StepSharpe = {
  step: ChainStep;
  value: number | null;
  why: string | null;
  /** This value minus the nearest earlier known value; null when there is none. */
  change: number | null;
};

const NEVER: Partial<Record<ChainStep, string>> = {
  screen: "The server does not send the screen run's Sharpe yet",
  replay: "The server does not send the replay run's Sharpe yet",
  walkforward: "The walk-forward run stores no Sharpe yet",
  judge: "The judge step has no Sharpe of its own",
};

export function stepSharpes(detail: ChainDetail, cells: SweepCell[] | null): StepSharpe[] {
  const values: Record<ChainStep, { value: number | null; why: string | null }> = {
    screen: { value: null, why: NEVER.screen! },
    optimise: { value: null, why: null },
    choose: { value: null, why: null },
    replay: { value: null, why: NEVER.replay! },
    walkforward: { value: null, why: NEVER.walkforward! },
    judge: { value: null, why: NEVER.judge! },
  };

  if (cells == null) {
    values.optimise.why = "This chain has no optimise run yet";
  } else {
    const sharpes = cells.map((c) => c.sharpe_ratio).filter((v): v is number => v != null);
    if (sharpes.length === 0) values.optimise.why = "The optimise run saved no Sharpe";
    else values.optimise.value = Math.max(...sharpes);
  }

  if (detail.chosen_variation_id == null || detail.chosen_ticker == null) {
    values.choose.why = "No cell has been chosen yet";
  } else {
    const chosen = cells?.find((c) => c.variation_id === detail.chosen_variation_id && c.ticker === detail.chosen_ticker);
    if (chosen?.sharpe_ratio == null) values.choose.why = "No Sharpe was saved for the chosen cell";
    else values.choose.value = chosen.sharpe_ratio;
  }

  let before: number | null = null;
  return STEP_ORDER.map((step) => {
    const { value, why } = values[step];
    const change = value != null && before != null ? value - before : null;
    if (value != null) before = value;
    return { step, value, why: value == null ? why : null, change };
  });
}
