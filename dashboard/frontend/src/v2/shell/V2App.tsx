import { useCallback, useEffect, useMemo, useState } from "react";
import { flushSync } from "react-dom";
import { Navigate, Route, Routes, matchPath, useLocation, useNavigate } from "react-router-dom";
import "../look.css";
import "./shell.css";
import { Background } from "../Background";
import { SettingsProvider, accentOf, pageKey, useSettings } from "../settings";
import Rail from "./Rail";
import TabBar from "./TabBar";
import { useLayoutMode } from "./layout";
import { LayoutContext, PhoneNav, type PhoneNavValue } from "../kit/phone";
import { PageNavContext, type PageNav } from "../kit/pageNav";
import Settings from "../pages/Settings";
import Palette from "./Palette";
import HoverTip from "./HoverTip";
import { useLiveFeed } from "./live";
import { APPS, findApp, findSection, pageFor } from "./sections";

function NotFound() {
  return <div className="v2-page nr-line">This page does not exist.</div>;
}

function SectionPage() {
  const loc = useLocation();
  const m = matchPath("/app/:app/:section", loc.pathname);
  const app = findApp(m?.params.app);
  const section = app ? findSection(app, m?.params.section) : undefined;
  if (!app || !section) return <NotFound />;
  const Page = pageFor(app, section);
  return <Page app={app} section={section} />;
}

// The section last open in each main, so up and down land where the viewer was. No expiry: it only
// holds a place in this tab, never anything typed.
const LAST_KEY = "v2-last-section";

function readLast(): Record<string, string> {
  try {
    const raw = sessionStorage.getItem(LAST_KEY);
    return raw ? (JSON.parse(raw) as Record<string, string>) : {};
  } catch (e) {
    console.error(`last section ${LAST_KEY}: ${e instanceof Error ? e.message : String(e)}`);
    return {};
  }
}

function writeLast(appId: string, sectionId: string) {
  try {
    sessionStorage.setItem(LAST_KEY, JSON.stringify({ ...readLast(), [appId]: sectionId }));
  } catch (e) {
    console.error(`last section ${LAST_KEY}: ${e instanceof Error ? e.message : String(e)}`);
  }
}

function Shell() {
  const { settings } = useSettings();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const [railOpen, setRailOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const live = useLiveFeed();
  const layout = useLayoutMode(settings.layout);
  const phone = layout === "phone";

  const m = matchPath("/app/:app/:section?", pathname);
  const seg = m?.params.app;
  const app = findApp(seg);
  const openSection = m?.params.section;
  const pinned = settings.rail === "open";
  const mini = !pinned && !railOpen;
  const page = pageKey(seg, openSection);
  const accent = accentOf(page, settings);

  // One way to change page: a slide when there is a direction (arrows, up and down) and motion is on and the browser has it.
  // Without a direction (rail, palette, tab bar) the page changes at once, no fade-out (Tamer 2026-11-16); the page's own parts rise.
  const go = useCallback(
    (to: string, dir?: "next" | "prev" | "up" | "down") => {
      if (dir && settings.motion === "on" && typeof document.startViewTransition === "function") {
        const root = document.documentElement;
        root.dataset.dir = dir;
        const t = document.startViewTransition(() => flushSync(() => navigate(to)));
        void t.finished.finally(() => { delete root.dataset.dir; });
      } else {
        navigate(to);
      }
    },
    [navigate, settings.motion],
  );

  const secs = app?.sections ?? [];
  const idx = secs.findIndex((s) => s.id === openSection);
  const pageNav = useMemo<PageNav | null>(() => {
    if (!app || secs.length < 2 || idx < 0) return null;
    const at = (i: number) => secs[(i + secs.length) % secs.length];
    const step = (d: -1 | 1) => go(`/app/${app.id}/${at(idx + d).id}`, d > 0 ? "next" : "prev");
    return { prev: { label: at(idx - 1).label }, next: { label: at(idx + 1).label }, step };
  }, [app, secs, idx, go]);

  const phoneNav = useMemo<PhoneNavValue | null>(
    () => (app ? { app: app.id, name: app.name, sections: app.sections, section: openSection } : null),
    [app, openSection],
  );

  const appId = app?.id;
  const sectionKnown = idx >= 0;
  useEffect(() => {
    if (appId && sectionKnown && openSection) writeLast(appId, openSection);
  }, [appId, sectionKnown, openSection]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((o) => !o);
        return;
      }
      if (e.key === "Escape" && !document.querySelector('[role="dialog"]')) {
        setRailOpen(false);
        return;
      }
      const horizontal = e.key === "ArrowLeft" || e.key === "ArrowRight";
      const vertical = e.key === "ArrowUp" || e.key === "ArrowDown";
      if (!horizontal && !vertical) return;
      if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
      if (e.target instanceof Element && e.target.closest("input, textarea, select, [contenteditable]")) return;
      if (document.querySelector('[role="dialog"]')) return;
      if (horizontal) {
        if (!pageNav) return;
        e.preventDefault();
        pageNav.step(e.key === "ArrowRight" ? 1 : -1);
        return;
      }
      if (!appId || !sectionKnown) return;
      e.preventDefault();
      const i = APPS.findIndex((a) => a.id === appId);
      const down = e.key === "ArrowDown";
      const to = APPS[(i + (down ? 1 : -1) + APPS.length) % APPS.length];
      const last = readLast()[to.id];
      const landing = to.sections.find((s) => s.id === last) ?? to.sections[0];
      go(`/app/${to.id}/${landing.id}`, down ? "down" : "up");
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [pageNav, appId, sectionKnown, go]);

  const cls = ["v2", settings.tone === "vivid" ? "vivid" : "", phone ? "phone" : "", pinned ? "pinned" : "", !mini ? "railopen" : "", settings.motion === "off" ? "still" : ""]
    .filter(Boolean)
    .join(" ");

  return (
    <div
      className={cls}
      data-page={page}
      data-bg={settings.bg}
      data-btn={settings.btn}
      data-aur={settings.aur}
      data-motion={settings.motion === "on" ? "full" : "calm"}
      style={{ ["--c" as string]: accent.hex, ["--b" as string]: accent.hex, ["--b-c" as string]: accent.rgb }}
    >
      <Background bg={settings.bg} />
      <HoverTip />
      {!phone && <Rail openApp={seg} openSection={openSection} mini={mini} pinned={pinned} onToggle={() => setRailOpen((o) => !o)} go={(to) => { setRailOpen(false); go(to); }} />}
      {phone && <TabBar openApp={seg} go={go} />}
      <main onClickCapture={() => { if (!pinned) setRailOpen(false); }}>
        <LayoutContext.Provider value={layout}>
        <PhoneNav.Provider value={phoneNav}>
        <PageNavContext.Provider value={pageNav}>
        <Routes>
          <Route path="/app" element={<Navigate to="/app/home/overview" replace />} />
          <Route path="/app/settings" element={<Settings />} />
          <Route path="/app/:app/:section" element={<SectionPage />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
        </PageNavContext.Provider>
        </PhoneNav.Provider>
        </LayoutContext.Provider>
      </main>
      {paletteOpen && <Palette live={live} go={go} onClose={() => setPaletteOpen(false)} />}
    </div>
  );
}

export default function V2App() {
  return (
    <SettingsProvider>
      <Shell />
    </SettingsProvider>
  );
}
