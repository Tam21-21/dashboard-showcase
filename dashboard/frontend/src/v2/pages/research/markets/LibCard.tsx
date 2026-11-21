import type { ReactNode } from "react";

// The card every Data library tab uses for "one thing of this kind" (mockup: R2.tile and R2.card): a name with a state word on
// the right, a big figure with its unit, a bar that says a share, and a row of facts. A card with an onClick is a button.

export type CardTone = "good" | "bad" | "warn" | "run" | "dim" | "data" | "";
export type CardFact = { label: string; value: ReactNode; tone?: CardTone; hover?: string };

export function LibCard({
  name, chip, fig, unit, bar, pic, facts, hue = "", hover, onClick, testId, pressed,
}: {
  name: string;
  chip?: { tone: CardTone; text: string; hover?: string };
  fig: ReactNode;
  unit?: ReactNode;
  /** The bar: parts of one whole, each a tone and a size; no parts is an empty track. */
  bar?: Array<{ tone: CardTone | "soft"; n: number; colour?: string; hover?: string }>;
  /** A picture in place of the bar (the mockup's small bars): drawn as given. */
  pic?: ReactNode;
  facts?: CardFact[];
  hue?: CardTone;
  hover?: string;
  onClick?: () => void;
  testId?: string;
  pressed?: boolean;
}) {
  const parts = (bar ?? []).filter((p) => p.n > 0);
  const inner = (
    <>
      <span className="lc-top">
        <span className="lc-name">{name}</span>
        {chip && <span className={`nl-st ${chip.tone || "dim"}`} title={chip.hover}>{chip.text}</span>}
      </span>
      <span className="lc-fig"><span className="num">{fig}</span>{unit != null && <span className="lc-unit">{unit}</span>}</span>
      {pic ?? (
        <span className="lc-bar">
          {parts.map((p, i) => <i key={i} className={`t-${p.tone}`} style={{ flex: `${p.n} 1 0`, background: p.colour }} title={p.hover} />)}
        </span>
      )}
      {facts && facts.length > 0 && (
        <span className="lc-facts">
          {facts.map((f) => (
            <span key={f.label} title={f.hover}><i>{f.label}</i><b className={`num ${f.tone ?? ""}`}>{f.value}</b></span>
          ))}
        </span>
      )}
    </>
  );
  const cls = `lc${hue ? ` h-${hue}` : ""}`;
  return onClick ? (
    <button type="button" className={cls} data-testid={testId} data-name={name} title={hover} aria-pressed={pressed} onClick={onClick}>{inner}</button>
  ) : (
    <div className={cls} data-testid={testId} data-name={name} title={hover}>{inner}</div>
  );
}
