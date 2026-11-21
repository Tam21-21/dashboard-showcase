import { useState } from "react";
import { PageHeader, Segmented, usePageMemory } from "../../kit";
import type { PageProps } from "../../shell/sections";
import DatasetsTab from "./datasets/DatasetsTab";
import EventsTab from "./events/EventsTab";
import FundamentalsTab from "./fundamentals/FundamentalsTab";
import MarketsTab from "./markets/MarketsTab";
import UniversesTab from "./universes/UniversesTab";
import CompanyFind from "./lookup/CompanyFind";
import LookupWindow from "./lookup/LookupWindow";
import type { Company } from "./lookup/names";
import "./markets.css";

// The Data library: a tab row and the tab chosen (mockup: the frozen mockup of 2026-11-15, K.libTabs). The title row holds
// the chosen tab's own buttons (a tab draws them into the slot on the right); the tab row holds the company lookup on the
// Markets tab (a pick opens its window over the page) and a search box on the Datasets tab. The Markets tab is display data (see its file); the Datasets tab lists the price series the research can use.

const TABS = [
  { label: "Markets", value: "markets" },
  { label: "Datasets", value: "datasets" },
  { label: "Universes", value: "universes" },
  { label: "Events", value: "events" },
  { label: "Fundamentals", value: "fundamentals" },
];

const LOOKUP_LABEL = "Look up a company or ticker";
const DATASET_SEARCH = "Find any dataset by name";

export default function Markets({ app, section }: PageProps) {
  const [tab, setTab] = usePageMemory<string>("research/data-library/tab", "markets");
  const [slot, setSlot] = useState<HTMLElement | null>(null);
  const [typed, setTyped] = useState("");
  const [asked, setAsked] = useState("");
  const [lookup, setLookup, clearLookup] = usePageMemory<Company | null>("research/markets/lookup", null);

  function pick(next: string) {
    setTab(next);
    setTyped("");
    setAsked("");
  }

  return (
    <div className="v2-page mk">
      <PageHeader crumb={app.name} title={section.label} right={<div className="mk-head" ref={setSlot} />} />

      <div className="mk-tabs">
        <Segmented variant="seg" options={TABS} value={tab} onChange={pick} />
        {tab === "markets" && <CompanyFind label={LOOKUP_LABEL} onPick={setLookup} />}
        {tab === "datasets" && (
          <input
            className="mk-find" type="search" aria-label={DATASET_SEARCH} placeholder={DATASET_SEARCH} value={typed}
            onChange={(e) => { setTyped(e.target.value); setAsked(e.target.value); }}
          />
        )}
      </div>

      {tab === "datasets" ? <DatasetsTab slot={slot} find={asked} onClear={() => { setTyped(""); setAsked(""); }} />
        : tab === "universes" ? <UniversesTab slot={slot} />
        : tab === "events" ? <EventsTab slot={slot} />
        : tab === "fundamentals" ? <FundamentalsTab slot={slot} />
        : <MarketsTab />}

      {lookup && <LookupWindow key={lookup.symbol} company={lookup} onClose={clearLookup} />}
    </div>
  );
}
