// Lane OP (Overview terminal, docs/units/OP/criteria.md). Plain-English types for the SEVEN
// display-only market routes lane OB already shipped and merged (dashboard/backend/routes/
// display.py). "Display-only" means this data is for showing on screen, never for a backtest:
// root CLAUDE.md's own rule says price/news data the dashboard shows a person must never leak
// into a research path. Every field here is copied straight from that file's own response shape,
// nothing invented, so a shape drift on the backend shows up here as a type error instead of a
// silent runtime guess.

// One row from GET /api/display/quotes. A symbol EODHD could not answer for still gets a row
// (the "available" flag), with the plain-English "reason" the route wrote, never a blank gap or a
// fabricated zero (AC-12: "an absent figure is a dash with its reason on hover").
export type MarketQuote =
  | {
      symbol: string;
      available: true;
      last: number | null;
      change: number | null;
      change_percent: number | null;
      // The upstream's own timestamp for THIS symbol (AC-3: "its own upstream timestamp, never
      // one page-level as-of"). ISO-8601 text, e.g. "2026-09-18T20:29:00+00:00".
      timestamp: string | null;
    }
  | {
      symbol: string;
      available: false;
      // The route's own plain-English sentence for why this one symbol has no quote right now.
      reason: string;
    };

// One bar as GET /api/display/history returns it: the exact shape routes/display.py's get_history
// builds, with BOTH an ISO calendar date (for a human) and a UNIX-epoch-seconds number (for the
// kit Chart, which needs a real number for an intraday-aware series).
export type HistoryBar = {
  time: string;
  epoch_seconds: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  /** Hourly bars only: true when the bar starts inside the regular session (09 to 15 New York time). */
  regular?: boolean;
};

export type HistoryResponse = {
  symbol: string;
  period: string;
  bars: HistoryBar[];
  time_form: string;
  /** The ticker lookup's extras: who served the bars, the newest day, the day the two sources join, why no join, what was left out. */
  source?: string;
  last_day?: string | null;
  join?: { day: string; scale: number } | null;
  fill_error?: string | null;
  left_out?: Record<string, number>;
  session?: { zone: string; open: string; close: string };
};

// One tenor from GET /api/display/yield-curve: the level AND the move against the previous
// observation, in basis points, plus which two calendar dates the move is actually between
// (AC-7's own text: "the level and the move in basis points").
export type YieldTenor = {
  tenor: string;
  level: number;
  change_bps: number;
  observation_date: string;
  previous_observation_date: string;
};

// One row from GET /api/display/commodities. "grain" says whether the print is daily or monthly
// (AC-7: "their own grain, so a monthly series says monthly rather than implying it is today's").
// "unit" is the upstream's own unit text (e.g. "Dollars per Barrel"), carried through as-is.
export type CommodityRow = {
  name: string;
  level: number;
  change: number;
  grain: string;
  observation_date: string;
  unit: string | null;
};

// One result from GET /api/display/search: a company from the SEC list, matched by ticker or name.
// "cik" is always a number (research_cik_ticker_map.cik is NOT NULL). "closest" is set on a row offered
// as a near match for a mistyped word, and is absent otherwise.
export type SearchResult = {
  /** The ticker plus ".US", the code the history route takes. */
  symbol: string;
  ticker: string;
  name: string;
  cik: number;
  closest?: boolean;
};

// GET /api/display/news: a ticker's recent headlines that name the company (the server already filters).
export type NewsItem = {
  time: string;
  headline: string;
  url: string;
  publisher: string | null;
  summary: string;
};
export type NewsResponse = {
  ticker: string;
  source: string;
  company_name: string | null;
  left_out: number;
  news: NewsItem[];
};

// One row from GET /api/display/market-news, already projected from the events store (never a
// second, drifting upstream call: routes/display.py reads the SAME rows Event Ops reads).
export type MarketNewsItem = {
  /** Epoch seconds as the route serves it today; an ISO string is read as well. */
  time: string | number;
  ticker: string | null;
  kind: string | null;
  headline: string;
};

// GET /api/display/market-session's one answer: the newest quote timestamp this SERVER PROCESS
// has ever actually seen (never an invented holiday calendar), and the word derived from how
// fresh it is ("unknown" until any quote has ever arrived, "live" or "stale" after).
export type MarketSessionResponse = {
  newest_quote_stamp: string | null;
  session: "unknown" | "live" | "stale" | string;
};
