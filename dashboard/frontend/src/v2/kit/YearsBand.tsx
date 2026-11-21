import { fmtDmy } from "../../lib/format";
import { Legend } from "./parts";

export type Stage = "screen" | "optimise" | "walkforward" | "later";

/** The colour of each stretch of history, in the order the key shows them. */
export const STRETCHES: Array<{ stage: Stage; label: string; colour: string }> = [
  { stage: "screen", label: "Screen", colour: "#29c6ff" },
  { stage: "optimise", label: "Optimise", colour: "#8fa3ff" },
  { stage: "walkforward", label: "Walk-forward", colour: "#e8d49a" },
  { stage: "later", label: "Later", colour: "#8d98a8" },
];

/** The colour key of the stretches, for a page header. */
export function StageLegend() {
  return <Legend items={STRETCHES.map((s): [string, string] => [s.label, s.colour])} />;
}

const DAY = 86400;

/**
 * The axis a band uses: 1 January (UTC) of the first start year rounded down to a multiple of 5, to
 * one day after the latest stretch end or cut. All times are epoch seconds.
 */
export function bandAxis(stretches: Array<{ from: number; to: number }>, cut: number | null): { from: number; to: number } {
  const firstYear = new Date(Math.min(...stretches.map((s) => s.from)) * 1000).getUTCFullYear();
  const from = Date.UTC(Math.floor(firstYear / 5) * 5, 0, 1) / 1000;
  const last = Math.max(...stretches.map((s) => s.to), ...(cut == null ? [] : [cut]));
  return { from, to: last + DAY };
}

/**
 * Which years a chain used: a track from `from` to `to` (epoch seconds), one coloured stretch per
 * stage, an optional dashed reserve cut, and a year mark under every fifth year.
 */
export function YearsBand({
  stretches, cut, from, to,
}: {
  stretches: Array<{ stage: Stage; from: number; to: number }>;
  cut?: number | null;
  from: number;
  to: number;
}) {
  const span = to - from;
  const share = (t: number) => `${(((t - from) / span) * 100).toFixed(2)}%`;
  const firstYear = new Date(from * 1000).getUTCFullYear();
  const lastYear = new Date(to * 1000).getUTCFullYear();
  const ticks: Array<{ year: number; at: number }> = [];
  for (let year = Math.ceil(firstYear / 5) * 5; year <= lastYear; year += 5) {
    const at = Date.UTC(year, 0, 1) / 1000;
    if (at >= from && at <= to) ticks.push({ year, at });
  }
  return (
    <div>
      <div style={{ position: "relative", height: 14, borderRadius: 4, background: "rgba(255,255,255,.04)" }}>
        {stretches.filter((s) => s.to > s.from).map((s) => {
          const meta = STRETCHES.find((m) => m.stage === s.stage)!;
          return (
            <i
              key={s.stage}
              data-stretch={s.stage}
              title={`${meta.label}: ${fmtDmy(s.from)} to ${fmtDmy(s.to)}`}
              style={{ position: "absolute", top: 0, bottom: 0, left: share(s.from), width: `${(((s.to - s.from) / span) * 100).toFixed(2)}%`, background: meta.colour, opacity: 0.85, borderRadius: 3 }}
            />
          );
        })}
        {typeof cut === "number" && (
          <i
            data-cut
            title={`Reserve from ${fmtDmy(cut)}`}
            style={{ position: "absolute", top: -4, bottom: -4, left: share(cut), width: 0, borderLeft: "2px dashed #eef2f7" }}
          />
        )}
      </div>
      <div style={{ position: "relative", height: 16, marginTop: 4, fontSize: 11, color: "var(--soft)", fontFamily: "var(--mono)" }}>
        {ticks.map((t) => (
          <span key={t.year} data-tick style={{ position: "absolute", left: share(t.at), transform: "translateX(-50%)" }}>{t.year}</span>
        ))}
      </div>
    </div>
  );
}
