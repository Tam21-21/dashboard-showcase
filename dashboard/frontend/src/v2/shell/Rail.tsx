import { Fragment, useLayoutEffect, useRef } from "react";
import { APPS } from "./sections";
import { Icon } from "./icons";
import { GROUP } from "./groups";

interface Props {
  openApp: string | undefined;
  openSection: string | undefined;
  mini: boolean;
  pinned: boolean;
  onToggle: () => void;
  go: (to: string) => void;
}

// The logo at the top of the rail (mockup rule 28, Tamer 2026-11-15): the Reactor in fire and ice, the bare mark.
// Its gradient ids carry a prefix so no other svg on a page can take them.
function Logo() {
  const arcs: Array<[string, string, string, string, string]> = [
    ["M40.78,32.92 A19,19 0 0 1 30.15,41.98", "40.78", "32.92", "30.15", "41.98"],
    ["M27.24,42.72 A19,19 0 0 1 13.57,39.88", "27.24", "42.72", "13.57", "39.88"],
    ["M11.20,38.04 A19,19 0 0 1 5.06,25.50", "11.20", "38.04", "5.06", "25.50"],
    ["M5.06,22.51 A19,19 0 0 1 11.20,9.96", "5.06", "22.51", "11.20", "9.96"],
    ["M13.56,8.12 A19,19 0 0 1 27.23,5.28", "13.56", "8.12", "27.23", "5.28"],
    ["M30.14,6.02 A19,19 0 0 1 40.77,15.07", "30.14", "6.02", "40.77", "15.07"],
  ];
  const stops = ["#27b4e8", "#28a0e7", "#2a8ce5", "#2b78e4", "#2b6add", "#2a5dd7", "#2a4fd0"];
  return (
    <svg className="logo" viewBox="0 0 48 48" aria-hidden="true">
      <defs>
        <linearGradient id="v2logo-t" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#f7a51f" />
          <stop offset="1" stopColor="#e85d0f" />
        </linearGradient>
        {arcs.map(([, x1, y1, x2, y2], i) => (
          <linearGradient key={i} id={`v2logo-e${i}`} gradientUnits="userSpaceOnUse" x1={x1} y1={y1} x2={x2} y2={y2}>
            <stop offset="0" stopColor={stops[i]} />
            <stop offset="1" stopColor={stops[i + 1]} />
          </linearGradient>
        ))}
      </defs>
      <g fill="none">
        {arcs.map(([d], i) => <path key={i} d={d} stroke={`url(#v2logo-e${i})`} strokeWidth="4.2" />)}
        <circle cx="24" cy="24" r="12.6" stroke="#2b78e4" strokeWidth="1.2" opacity="0.6" />
      </g>
      <g fill="url(#v2logo-t)">
        <rect x="15.4" y="16.6" width="17.2" height="3.6" />
        <rect x="22.2" y="16.6" width="3.6" height="15.6" />
      </g>
    </svg>
  );
}

// The open group's pages with one highlight (and its white bar) that slides by a CSS transition to the open page's button
// (the mockup's `.rail .mark`, Tamer 2026-11-16). A group's first placement, or one from hidden, has class now: no slide.
function Secs({ app, openSection, mini, go }: { app: (typeof APPS)[number]; openSection: string | undefined; mini: boolean; go: (to: string) => void }) {
  const box = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const place = (now: boolean) => {
      const mark = box.current?.querySelector<HTMLElement>(":scope > .mark");
      if (!mark) return;
      const on = box.current?.querySelector<HTMLElement>(".sec.on");
      if (!on) { mark.style.opacity = "0"; return; }
      const wasShown = mark.style.opacity === "1";
      mark.style.transform = `translate(${on.offsetLeft}px, ${on.offsetTop}px)`;
      mark.style.width = `${on.offsetWidth}px`;
      mark.style.height = `${on.offsetHeight}px`;
      mark.style.opacity = "1";
      mark.classList.toggle("now", now || !wasShown);
    };
    place(false);
    const onResize = () => place(true);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [openSection, mini]);
  return (
    <div className="secs" ref={box}>
      <span className="mark" aria-hidden="true" />
      {app.sections.map((s) => (
        <Fragment key={s.id}>
          {GROUP[s.id] && <span className="rsec" aria-hidden="true"><span className="sname">{GROUP[s.id]}</span></span>}
          <button
            type="button"
            className={`sec${s.id === openSection ? " on" : ""}`}
            aria-label={s.label}
            title={mini ? s.label : undefined}
            onClick={() => go(`/app/${app.id}/${s.id}`)}
          >
            <Icon name={s.icon} />
            <span className="sname">{s.label}</span>
          </button>
        </Fragment>
      ))}
    </div>
  );
}

// Closed by default; only its toggle opens it, as in the mockup. The shell folds it again on a page change, Escape or a
// press on the page. The sheet behind the names is its own element beside the rail, as in the mockup: drawn inside the
// rail, its blur saw only the rail and the page showed through sharp behind the names (Tamer's screenshots 2026-11-15).
export default function Rail({ openApp, openSection, mini, pinned, onToggle, go }: Props) {
  const label = mini ? "Open the side bar" : "Close the side bar";
  return (
    <>
    <div className={`rail-sheet${mini ? "" : " on"}`} aria-hidden="true" />
    <nav className={`rail${mini ? " mini" : ""}`} aria-label="Apps">
      <div className="rtop" aria-hidden="true"><Logo /></div>
      {!pinned && (
        <button type="button" className="tog" title={mini ? label : undefined} aria-label={label} onClick={onToggle}>
          <Icon name="panel" />
          <span className="appname">Close</span>
        </button>
      )}
      {APPS.map((a) => {
        const open = a.id === openApp;
        return (
          <div key={a.id} className="appwrap">
            <button
              type="button"
              className={`app${open ? " open" : ""}`}
              title={mini ? a.name : undefined}
              aria-label={a.name}
              onClick={() => go(`/app/${a.id}/${a.sections[0].id}`)}
            >
              <Icon name={a.icon} />
              <span className="appname">{a.name}</span>
            </button>
            {open && a.sections.length > 1 && (
              <Secs app={a} openSection={openSection} mini={mini} go={go} />
            )}
          </div>
        );
      })}
      <div className="bottom">
        <button
          type="button"
          className={`app${openApp === "settings" ? " open" : ""}`}
          title={mini ? "Settings" : undefined}
          aria-label="Settings"
          onClick={() => go("/app/settings")}
        >
          <Icon name="gear" />
          <span className="appname">Settings</span>
        </button>
      </div>
    </nav>
    </>
  );
}
