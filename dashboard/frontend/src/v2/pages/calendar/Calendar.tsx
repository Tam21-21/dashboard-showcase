import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Background } from "../../Background";
import { PageHeader, Say, Segmented, usePageMemory } from "../../kit";
import { PhoneOff, useLayout } from "../../kit/phone";
import type { PageProps } from "../../shell/sections";
import { serverDetail } from "../../../lib/errorText";
import type { CalendarEvent } from "../../../types";
import EventDetail from "./EventDetail";
import Grid from "./Grid";
import Month from "./Month";
import NewEvent from "./NewEvent";
import AskClaude from "./AskClaude";
import Templates from "./Templates";
import KindCard, { bookedMinutes } from "./KindCard";
import Today, { TodoPanel } from "./Today";
import WeatherCard from "./WeatherCard";
import {
  addDays, isTimed, stepAnchor, todayIso, useCalendarRange, useCalendarStatus, useForecast, useTodoList, useWeather, viewDays, viewTitle,
} from "./data";
import type { Drag, View } from "./data";
import "./calendar.css";

// Drag, stretch, delete and templates write through the server; a refusal shows in the header line.
// FULL SCREEN puts the page alone on the screen (the browser's full screen), without the side column, and the
// day and week grids fit 06:00 to 23:00 to the screen's height; Esc or EXIT FULL SCREEN leaves it.

const VIEWS = [
  { label: "Today", value: "TODAY" },
  { label: "Day", value: "DAY" },
  { label: "Week", value: "WEEK" },
  { label: "Month", value: "MONTH" },
];

// The phone offers no WEEK grid.
const PHONE_VIEWS = VIEWS.filter((v) => v.value !== "WEEK");

type Nav = { view: View; anchor: string };

