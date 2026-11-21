import { useState } from "react";
import type { ComponentProps } from "react";
import type { NewsResponse } from "../../../../types/display";
import { Block, State } from "../../../kit";
import { LibCard } from "../markets/LibCard";
import { newsWhen, plain } from "./names";

// The news part of the lookup window (display_methods.md, "Ticker lookup window", News): the headlines that name the company, newest
// first, as the server served them (it already leaves out the ones that only touch it). When in Paris time, then the headline, which
// opens the article with its summary on hover. Display data only.

const FIRST_PAGE = 10;
const MORE_AT_MOST = 40;

type CardProps = ComponentProps<typeof LibCard>;

const named = (a: NewsResponse) => a.company_name ?? a.ticker;
const sourceWords = (a: NewsResponse) =>
  `Source: ${a.source}; the last 30 days, newest first. ${a.news.length} of the ${a.news.length + a.left_out} articles tagged ${a.ticker} name ${named(a)} or ${a.ticker} in the headline or summary; the rest only touch it and are left out. Times are Paris time.`;

/** The News card: the count, the share of the tagged articles that name the company, the newest one's time and the count left out. */
export function newsCard(a: NewsResponse): CardProps {
  const n = a.news.length;
  return {
    name: "News",
    hue: "data",
    chip: n > 0 ? { tone: "run", text: "Last 30 days", hover: sourceWords(a) } : { tone: "dim", text: "Nothing in 30 days", hover: sourceWords(a) },
    fig: String(n),
    unit: `${n === 1 ? "headline that names" : "headlines that name"} ${named(a)}, of ${n + a.left_out} tagged ${a.ticker}`,
    bar: [{ tone: "run", n }, { tone: "soft", n: a.left_out }],
    facts: [
      { label: "Newest", value: n > 0 ? newsWhen(a.news[0].time) : "-" },
      { label: "Left out", value: String(a.left_out) },
    ],
    testId: "lk-card-news",
  };
}

const opens = (url: string) => /^https?:\/\//i.test(url);

export function NewsPanel({ answer }: { answer: NewsResponse }) {
  const [shown, setShown] = useState(FIRST_PAGE);
  const total = answer.news.length;
  const left = total - shown;
  return (
    <Block
      id="lk-news"
      title={<span title={sourceWords(answer)}>News</span>}
      state={<><State tone="run">{`${total} ${total === 1 ? "headline" : "headlines"}`}</State><State tone="dim">Paris time</State></>}
    >
      {total === 0 ? (
        <div className="empty">No headline names it in the last 30 days.</div>
      ) : (
        <>
          <table className="fd-tbl lk-news">
            <thead><tr><th>When</th><th>Headline</th></tr></thead>
            <tbody>
              {answer.news.slice(0, shown).map((it, i) => (
                <tr key={`${it.time}-${i}`}>
                  <td className="lk-when">{newsWhen(it.time)}</td>
                  <td className="lk-hl">
                    {opens(it.url)
                      ? <a href={it.url} target="_blank" rel="noopener noreferrer" title={it.summary ? plain(it.summary) : undefined}>{plain(it.headline)}</a>
                      : <span title={it.summary ? plain(it.summary) : undefined}>{plain(it.headline)}</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {left > 0 && (
            <div className="lk-more">
              <button type="button" className="btn q" onClick={() => setShown(shown + Math.min(MORE_AT_MOST, left))}>{`Show ${Math.min(MORE_AT_MOST, left)} more`}</button>
              <span className="num">{`${shown} of ${total} shown`}</span>
            </div>
          )}
        </>
      )}
    </Block>
  );
}
