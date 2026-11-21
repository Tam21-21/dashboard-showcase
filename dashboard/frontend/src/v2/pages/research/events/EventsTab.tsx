import { useEffect, useState } from "react";
import type { CSSProperties } from "react";
import { keepPreviousData, useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { api } from "../../../../lib/api";
import { plainDetail, serverDetail } from "../../../../lib/errorText";
import { Block, Say } from "../../../kit";
import ChartWindow from "../markets/ChartWindow";
import { LibCard } from "../markets/LibCard";
import type { CardTone } from "../markets/LibCard";
import type { ChartTarget } from "../markets/Tiles";
import { isStale } from "../datasets/stale";
import EarningsPanel from "./EarningsPanel";
import EventDetail, { RowLine } from "./EventDetail";
import { ACTION_COLOURS, actionWord, KINDS, rowStamp, sentenceCase } from "./eventText";
import type { SearchedEvent } from "./eventText";
import "./events.css";

// The Events tab of the Data library (mockup: docs/design/rail_mockup_2026-10-10.html, dlEvents).
// Left out because the server has nothing behind them: Company filings (lane EA),
// the strip's REFRESH and Keep up to date, and a 13D's link to SEC. The methods are in
// docs/research_lab/display_methods.md, "Data library: Events tab, 2026-10-22".

// The first page is twelve rows and each Show more adds forty-eight, as the mockup's table.
const PAGE = 12;
const MORE = 48;
const PAUSE_MS = 300;
const RATING_COLOUR = KINDS[1].colour;
const NO_REASON = "no reason recorded";

const grouped = (n: number) => n.toLocaleString("en-US");
const chartOf = (ticker: string): ChartTarget => ({ name: ticker, symbol: `${ticker}.US`, dp: 2 });

export default function EventsTab(_props: { slot?: HTMLElement | null } = {}) {
  const feedsQ = useQuery({ queryKey: ["events", "feeds"], queryFn: () => api.research.getEventFeeds(1) });
  const catalogQ = useQuery({ queryKey: ["events", "catalog"], queryFn: () => api.research.listDatasets() });
  const topQ = useQuery({ queryKey: ["events", "news-top"], queryFn: () => api.research.newsTop() });
  const actionsQ = useQuery({ queryKey: ["events", "rating-actions"], queryFn: () => api.research.ratingActions() });

  const [on, setOn] = useState<string[]>(KINDS.map((k) => k.type));
  const [action, setAction] = useState<string | null>(null);
  const [typed, setTyped] = useState("");
  const [q, setQ] = useState("");
  const [chart, setChart] = useState<ChartTarget | null>(null);
  const [open, setOpen] = useState<SearchedEvent | null>(null);

  useEffect(() => {
    const t = setTimeout(() => setQ(typed.trim()), PAUSE_MS);
    return () => clearTimeout(t);
  }, [typed]);

  const types = KINDS.map((k) => k.type).filter((t) => on.includes(t)).join(",");
  const searchQ = useInfiniteQuery({
    queryKey: ["events", "search", types, q, action],
    queryFn: ({ pageParam }) =>
      api.research.searchEvents({
        types, q: q || undefined, action: action ?? undefined, after_cursor: pageParam, limit: pageParam === undefined ? PAGE : MORE,
      }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next?.after_cursor,
    placeholderData: keepPreviousData,
  });

  const toggle = (type: string) => {
    if (on.includes(type) && on.length === 1) return;
    setOn(on.includes(type) ? on.filter((t) => t !== type) : [...on, type]);
  };
  const pickAction = (a: string) => {
    if (action === a) {
      setAction(null);
      return;
    }
    setAction(a);
    if (!on.includes("analyst_rating")) setOn([...on, "analyst_rating"]);
  };

  const now = Math.floor(Date.now() / 1000);
  const feeds = feedsQ.data ?? [];
  const catalog = catalogQ.data ?? [];

  const SOURCES: Record<string, string> = {
    news: "Alpaca news, Benzinga wire", analyst_rating: "Benzinga analyst calendar", sec_13d: "SEC EDGAR, Schedule 13D",
  };

  function feedCard(type: string, name: string, colour: string) {
    const f = feeds.find((x) => x.event_type === type);
    const failed = catalog.filter((d) => d.dataset_type === "event_series" && d.event_type === type && d.status === "failed");
    const newest = f?.latest_knowledge_at ?? null;
    const n = f?.total_count ?? null;
    let chip: { tone: CardTone; text: string; hover?: string };
    if (failed.length > 0) chip = { tone: "bad", text: "Failed", hover: failed.map((d) => `${d.source}: ${d.last_error ? plainDetail(d.last_error) : NO_REASON}`).join("\n") };
    else if (n === null) chip = { tone: "dim", text: "Not read" };
    else if (n === 0) chip = { tone: "dim", text: "Nothing held" };
    else if (newest === null) chip = { tone: "dim", text: "Newest not read" };
    else chip = isStale(newest, now) ? { tone: "warn", text: "Stale" } : { tone: "good", text: "Working" };
    return (
      <LibCard
        key={type} name={name} chip={chip} hue={chip.tone === "dim" ? "" : chip.tone} fig={n === null ? "-" : grouped(n)}
        unit={`${n === 1 ? "event" : "events"}, from ${SOURCES[type]}`}
        bar={[{ tone: "soft", n: n ? 1 : 0.0001, colour: colour }]}
        facts={[{ label: "Newest", value: newest === null ? "-" : rowStamp(newest) }]}
        hover={`Source: ${SOURCES[type]}`} testId="feed-card"
      />
    );
  }

  const stripError = feedsQ.isError ? serverDetail(feedsQ.error) : catalogQ.isError ? serverDetail(catalogQ.error) : null;
  const pages = searchQ.data?.pages ?? [];
  const events = pages.flatMap((p) => p.events);
  const total = pages.length > 0 ? pages[0].total_matching : null;
  const top = topQ.data ?? [];
  const actions = actionsQ.data;

  const feedTotal = feeds.reduce((acc, f) => acc + f.total_count, 0);
  const held = feeds.filter((f) => f.total_count > 0).length;
  const countOf = (type: string) => grouped(feeds.find((f) => f.event_type === type)?.total_count ?? 0);
  const words = ["no", "one", "two", "three"];

  return (
    <div className="evt">
      <Say
        lead={feedsQ.isPending ? "Loading the event feeds." : `${grouped(feedTotal)} events are stored, in ${words[Math.min(held, 3)]} ${held === 1 ? "feed" : "feeds"}.`}
        more={feedsQ.isPending || stripError ? stripError : <><em className="data">{`${countOf("news")} news headlines`}</em>{" and "}<em className="data">{`${countOf("analyst_rating")} analyst ratings`}</em>{". An event is something that happened to a company at a known time."}</>}
      />

      <Block
        title={<span title="A feed is stale once its newest event is over 7 days old">Feeds</span>} className="ev-feeds"
        state={<span className="nl-st run">{`${held} of ${KINDS.length} hold events`}</span>}
      >
        {stripError && <div className="ev-err">{stripError}</div>}
        <div className="lc-wall">{KINDS.map((k) => feedCard(k.type, k.name, k.colour))}</div>
      </Block>

      <div className="ev-half">
        <Block title={<span title="Tickers named in news over the last 30 days; a ticker opens its chart">Most in the news, 30 days</span>}>
          {topQ.isError ? (
            <div className="ev-err">{serverDetail(topQ.error)}</div>
          ) : topQ.isPending ? (
            <div className="ev-none">Loading.</div>
          ) : top.length === 0 ? (
            <div className="ev-none">No news in the last 30 days</div>
          ) : (
            <div className="ev-top">
              {top.slice(0, 12).map((row) => (
                <button key={row.ticker} type="button" data-top={row.ticker} onClick={() => setChart(chartOf(row.ticker))}>
                  <span className="num ev-tkh">{row.ticker}</span>
                  <span className="ev-hbar"><i style={{ width: `${(row.count / top[0].count) * 100}%` }} /></span>
                  <span className="num ev-n">{row.count}</span>
                </button>
              ))}
            </div>
          )}
        </Block>

        <Block title="Analyst actions" state={actions ? <span className="nl-st run">{grouped(actions.total)}</span> : undefined}>
          {actionsQ.isError ? (
            <div className="ev-err">{serverDetail(actionsQ.error)}</div>
          ) : actionsQ.isPending ? (
            <div className="ev-none">Loading.</div>
          ) : !actions || actions.total === 0 || actions.actions.length === 0 ? (
            <div className="ev-none">No ratings are held yet</div>
          ) : (
            <div className="ev-acts">
              <div className="ev-kbar">
                {actions.actions.map((a, i) => (
                  <i
                    key={a.action} data-bar={a.action} className={action && action !== a.action ? "off" : ""}
                    style={{ background: ACTION_COLOURS[i % ACTION_COLOURS.length], flex: `${a.count} 1 0` }}
                    title={`${sentenceCase(actionWord(a.action)[0])}: ${grouped(a.count)}, ${Math.round((a.count / actions.total) * 100)}%`}
                    onClick={() => pickAction(a.action)}
                  />
                ))}
              </div>
              <div className="ev-legend">
                {actions.actions.map((a, i) => (
                  <button
                    key={a.action}
                    type="button"
                    data-act={a.action}
                    className={action === a.action ? "on" : action ? "off" : ""}
                    style={{ "--k": ACTION_COLOURS[i % ACTION_COLOURS.length] } as CSSProperties}
                    aria-pressed={action === a.action}
                    onClick={() => pickAction(a.action)}
                  >
                    <i />
                    <span>{sentenceCase(actionWord(a.action)[0])}</span>
                    <b className="num">{grouped(a.count)}</b>
                    <small>{Math.round((a.count / actions.total) * 100)}%</small>
                  </button>
                ))}
              </div>
            </div>
          )}
        </Block>
      </div>

      <EarningsPanel onChart={(t) => setChart(chartOf(t))} />

      <Block
        title="All events"
        state={<span className="nl-st run" data-testid="ev-total">{total === null ? "-" : grouped(total)}</span>}
      >
        <div className="ev-bar2">
          <input
            className="ev-search"
            aria-label="Search events"
            placeholder="Search headlines, firms, filings, tickers"
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
          />
          {KINDS.map((k) => {
            const count = feeds.find((f) => f.event_type === k.type)?.total_count;
            return (
              <button
                key={k.type}
                type="button"
                className={`ev-chip${on.includes(k.type) ? " on" : ""}`}
                style={{ "--k": k.colour } as CSSProperties}
                aria-pressed={on.includes(k.type)}
                onClick={() => toggle(k.type)}
              >
                <i />{k.name}{count !== undefined && <span className="num"> {grouped(count)}</span>}
              </button>
            );
          })}
          {action !== null && (
            <button type="button" className="ev-chip on" style={{ "--k": RATING_COLOUR } as CSSProperties} onClick={() => setAction(null)}>
              <i />{actionWord(action)[0].toLowerCase()} <span className="ev-x">✕</span>
            </button>
          )}
        </div>
        <div className="ev-head"><span>When</span><span>Kind</span><span>Ticker</span><span>What</span></div>
        <div className="ev-feed" data-testid="ev-feed">
          {searchQ.isError ? (
            <div className="ev-err">{serverDetail(searchQ.error)}</div>
          ) : searchQ.isPending ? (
            <div className="ev-none">Loading.</div>
          ) : events.length === 0 ? (
            <div className="ev-none">No events match</div>
          ) : (
            events.map((e) => <RowLine key={e.event_id} e={e} onOpen={setOpen} onChart={(t) => setChart(chartOf(t))} />)
          )}
        </div>
        {searchQ.hasNextPage && total !== null && (
          <div className="ev-foot">
            <span>{`${grouped(events.length)} of ${grouped(total)} shown`}</span>
            <button type="button" className="btn q" disabled={searchQ.isFetchingNextPage} onClick={() => void searchQ.fetchNextPage()}>
              {`Show ${grouped(Math.min(MORE, total - events.length))} more`}
            </button>
          </div>
        )}
      </Block>

      {open && <EventDetail event={open} onOpen={setOpen} onClose={() => setOpen(null)} />}
      {chart && <ChartWindow key={chart.symbol ?? chart.name} start={chart} onClose={() => setChart(null)} />}
    </div>
  );
}
