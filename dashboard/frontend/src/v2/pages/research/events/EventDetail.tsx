import { useQuery } from "@tanstack/react-query";
import { api } from "../../../../lib/api";
import { serverDetail } from "../../../../lib/errorText";
import { fmtDmy } from "../../../../lib/format";
import { Window } from "../../../kit";
import { articleUrl, detailRows, eventParts, kindOf, KINDS, rowStamp } from "./eventText";
import type { SearchedEvent, TextPart } from "./eventText";

// The detail overlay of one event (mockup: openEvent). The lines under MORE ON are the same
// ticker's other events, newest first, the event itself left out, six at most.

export const USE_IN_RUN_WHY = "An event run needs an event strategy; building one from here comes in a later lane";

// The words of a row or a detail, the action word and the percent in their colours.
export function EventText({ parts }: { parts: TextPart[] }) {
  return (
    <>
      {parts.map((p, i) =>
        p.word ? <span key={i} className={`state ${p.tone ?? ""}`.trim()}>{p.text}</span>
          : p.tone ? <span key={i} className={`num ${p.tone}`}>{p.text}</span>
          : <span key={i}>{p.text}</span>)}
    </>
  );
}

// One line of a feed: when, the type's colour and name, up to three tickers, the words. With `onChart` the
// tickers open the chart window; without it the first ticker is plain text (the lines in a detail).
export function RowLine({
  e, onOpen, onChart,
}: { e: SearchedEvent; onOpen: (e: SearchedEvent) => void; onChart?: (ticker: string) => void }) {
  const kind = kindOf(e.event_type);
  return (
    <div
      className="ev-line"
      data-ev={e.event_id}
      role="button"
      tabIndex={0}
      onClick={() => onOpen(e)}
      onKeyDown={(k) => { if (k.key === "Enter") onOpen(e); }}
    >
      <span className="num ev-when">{rowStamp(e.knowledge_at)}</span>
      <span className="ev-kind"><i style={{ background: kind.colour }} />{kind.name}</span>
      <span className="ev-tks">
        {onChart
          ? e.tickers.slice(0, 3).map((t) => (
            <button
              key={t}
              type="button"
              className="ev-tk"
              title={`${t}: opens its chart`}
              onClick={(ev) => { ev.stopPropagation(); onChart(t); }}
            >{t}</button>
          ))
          : e.tickers[0] && <span className="ev-tk plain">{e.tickers[0]}</span>}
      </span>
      <span className="tx"><EventText parts={eventParts(e)} /></span>
    </div>
  );
}

const MORE_SHOWN = 6;
const ALL_TYPES = KINDS.map((k) => k.type).join(",");

export default function EventDetail({
  event, onOpen, onClose,
}: { event: SearchedEvent; onOpen: (e: SearchedEvent) => void; onClose: () => void }) {
  const kind = kindOf(event.event_type);
  const ticker = event.tickers[0] ?? null;
  const url = articleUrl(event);
  const title = `${kind.name.toUpperCase()}, ${fmtDmy(event.knowledge_at)}`;


  const moreQ = useQuery({
    queryKey: ["events", "more", ticker, event.event_id],
    queryFn: () => api.research.searchEvents({ types: ALL_TYPES, ticker: ticker!, limit: MORE_SHOWN + 1 }),
    enabled: ticker !== null,
  });
  const more = (moreQ.data?.events ?? []).filter((x) => x.event_id !== event.event_id).slice(0, MORE_SHOWN);

  return (
    <Window label={title} size="xl" className="ev-detail" onClose={onClose}>
        <div className="ev-dhead">
          <i className="ev-dot" style={{ background: kind.colour }} />
          <span className="ev-dtitle">{title}</span>
          <span className="ev-grow" />
          {url && (
            <button type="button" className="btn q" onClick={() => window.open(url, "_blank", "noopener")}>OPEN ARTICLE</button>
          )}
          <button type="button" className="btn" disabled title={USE_IN_RUN_WHY}>Use in a run</button>
          <button type="button" className="btn q" onClick={onClose}>CLOSE</button>
        </div>
        <div className="ev-big"><EventText parts={eventParts(event)} /></div>
        <div className="ev-rows">
          {detailRows(event).map(([label, value]) => (
            <div key={label}><span>{label}</span><b>{value}</b></div>
          ))}
        </div>
        {ticker !== null && (moreQ.isPending || moreQ.isError || more.length > 0) && (
          <>
            <div className="kl ev-morel">MORE ON {ticker}</div>
            {moreQ.isError ? (
              <div className="ev-err">{serverDetail(moreQ.error)}</div>
            ) : moreQ.isPending ? (
              <div className="ev-none">Loading.</div>
            ) : (
              <div className="ev-feed">
                {more.map((x) => <RowLine key={x.event_id} e={x} onOpen={onOpen} />)}
              </div>
            )}
          </>
        )}
    </Window>
  );
}
