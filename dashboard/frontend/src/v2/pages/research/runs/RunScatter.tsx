import { useRef } from "react";
import { useUnveil } from "../../../kit";
import { stepTicks, useWidth } from "./kinds";

// Runs with results: Sharpe up, deepest drop across. Where a run sits among the others is the point. A press on a dot
// opens that run below; the picked dot wears a ring and its name.

export type ScatterRun = { id: number; sharpe: number; drop: number; sign: "good" | "bad" | "soft"; title: string };

const COLOUR = { good: "47,224,138", bad: "255,90,99", soft: "141,152,168" };

export function RunScatter({ runs, picked, onPick }: { runs: ScatterRun[]; picked: number | null; onPick: (id: number) => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const W = useWidth(ref);
  useUnveil(ref, "x", { dur: 900 });
  const H = Math.round(Math.min(430, Math.max(280, W * 0.34)));
  const L = 44;
  const R = 14;
  const base = H - 34;
  const xMax = Math.max(0.05, ...runs.map((r) => r.drop)) * 1.08;
  const yLo = Math.min(0, ...runs.map((r) => r.sharpe));
  const yHi = Math.max(0.25, ...runs.map((r) => r.sharpe)) * 1.12;
  const X = (v: number) => L + (v / xMax) * (W - L - R);
  const Y = (v: number) => base - ((v - yLo) / (yHi - yLo)) * (base - 14);
  const ySteps = stepTicks(yLo, yHi, 0.25);
  const xSteps = stepTicks(0, xMax, xMax > 1.6 ? 0.5 : 0.2);
  return (
    <div ref={ref} className="sc" data-testid="scatter">
      {W > 0 && <span className="sc-y" title="The gain for the risk taken">Sharpe</span>}
      {W > 0 && (
        <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label="The runs with figures">
          <title>Each dot is one run. Higher is better, further left is safer. Press a dot to open that run below</title>
          {ySteps.map((v) => (
            <g key={v}>
              <line x1={L} x2={W - R} y1={Math.round(Y(v)) + 0.5} y2={Math.round(Y(v)) + 0.5} className="wk-grid" />
              <text x={L - 8} y={Y(v) + 4} textAnchor="end" className="wk-t">{v.toFixed(2)}</text>
            </g>
          ))}
          {xSteps.map((v) => (
            <text key={v} x={X(v)} y={base + 22} textAnchor="middle" className="wk-t">{`${Math.round(v * 100)}%`}</text>
          ))}
          {runs.map((r) => {
            const cx = X(r.drop);
            const cy = Y(r.sharpe);
            const sel = r.id === picked;
            const c = COLOUR[r.sign];
            const flip = cx > W - 90;
            return (
              <g key={r.id} data-run={r.id} className="sc-dot" onClick={() => onPick(r.id)}>
                <title>{r.title}</title>
                <circle data-dot cx={cx} cy={cy} r={8} fill={`rgba(${c},.32)`} stroke={`rgba(${c},.95)`} strokeWidth={1.5} />
                {sel && <circle cx={cx} cy={cy} r={13} fill="none" stroke="var(--hi)" strokeWidth={2} />}
                {sel && <text x={flip ? cx - 20 : cx + 20} y={cy + 4} textAnchor={flip ? "end" : "start"} className="sc-name">{`Run ${r.id}`}</text>}
              </g>
            );
          })}
        </svg>
      )}
      {W > 0 && <div className="sc-x"><span title="The largest fall from a high to the low after it">Deepest drop</span></div>}

    </div>
  );
}
