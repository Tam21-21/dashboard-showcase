import "./lookup.css";
import { useRef } from "react";
import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../../../lib/api";
import { serverDetail } from "../../../../lib/errorText";
import { Block, Say, Window } from "../../../kit";
import { LibCard } from "../markets/LibCard";
import { shortReason } from "../markets/series";
import { AccountsPanels, accountsParts, useAccountsAnswer } from "./Accounts";
import { NewsPanel, newsCard } from "./NewsPart";
import { PricePanel, priceParts } from "./PricePart";
import { coShort } from "./names";
import type { Company } from "./names";

// The ticker lookup's window (mockup: the frozen mockup of 2026-11-18, openCo). It opens in its final shape at once: the finding in
// large words, a Summary of one card for each part, then each part's panel. A part is grey shapes until its own answer is in, so
// the price can be read while the news is still on its way. Display data only; nothing here is stored.

const bone = (cls: string) => <i className={`lk-bone b-${cls}`} aria-hidden="true" />;

/** A part that has not answered: its name, "Fetching" and grey shapes where the figure, the line and the facts will be. */
function waitCard(name: string, k: string) {
  return (
    <div className="lk-wait" data-k={k} aria-busy="true" key={k}>
      <LibCard name={name} hue="data" chip={{ tone: "run", text: "Fetching" }} fig={bone("fig")} unit={bone("sub")} pic={<>{bone("pic")}{bone("facts")}</>} />
    </div>
  );
}

/** A part the server refused: "Not read", a dash with the server's words on hover. */
function badCard(name: string, words: string, k: string) {
  return (
    <div className="lk-wait" data-k={k} key={k}>
      <LibCard name={name} chip={{ tone: "bad", text: "Not read" }} fig={<span title={words}>-</span>} pic={<span className="lc-bar" />} />
    </div>
  );
}

const BONES = {
  prices: (
    <Block className="lk-sk">
      <div className="lk-sk-head">{bone("head")}{bone("seg")}</div>{bone("chart")}{bone("kv")}
    </Block>
  ),
  filings: (
    <>
      <Block className="lk-sk"><div className="lk-sk-head">{bone("head")}{bone("yr")}</div>{bone("c1")}</Block>
      <Block className="lk-sk"><div className="lk-sk-head">{bone("head")}</div>{bone("c2")}</Block>
      <Block className="lk-sk"><div className="lk-sk-head">{bone("head")}</div>{bone("c2")}</Block>
      <Block className="lk-sk"><div className="lk-sk-head">{bone("head")}</div>{bone("c3")}</Block>
    </>
  ),
  news: (
    <Block className="lk-sk">
      <div className="lk-sk-head">{bone("head")}</div>
      {Array.from({ length: 6 }, (_, i) => <div className="lk-nb" key={i}>{bone("when")}{bone("line")}</div>)}
    </Block>
  ),
};

export default function LookupWindow({ company, onClose }: { company: Company; onClose: () => void }) {
  const short = coShort(company.name);
  const body = useRef<HTMLDivElement>(null);
  const prices = useQuery({ queryKey: ["display", "history", company.symbol, "max"], queryFn: () => api.display.getHistory(company.symbol, "max"), retry: false });
  const { history, notHeld, failure } = useAccountsAnswer(company.cik, company.ticker, short);
  const news = useQuery({ queryKey: ["display", "news", company.ticker], queryFn: () => api.display.getNews(company.ticker), retry: false });

  const scrollTo = (id: string) => () => body.current?.querySelector(`#${id}`)?.scrollIntoView?.({ behavior: "smooth", block: "start" });

  let say: { lead: ReactNode; more: ReactNode };
  let priceCard: ReactNode;
  let pricePanel: ReactNode;
  if (prices.isPending) {
    say = { lead: `Fetching ${short}.`, more: null };
    priceCard = waitCard("Share price", "prices");
    pricePanel = BONES.prices;
  } else {
    const failed = prices.isError ? serverDetail(prices.error) : prices.data.bars.length === 0 ? (shortReason(0, 1) as string) : null;
    if (failed !== null || !prices.data) {
      const words = failed ?? "No prices came back.";
      say = { lead: `${short}'s prices could not be read.`, more: null };
      priceCard = badCard("Share price", words, "prices");
      pricePanel = <Block id="lk-prices" title="Share price"><div className="empty">{words}</div></Block>;
    } else {
      const p = priceParts(short, prices.data.bars, prices.data);
      say = { lead: p.lead, more: p.more };
      priceCard = <LibCard {...p.card} onClick={scrollTo("lk-prices")} />;
      pricePanel = <PricePanel symbol={company.symbol} bars={prices.data.bars} answer={prices.data} />;
    }
  }

  let newsCardNode: ReactNode;
  let newsPanel: ReactNode;
  if (news.isPending) {
    newsCardNode = waitCard("News", "news");
    newsPanel = BONES.news;
  } else if (news.isError) {
    const words = serverDetail(news.error);
    newsCardNode = badCard("News", words, "news");
    newsPanel = <Block id="lk-news" title="News"><div className="empty">{words}</div></Block>;
  } else {
    newsCardNode = <LibCard {...newsCard(news.data)} onClick={scrollTo("lk-news")} />;
    newsPanel = <NewsPanel answer={news.data} />;
  }

  let filingsCard: ReactNode;
  let filingsPanel: ReactNode;
  let accountsMore: ReactNode = null;
  if (history.isError) {
    const words = serverDetail(history.error);
    filingsCard = badCard("Revenue", words, "filings");
    filingsPanel = <Block id="lk-filings" title="Revenue and net income"><div className="empty">{words}</div></Block>;
  } else if (history.isPending || (notHeld && !failure)) {
    filingsCard = waitCard("Revenue", "filings");
    filingsPanel = BONES.filings;
  } else if (notHeld && failure) {
    filingsCard = badCard("Revenue", failure.words, "filings");
    filingsPanel = <Block id="lk-filings" title="Revenue and net income"><div className="empty" title={failure.hover}>{failure.words}</div></Block>;
  } else if (history.data.periods.length === 0) {
    const words = `No yearly report of ${short} is held.`;
    filingsCard = <LibCard name="Revenue" chip={{ tone: "dim", text: "No yearly report" }} fig={<span title={words}>-</span>} pic={<span className="lc-bar" />} />;
    filingsPanel = <Block id="lk-filings" title="Revenue and net income"><div className="empty">{words}</div></Block>;
  } else {
    const a = accountsParts(history.data);
    accountsMore = a.more;
    filingsCard = <LibCard {...a.card} onClick={scrollTo("lk-filings")} />;
    filingsPanel = <AccountsPanels h={history.data} />;
  }
  if (prices.isPending && !accountsMore) say = { ...say, more: "Prices, company accounts and news are on their way from the server." };
  const more = say.more && accountsMore ? <>{say.more} {accountsMore}</> : (say.more ?? accountsMore);

  return (
    <Window label={company.name} size="xl" className="lk-win" onClose={onClose}>
      <div className="win-h">
        <h2>{company.name}<span className="lk-tk num">{company.ticker}</span></h2>
        <button type="button" className="btn q" onClick={onClose}>Close</button>
      </div>
      <div className="lk" ref={body}>
        <Say lead={say.lead} more={more} />
        <Block title="Summary" className="lk-sum">
          <div className="lc-wall">{priceCard}{filingsCard}{newsCardNode}</div>
        </Block>
        {pricePanel}
        {filingsPanel}
        {newsPanel}
      </div>
    </Window>
  );
}
