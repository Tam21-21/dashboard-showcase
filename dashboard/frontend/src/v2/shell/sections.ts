import type { ComponentType } from "react";
import type { AppId } from "../settings";
import type { IconName } from "./icons";
import SectionStub from "../pages/SectionStub";
import Markets from "../pages/research/Markets";
import Runs from "../pages/research/Runs";
import Chains from "../pages/research/Chains";
import Judge from "../pages/research/Judge";
import Strategies from "../pages/research/Strategies";
import Generator from "../pages/research/generator/Generator";
import Overview from "../pages/research/Overview";
import Operations from "../pages/research/Operations";
import ForwardTesting from "../pages/research/ForwardTesting";
import Paper from "../pages/trading/Paper";
import Capital from "../pages/trading/Capital";
import Calendar from "../pages/calendar/Calendar";
import Home from "../pages/home/Home";
import Sports from "../pages/home/Sports";
import Positions from "../pages/portfolio/Positions";
import Money from "../pages/portfolio/Money";
import Budget from "../pages/portfolio/Budget";

// The apps and their sections, as in docs/design/rail_mockup_2026-10-10.html (APPS).

export interface SectionDef {
  id: string;
  label: string;
  icon: IconName;
}
export interface AppDef {
  id: AppId;
  name: string;
  icon: IconName;
  sections: readonly SectionDef[];
}
export interface PageProps {
  app: AppDef;
  section: SectionDef;
}

export const APPS: readonly AppDef[] = [
  {
    id: "home", name: "Home", icon: "home",
    sections: [
      { id: "overview", label: "Overview", icon: "overview" },
      { id: "sports", label: "Sports", icon: "run" },
    ],
  },
  {
    id: "research", name: "Research", icon: "lab",
    sections: [
      { id: "overview", label: "Overview", icon: "overview" },
      { id: "data-library", label: "Data library", icon: "data" },
      { id: "strategies", label: "Strategies", icon: "strat" },
      { id: "generator", label: "Generator", icon: "gen" },
      { id: "runs", label: "Runs", icon: "runs" },
      { id: "chains", label: "Chain", icon: "chains" },
      { id: "judge", label: "Judge", icon: "judge" },
      { id: "forward-testing", label: "Forward testing", icon: "fwd" },
      { id: "operations", label: "Operations", icon: "ops" },
    ],
  },
  {
    id: "calendar", name: "Calendar", icon: "cal",
    sections: [{ id: "calendar", label: "Calendar", icon: "cal" }],
  },
  {
    id: "trading", name: "Trading", icon: "trade",
    sections: [
      { id: "paper", label: "Paper", icon: "paper" },
      { id: "capital", label: "Capital", icon: "shield" },
    ],
  },
  {
    id: "portfolio", name: "Portfolio", icon: "wallet",
    sections: [
      { id: "positions", label: "Positions", icon: "pie" },
      { id: "money", label: "Money", icon: "coins" },
      { id: "budget", label: "Budget", icon: "plan" },
    ],
  },
];

// A section with its own page names it here, as "app/section"; the rest show NotRebuilt.
const PAGES: Record<string, ComponentType<PageProps>> = {
  "home/overview": Home,
  "home/sports": Sports,
  "research/overview": Overview,
  "research/data-library": Markets,
  "research/strategies": Strategies,
  "research/generator": Generator,
  "research/runs": Runs,
  "research/chains": Chains,
  "research/judge": Judge,
  "research/forward-testing": ForwardTesting,
  "research/operations": Operations,
  "calendar/calendar": Calendar,
  "trading/paper": Paper,
  "trading/capital": Capital,
  "portfolio/positions": Positions,
  "portfolio/money": Money,
  "portfolio/budget": Budget,
};

export function pageFor(app: AppDef, section: SectionDef): ComponentType<PageProps> {
  return PAGES[`${app.id}/${section.id}`] ?? SectionStub;
}

export function findApp(id: string | undefined): AppDef | undefined {
  return APPS.find((a) => a.id === id);
}

export function findSection(app: AppDef, id: string | undefined): SectionDef | undefined {
  return app.sections.find((s) => s.id === id);
}
