import { useState } from "react";
import { Block, Segmented, ZeroBar } from "../../../kit";
import FigText from "./FigText";
import { SECTORS, SECTOR_PERIODS, signedPct, toneOf } from "./data";
import { changeOver } from "./series";
import { figFrom } from "./useBars";
import type { BarsMap } from "./useBars";

/** US sectors as bars around zero, the biggest rise first, a sector with no figure last. Counts only, no mood word. */
export default function Sectors({ bars }: { bars: BarsMap }) {
  const [period, setPeriod] = useState("week");
  const n = SECTOR_PERIODS.find((p) => p.value === period)?.bars ?? 5;
  const rows = SECTORS.map((s) => ({ ...s, fig: figFrom(bars.get(s.symbol), (b) => changeOver(b, n)) }));
  const shown = rows.filter((r) => r.fig.value !== null);
  const missing = rows.filter((r) => r.fig.value === null);
  shown.sort((a, b) => (b.fig.value as number) - (a.fig.value as number));
  const widest = Math.max(0, ...shown.map((r) => Math.abs(r.fig.value as number)));

  return (
    <Block
      title="US sectors"
      right={<Segmented variant="seg" options={SECTOR_PERIODS.map((p) => ({ label: p.label, value: p.value }))} value={period} onChange={setPeriod} />}
    >
      <div className="dv" title="Daily closes of the eleven US sector funds from Alpaca. Day = 1 trading day, week = 5, month = 21.">
        {[...shown, ...missing].map((r) => {
          const v = r.fig.value;
          return (
            <div key={r.symbol} data-testid="sector-row">
              <span className="dn2">{r.name}</span>
              <ZeroBar value={v} widest={widest} />
              <span className={`num ${toneOf(v)}`} title={r.symbol}><FigText fig={r.fig} text={(x) => signedPct(x)} /></span>
            </div>
          );
        })}
      </div>
    </Block>
  );
}
