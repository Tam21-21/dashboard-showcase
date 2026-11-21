import type { ReactNode } from "react";
import "./look6.css";

// The new look's parts (round six, Tamer 2026-11-14, from the look trial on his Desktop, overview_trial/lab.html):
// a solid card with a plain title, the page's finding said in one large sentence, and a state word on a tint of its colour.

export type NlTone = "good" | "bad" | "warn" | "run" | "dim";

/** A solid card: the title in plain case, an optional state word beside it, an optional control on the right. */
export function Block({ title, state, right, id, className, children }: {
  title?: ReactNode; state?: ReactNode; right?: ReactNode; id?: string; className?: string; children?: ReactNode;
}) {
  return (
    <section className={className ? `nl-blk ${className}` : "nl-blk"} id={id}>
      {(title != null || right != null) && (
        <div className="nl-blk-h">
          <div className="nl-blk-t">{title != null && <h2>{title}</h2>}{state}</div>
          {right}
        </div>
      )}
      {children}
    </section>
  );
}

/** The page's finding, said once in large type; the second part in body type under it. */
export function Say({ lead, more }: { lead: ReactNode; more?: ReactNode }) {
  return (
    <p className="nl-say">
      <b>{lead}</b>
      {more != null && <span className="more">{more}</span>}
    </p>
  );
}

/** A state word on a tint of its colour; the colour never stands without the word. */
export function State({ tone, children, hover }: { tone: NlTone; children: ReactNode; hover?: string }) {
  return <span className={`nl-st ${tone}`} title={hover}>{children}</span>;
}
