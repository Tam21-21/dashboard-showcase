import { DASH, toneOf } from "./data";
import type { Fig } from "./series";

/** A figure as text, or a dash whose hover says why there is none. */
export default function FigText({
  fig, text, id, tone = false,
}: { fig: Fig; text: (v: number) => string; id?: string; tone?: boolean }) {
  const cls = fig.value !== null && tone ? toneOf(fig.value) : "";
  return (
    <span data-testid={id} className={`num ${cls}`.trim()} title={fig.reason ?? undefined}>
      {fig.value !== null ? text(fig.value) : DASH}
    </span>
  );
}
