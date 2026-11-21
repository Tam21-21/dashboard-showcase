// The Universes tab's one colour key: the SEC sector office (EDGAR's ownerOrg label, DECISIONS #453). Colours and
// display words are the mockup's own list (docs/design/rail_mockup_2026-10-10.html, DLD.ORGS). Every sector colour
// on the tab comes from here.

export const NO_SECTOR_KEY = "none";

export type SectorInfo = { edgar: string | null; label: string; colour: string };

export const SECTORS: SectorInfo[] = [
  { edgar: "Technology", label: "Technology", colour: "#29c6ff" },
  { edgar: "Finance", label: "Finance", colour: "#a78bfa" },
  { edgar: "Manufacturing", label: "Manufacturing", colour: "#ff9a3d" },
  { edgar: "Life Sciences", label: "Life sciences", colour: "#ff8fd0" },
  { edgar: "Energy & Transportation", label: "Energy and transport", colour: "#e8d49a" },
  { edgar: "Real Estate & Construction", label: "Real estate", colour: "#c9a27a" },
  { edgar: "Trade & Services", label: "Trade and services", colour: "#7fd9a8" },
  { edgar: "Industrial Applications and Services", label: "Industrial services", colour: "#8fa3ff" },
  { edgar: "Crypto Assets", label: "Crypto assets", colour: "#f6c445" },
  { edgar: null, label: "Not on file", colour: "#5b6472" },
];

const NOT_ON_FILE = SECTORS[SECTORS.length - 1];

// A label outside the nine is shown in its own words with the not-on-file colour; the server only sends the nine.
export function sectorInfo(edgar: string | null | undefined): SectorInfo {
  if (edgar == null) return NOT_ON_FILE;
  return SECTORS.find((s) => s.edgar === edgar) ?? { edgar, label: edgar, colour: NOT_ON_FILE.colour };
}

export const sectorKey = (edgar: string | null | undefined): string => edgar ?? NO_SECTOR_KEY;

export type SectorCount = { edgar: string | null; key: string; count: number };

// The same order everywhere: most first, then the EDGAR label, "not on file" last.
export function orderSectors(rows: SectorCount[]): SectorCount[] {
  return [...rows].sort(
    (a, b) => (a.edgar === null ? 1 : 0) - (b.edgar === null ? 1 : 0) || b.count - a.count || (a.edgar ?? "").localeCompare(b.edgar ?? ""),
  );
}

// The members counted by sector, in that order.
export function bySector(members: Array<{ sector?: string | null }>): SectorCount[] {
  const counts = new Map<string | null, number>();
  for (const m of members) {
    const s = m.sector ?? null;
    counts.set(s, (counts.get(s) ?? 0) + 1);
  }
  return orderSectors([...counts.entries()].map(([edgar, count]) => ({ edgar, key: sectorKey(edgar), count })));
}
