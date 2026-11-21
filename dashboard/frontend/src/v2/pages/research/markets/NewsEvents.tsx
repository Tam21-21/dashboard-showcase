import type { MarketNewsItem } from "../../../../types/display";
import { Block, State } from "../../../kit";
import { DASH, clockWord } from "./data";

/** The newest news rows. Display only: nothing here feeds research. */
export function News({ items, error }: { items: MarketNewsItem[] | undefined; error: string | null }) {
  return (
    <Block title={<span title="Shown here only, never used in research">News</span>}>
      {error && <div className="empty">{error}</div>}
      {!error && items && items.length === 0 && <div className="empty">No news rows yet.</div>}
      <div className="nws">
        {(items ?? []).map((n, i) => (
          <div key={i} data-testid="news-row">
            <span className="num lo" title={String(n.time)}>{clockWord(n.time)}</span>
            <span className="tk num">{n.ticker ?? DASH}</span>
            <span className="hl">{n.headline}</span>
          </div>
        ))}
      </div>
    </Block>
  );
}

/** There is no calendar feed behind this, so the panel says so and shows no dates. */
export function Events() {
  return (
    <Block title="This week's events" state={<State tone="warn">Not built yet</State>}>
      <div className="empty">The economic calendar feed is not connected yet.</div>
    </Block>
  );
}