export default function Calendar({ app }: PageProps) {
  const today = todayIso();
  const phone = useLayout() === "phone";
  const [laptopNav, setLaptopNav] = usePageMemory<Nav>("calendar/view", { view: "WEEK", anchor: today });
  // On the phone the view is local state that starts at TODAY on every mount; the laptop's remembered view is never touched.
  const [phoneNav, setPhoneNav] = useState<Nav>({ view: "TODAY", anchor: today });
  const nav = phone ? phoneNav : laptopNav;
  const setNav = (next: Nav | ((prev: Nav) => Nav)) => (phone ? setPhoneNav(next) : setLaptopNav(next));
  // TODAY lives in the address (?view=today) on the laptop, never in the stored view, so the stored default stays WEEK.
  const [params, setParams] = useSearchParams();
  const view: View = !phone && params.get("view") === "today" ? "TODAY" : nav.view;
  const { anchor } = nav;
  const setView = (v: View) => {
    if (v === "TODAY" && phone) {
      setNav((n) => ({ ...n, view: "TODAY" }));
      return;
    }
    if (v === "TODAY") {
      setParams({ view: "today" });
      return;
    }
    setParams({});
    setNav((n) => ({ ...n, view: v }));
  };
  const setAnchor = (a: string) => setNav((n) => ({ ...n, anchor: a }));
  const [open, setOpen] = useState<CalendarEvent | null>(null);
  const [creating, setCreating] = usePageMemory("calendar/new-event/open", false);
  const [drag, setDrag] = useState<Drag | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const pageRef = useRef<HTMLDivElement>(null);
  const [full, setFull] = useState(false);

  useEffect(() => {
    const sync = () => setFull(document.fullscreenElement !== null && document.fullscreenElement === pageRef.current);
    document.addEventListener("fullscreenchange", sync);
    return () => document.removeEventListener("fullscreenchange", sync);
  }, []);
  const toggleFull = () => {
    const page = pageRef.current;
    if (!page) return;
    const done = full ? document.exitFullscreen() : page.requestFullscreen();
    done.catch((err: unknown) =>
      setActionError(`The browser refused full screen: ${err instanceof Error ? err.message : String(err)}`));
  };

  const days = viewDays(view, anchor);
  const statusQ = useCalendarStatus();
  const rangeQ = useCalendarRange(days[0], days[days.length - 1]);
  const weatherQ = useWeather();
  const forecastQ = useForecast();

  let line: { text: string; retry: boolean; settings: boolean } | null = null;
  if (actionError) {
    line = { text: actionError, retry: false, settings: false };
  } else if (statusQ.isError || rangeQ.isError) {
    line = { text: serverDetail(statusQ.isError ? statusQ.error : rangeQ.error), retry: true, settings: false };
  } else if (statusQ.data && !statusQ.data.configured) {
    line = { text: "Google sign-in is not set up on this server.", retry: false, settings: false };
  } else if (statusQ.data && !statusQ.data.connected) {
    line = { text: "Google Calendar is not connected.", retry: false, settings: true };
  }
  const retry = () => {
    if (statusQ.isError) void statusQ.refetch();
    if (rangeQ.isError) void rangeQ.refetch();
  };

  const events = rangeQ.data ?? [];
  const openDay = (d: string) => {
    setParams({});
    setNav({ view: "DAY", anchor: d });
  };

  // The finding: what is on today, what comes next, the week ahead and the to-do list.
  const weekQ = useCalendarRange(today, addDays(today, 6));
  const todoList = useTodoList(today);
  const week = weekQ.data ?? [];
  const todays = week.filter((e) => e.start_dt.slice(0, 10) === today);
  const nowMin = new Date().getHours() * 60 + new Date().getMinutes();
  const startOf = (e: CalendarEvent) => (isTimed(e) ? Number(e.start_dt.slice(11, 13)) * 60 + Number(e.start_dt.slice(14, 16)) : -1);
  const next = [...todays].filter((e) => isTimed(e) && startOf(e) > nowMin).sort((a, b) => startOf(a) - startOf(b))[0];
  const weekMin = week.reduce((t, e) => t + bookedMinutes(e), 0);
  const todoOpen = todoList.items.filter((i) => !i.todo.done).length;
  const weekText = `${week.length} ${week.length === 1 ? "event" : "events"}, ${dur(weekMin)} booked`;
  const lead = weekQ.isError ? "The calendar could not be read." : !weekQ.data ? "Reading the calendar." : todays.length ? `${todays.length} ${todays.length === 1 ? "event" : "events"} today.` : "Nothing on the calendar today.";
  const more = weekQ.data ? (
    <>
      {next ? <>{"Next: "}<em className="run">{`${short(next.title)} at ${next.start_dt.slice(11, 16)}`}</em>{". "}</> : todays.length ? "Nothing more today. " : ""}
      {"The next 7 days hold "}<em className="run">{weekText}</em>{". "}
      {todoList.dayQ.isPending || todoList.lateQ.isPending ? "" : todoOpen > 0 ? <em className={todoList.lateCount > 0 ? "warn" : "run"}>{`${todoOpen} ${todoOpen === 1 ? "to-do" : "to-dos"} open${todoList.lateCount > 0 ? `, ${todoList.lateCount} late` : ""}.`}</em> : "The to-do list is empty."}
    </>
  ) : null;
  const [only, setOnly] = useState<string | null>(null);
  const spanText = view === "TODAY" ? "Today" : view === "DAY" ? viewTitle("DAY", anchor) : viewTitle(view, anchor);

  return (
    <div className={`v2-page${full ? " calfull" : ""}`} ref={pageRef} data-only={only ?? undefined}>
      {full && <Background app={app.id} />}
      <PageHeader
        crumb={app.name}
        title="Calendar"
        right={
          <div className="cctl">
            {line && (
              <span className="cst">
                {line.text}
                {line.retry && <button type="button" className="btn" onClick={retry}>Retry</button>}
                {line.settings && <Link className="btn" to="/app/settings">Settings</Link>}
              </span>
            )}
            <PhoneOff><AskClaude /></PhoneOff>
            <button type="button" className="btn" onClick={() => setCreating(true)}>New event</button>
          </div>
        }
      />
      {!full && <Say lead={lead} more={more} />}
      {!full && (
        <div className="cal-top">
          <TodoPanel today={today} />
          <div className="cal-side">
            <WeatherCard today={today} weather={weatherQ} forecast={forecastQ} />
            <KindCard events={events} span={spanText} only={only} onOnly={setOnly} />
          </div>
        </div>
      )}
      <div className="cal">
        <div className="nl-blk cmain">
          <div className="cmh">
            <div className="cmt">
              <h2>{viewTitle(view, anchor)}</h2>
              {view !== "TODAY" && (
                <span className="cmn">
                  <button type="button" className="btn" aria-label="Previous" onClick={() => setAnchor(stepAnchor(view, anchor, -1))}>&lsaquo;</button>
                  <button type="button" className="btn" onClick={() => setAnchor(today)}>Today</button>
                  <button type="button" className="btn" aria-label="Next" onClick={() => setAnchor(stepAnchor(view, anchor, 1))}>&rsaquo;</button>
                </span>
              )}
            </div>
            <div className="cmr">
              <Segmented options={phone ? PHONE_VIEWS : VIEWS} value={view} onChange={(v) => setView(v as View)} />
              <PhoneOff>
                <button type="button" className="btn" onClick={toggleFull}>{full ? "Exit full screen" : "Full screen"}</button>
              </PhoneOff>
            </div>
          </div>
          {view === "TODAY" ? (
            <Today onOpen={setOpen} todo={false} />
          ) : view === "MONTH" ? (
            <Month days={days} month={anchor} today={today} events={events} onOpenDay={openDay} onOpen={setOpen} />
          ) : (
            <Grid days={days} today={today} events={events} forecast={forecastQ.data} onOpen={setOpen}
              drag={drag} setDrag={setDrag} onError={setActionError} fit={full} />
          )}
          <PhoneOff>
            <Templates setDrag={setDrag} onError={setActionError} />
          </PhoneOff>
        </div>
      </div>
      {open && <EventDetail event={open} onClose={() => setOpen(null)} onError={setActionError} />}
      {creating && <NewEvent startDay={view === "DAY" ? anchor : today} onClose={() => setCreating(false)} />}
    </div>
  );
}

const dur = (m: number) => (m < 60 ? `${m} min` : m % 60 ? `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")}` : `${m / 60} h`);
// The calendar's titles often carry a code or a room after the name ("Gym - B12", "Call (Zoom)"); the sentence keeps the name.
const short = (t: string) => t.replace(/\s+-\s+[A-Z]?\d+.*$/, "").replace(/\s*\(.*\)\s*$/, "");
