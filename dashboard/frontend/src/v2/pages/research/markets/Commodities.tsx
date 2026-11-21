import type { CommodityRow } from "../../../../types/display";
import { Block } from "../../../kit";
import { commodityName, commodityPct, signedPct, toneOf } from "./data";

/** Each commodity with its level, the change since the last print, and whether it prints daily or monthly. */
export default function Commodities({ rows, error }: { rows: CommodityRow[] | undefined; error: string | null }) {
  return (
    <Block title="Commodities">
      {error && <div className="empty">{error}</div>}
      {!error && rows && rows.length === 0 && <div className="empty">The commodities feed returned no rows.</div>}
      <div className="mk-com">
        {(rows ?? []).map((c) => {
          const pct = commodityPct(c);
          const monthly = c.grain === "monthly";
          return (
            <div key={c.name} data-testid="commodity-row" title={`${c.name}, printed ${c.observation_date}`}>
              <span className="nm">{commodityName(c.name)}</span>
              <span className="meta">
                <b className="num">{c.level.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</b>
                {c.unit ? ` ${c.unit.toLowerCase()}, ` : " "}{monthly ? "monthly" : "daily"}{" "}
                <b className={`num ${toneOf(pct) === "g" ? "good" : toneOf(pct) === "rd" ? "bad" : "dim"}`} title={pct == null ? "The previous print was zero, so there is no percent." : undefined}>{signedPct(pct, 1)}</b>
              </span>
            </div>
          );
        })}
      </div>
    </Block>
  );
}
