// The step line: one number at each step of a sequence, drawn after the Chain mockup's chainLine
// (docs/design/rail_mockup_2026-10-10.html, research/CHAIN). A dot sits at every step that has a value,
// joined to its neighbour by a line only when that neighbour has one too; a step with no value shows a
// dash that says why on hover. There is no pass bar, because the server sends none.

export interface StepLineStep {
  /** Machine key of the step, written on its dot or dash as data-step. */
  step: string;
  /** Plain name, as shown in the hover text. */
  label: string;
  value: number | null;
  /** Why there is no value; shown on the dash. */
  why: string | null;
  /** This value minus the nearest earlier value, or null. */
  change: number | null;
}

const W = 600;
const H = 150;
const PAD = { t: 22, r: 16, b: 20, l: 16 };

const caption = (label: string) => (label.toUpperCase() === "WALK-FORWARD" ? "WALK-FWD" : label.toUpperCase());
const signed = (v: number, fmt: (n: number) => string) => (v > 0 ? `+${fmt(v)}` : fmt(v));

export function StepLine({ testid, steps, fmt }: { testid: string; steps: StepLineStep[]; fmt: (v: number) => string }) {
  const known = steps.flatMap((s) => (s.value == null ? [] : [s.value]));
  const lo = Math.min(0, ...known);
  const hi = Math.max(1.2, ...known);
  const sx = (i: number) => PAD.l + 24 + (i * (W - PAD.l - PAD.r - 48)) / Math.max(1, steps.length - 1);
  const sy = (v: number) => H - PAD.b - ((v - lo) / (hi - lo)) * (H - PAD.b - PAD.t);

  let previous: number | null = null;
  const colours = steps.map((s) => {
    if (s.value == null) return null;
    const colour = previous == null ? "var(--hi)" : s.value >= previous ? "var(--g)" : "var(--r)";
    previous = s.value;
    return colour;
  });

  return (
    <div data-testid={testid}>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} role="img" aria-label="Sharpe at each step">
        <line x1={PAD.l} x2={W - PAD.r} y1={sy(0)} y2={sy(0)} stroke="rgba(255,255,255,0.14)" />
        {steps.map((s, i) => (
          <text key={`l-${s.step}`} x={sx(i)} y={H - 4} fontSize="10" fill="#8d98a8" textAnchor="middle">{caption(s.label)}</text>
        ))}
        {steps.map((s, i) => {
          const next = steps[i + 1];
          if (s.value == null || next == null || next.value == null) return null;
          return <line key={`j-${s.step}`} x1={sx(i)} y1={sy(s.value)} x2={sx(i + 1)} y2={sy(next.value)} stroke="#eef2f7" strokeWidth={2} />;
        })}
        {steps.map((s, i) => {
          if (s.value == null) {
            return (
              <text key={s.step} data-step={s.step} data-unknown x={sx(i)} y={sy(0) - 8} fontSize="14" fill="#8d98a8" textAnchor="middle">
                -<title>{s.why ?? "No value"}</title>
              </text>
            );
          }
          const tip = `${s.label}: Sharpe ${fmt(s.value)}${s.change == null ? "" : `, change ${signed(s.change, fmt)}`}`;
          return (
            <g key={s.step}>
              <circle data-step={s.step} cx={sx(i)} cy={sy(s.value)} r={4.5} fill={colours[i] as string}>
                <title>{tip}</title>
              </circle>
              <text x={sx(i)} y={sy(s.value) - 10} fontSize="11" fill="#c3cbd6" textAnchor="middle">{fmt(s.value)}</text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
