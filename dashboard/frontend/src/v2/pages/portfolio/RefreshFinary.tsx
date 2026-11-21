import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Toast } from "../../kit";
import { getOverview, postRefresh } from "../../data/moneyApi";
import { serverDetail } from "../../../lib/errorText";

// The Refresh button of the Money and Positions heads (DECISIONS #494 item 1). A press asks the server to read
// Finary now, then the page asks for the overview every 10 seconds. A read counts as new when its read_at differs
// from the one shown at the press. After 5 minutes with no new read the wait ends and the run is linked.
// The waiting state lives in the query cache, so both pages' buttons agree. A reload forgets the wait.

const POLL_MS = 10_000;
const GIVE_UP_MS = 300_000;
const TITLE = "Reads what Finary holds now. An account Finary has not synced stays as it is";
const WAIT_KEY = ["money", "refresh"] as const;

interface Waiting { since: number; before: string | null; runUrl: string }
interface Message { text: string; link?: string }

export default function RefreshFinary() {
  const qc = useQueryClient();
  const waitingQ = useQuery<Waiting | null>({ queryKey: WAIT_KEY, queryFn: () => null, enabled: false, staleTime: Infinity, initialData: null });
  const waiting = waitingQ.data;
  const overview = useQuery({ queryKey: ["money", "overview"], queryFn: getOverview, refetchInterval: waiting ? POLL_MS : false });
  const [message, setMessage] = useState<Message | null>(null);
  // Only the component whose button was pressed writes a message.
  const mine = useRef<Waiting | null>(null);
  const readAt = overview.data?.read_at ?? null;

  const clearIf = (since: number) => {
    if (qc.getQueryData<Waiting | null>(WAIT_KEY)?.since === since) qc.setQueryData<Waiting | null>(WAIT_KEY, null);
  };

  useEffect(() => {
    const m = mine.current;
    if (!m || readAt === null || readAt === m.before) return;
    mine.current = null;
    clearIf(m.since);
    setMessage({ text: "Finary was read again." });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [readAt]);

  // A read that lands in another component's wait also ends this one's.
  useEffect(() => {
    if (!waiting || mine.current || readAt === null || readAt === waiting.before) return;
    clearIf(waiting.since);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [readAt, waiting]);

  // Its own timer, so a failing overview cannot hide the end of the wait.
  const since = waiting?.since ?? null;
  useEffect(() => {
    if (!waiting) return;
    const t = setTimeout(() => {
      clearIf(waiting.since);
      if (mine.current?.since === waiting.since) {
        mine.current = null;
        setMessage({ text: "No new read after 5 minutes.", link: waiting.runUrl });
      }
    }, Math.max(0, waiting.since + GIVE_UP_MS - Date.now()));
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [since]);

  async function press() {
    const startedAt = Date.now();
    const before = readAt;
    setMessage(null);
    try {
      const answer = await postRefresh();
      const value: Waiting = { since: startedAt, before, runUrl: answer.run_url };
      mine.current = value;
      qc.setQueryData<Waiting | null>(WAIT_KEY, value);
    } catch (err) {
      setMessage({ text: serverDetail(err) });
    }
  }

  const node = useMemo(
    () => (message ? (
      <>
        {message.text}
        {message.link ? <>{" "}<a href={message.link} target="_blank" rel="noreferrer">Open the run</a></> : null}
      </>
    ) : null),
    [message],
  );

  return (
    <>
      <button type="button" className="btn" title={TITLE} disabled={overview.isPending || waiting !== null} onClick={() => void press()}>
        {waiting ? "Reading Finary" : "Refresh"}
      </button>
      {node ? <Toast onClose={() => setMessage(null)}>{node}</Toast> : null}
    </>
  );
}
