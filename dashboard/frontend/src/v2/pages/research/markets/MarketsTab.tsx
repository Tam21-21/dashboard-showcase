import { useQuery } from "@tanstack/react-query";
import { api } from "../../../../lib/api";
import { serverDetail } from "../../../../lib/errorText";
import { usePageMemory } from "../../../kit";
import ChartWindow from "./ChartWindow";
import Commodities from "./Commodities";
import { News } from "./NewsEvents";
import Rates from "./Rates";
import Sectors from "./Sectors";
import Wall from "./Wall";
import type { ChartTarget } from "./Tiles";
import { BAR_SYMBOLS } from "./data";
import { useTileSources } from "./sources";

// The Markets tab of the Data library (mockup: docs/design/rail_mockup_2026-10-10.html,
// research/DATA LIBRARY; the frozen mockup of 2026-11-15, lab-markets.js). Every figure comes from the daily closes the server
// serves through /api/display/history, each by its method in docs/research_lab/display_methods.md. Display data
// only: none of it enters research. The tab is the wall of markets, then US sectors beside the rates and commodities, then news.

export default function MarketsTab() {
  const [chart, setChart] = usePageMemory<ChartTarget | null>("research/markets/chart", null);

  const sources = useTileSources(BAR_SYMBOLS);
  const { bars, tenors, yieldError, commodities, commoditiesError } = sources;
  const newsQ = useQuery({ queryKey: ["display", "market-news"], queryFn: () => api.display.getMarketNews(6), refetchInterval: 60_000 });

  return (
    <>
      <Wall sources={sources} onPick={setChart} />

      <div className="lay mk-lay">
        <Sectors bars={bars} />
        <div className="mk-stack">
          <Rates tenors={tenors} error={yieldError} />
          <Commodities rows={commodities} error={commoditiesError} />
        </div>
      </div>

      <div className="mk-lay">
        <News items={newsQ.data?.news} error={newsQ.isError ? serverDetail(newsQ.error) : null} />
      </div>

      {chart && <ChartWindow key={chart.symbol ?? chart.name} start={chart} onClose={() => setChart(null)} />}
    </>
  );
}
