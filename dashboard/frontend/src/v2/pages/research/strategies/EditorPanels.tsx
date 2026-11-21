import { useState } from "react";
import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Facts, Panel, State, StateWord, Table } from "../../../kit";
import {
  editorPanelsKeys, fetchEditorCatalogue, fetchEditorStatus, fetchEditorStrategy, fetchSizingMethods,
} from "../../../data/editorPanelsApi";
import type { CatalogueIndicator } from "../../../data/editorPanelsApi";
import { serverDetail } from "../../../../lib/errorText";
import "./editorPanels.css";

// Mockup: docs/design/rail_mockup_2026-10-10.html, editorPage (cat, MONEY, RISK AND EXITS and
// PARAMETERS cards, and the bar's QUICK TEST). Each panel shows what the server has; where it has
// nothing, a dash with a hover saying why.

type Insert = (text: string) => void;
type Row = { key: string; text: string; hover?: string; insert: string; lookAhead?: boolean };

const firstLine = (s: string) => s.split("\n")[0] ?? "";

function indicatorRow(i: CatalogueIndicator): Row {
  const defaults = Object.entries(i.parameters).map(([k, v]) => `${k}=${v}`).join(", ");
  return {
    key: `i:${i.name}`,
    text: i.name,
    hover: defaults ? `${i.description} Defaults: ${defaults}` : i.description,
    insert: `vbt.${i.name}.run(${i.inputs.join(", ")})`,
    lookAhead: i.look_ahead,
  };
}

export function CataloguePanel({ ready, onInsert }: { ready: boolean; onInsert: Insert }) {
  const [find, setFind] = useState("");
  const q = useQuery({ queryKey: editorPanelsKeys.catalogue, queryFn: fetchEditorCatalogue, staleTime: Infinity, refetchOnWindowFocus: false });
  const search = (
    <input
      className="fin ep-find"
      aria-label="Find in the catalogue"
      placeholder="Find"
      value={find}
      onChange={(e) => setFind(e.target.value)}
    />
  );
  if (q.isError) {
    return (
      <Panel title="Catalogue">
        {search}
        <p className="empty"><span title={serverDetail(q.error)}>-</span></p>
      </Panel>
    );
  }
  if (!q.data) {
    return <Panel title="Catalogue">{search}<p className="empty">Loading the catalogue.</p></Panel>;
  }
  const c = q.data;
  const groups: Array<[string, Row[]]> = [
    ["Indicators", c.vbt_indicators.indicators.map(indicatorRow)],
    ["Helpers", c.helpers.map((h) => ({
      key: `h:${h.name}`,
      text: h.name,
      hover: [h.signature ? `${h.name}${h.signature}` : h.name, h.doc ? firstLine(h.doc) : ""].filter(Boolean).join(" "),
      insert: h.kind === "function" ? `${h.name}()` : h.name,
    }))],
    ["Settings", c.settings.map((s) => ({
      key: `s:${s.scope}.${s.key}`,
      text: `${s.scope}.${s.key} ${String(s.value)}`,
      hover: s.value_type,
      insert: `get_setting(settings, "${s.scope}", "${s.key}")`,
    }))],
    ["Event types", c.event_types.map((t) => ({ key: `e:${t}`, text: t, insert: `"${t}"` }))],
  ];
  const needle = find.trim().toLowerCase();
  const shown = groups
    .map(([g, rows]) => [g, rows.filter((r) => r.text.toLowerCase().includes(needle))] as [string, Row[]])
    .filter(([, rows]) => rows.length > 0);
  return (
    <Panel title="Catalogue" note={<State tone="dim">{needle ? `${shown.reduce((n, [, r]) => n + r.length, 0)} of ${groups.reduce((n, [, r]) => n + r.length, 0)}` : `${groups.reduce((n, [, r]) => n + r.length, 0)} entries`}</State>}>
      {search}
      <div className="ep-list">
        {shown.length === 0 && <p className="empty"><StateWord word="NO MATCH" tone="none" /></p>}
        {shown.map(([g, rows]) => (
          <div key={g}>
            <div className="ep-group"><b className={`ep-g-${g.split(" ")[0].toLowerCase()}`}><i />{g}</b><small className="num">{rows.length}</small></div>
            {rows.map((r) => (
              <button
                key={r.key}
                type="button"
                data-code
                className="ep-row num"
                disabled={!ready}
                title={r.hover}
                onClick={() => onInsert(r.insert)}
              >
                {r.text}
                {r.lookAhead && <> <State tone="bad">Look-ahead</State></>}
              </button>
            ))}
          </div>
        ))}
      </div>
    </Panel>
  );
}

const MONEY_WORDS: Record<string, string> = {
  vol_target_atr: "Volatility target",
  fixed_fraction: "Fixed fraction",
  equal_slots: "Equal slots",
};
const MONEY_ABOUT: Record<string, string> = {
  vol_target_atr: "Sizes each trade so that hitting the stop loses the same share of the account.",
};
const MISSING: Array<[string, string]> = [
  ["fixed_fraction", "Not on the server's sizing list, so a strategy cannot pick it yet. Event strategies size this way from their settings."],
  ["equal_slots", "Not on the server's sizing list, so a strategy cannot pick it yet. Generator theories size this way."],
];
const moneyWord = (id: string) => MONEY_WORDS[id] ?? plainKey(id);
const plainKey = (k: string) => k.replace(/_/g, " ").replace(/^./, (m) => m.toUpperCase());
const percent = (v: unknown) => `${Number((Number(v) * 100).toFixed(2))}%`;

function specFacts(spec: Record<string, number | string>): Array<[string, string]> {
  return Object.entries(spec).map(([k, v]): [string, string] => {
    if (k === "risk_fraction") return ["Risk per trade", percent(v)];
    if (k === "stop_multiple") return ["Stop distance", `${v} × volatility`];
    if (k === "estimator") return ["Volatility from", v === "atr" ? "true range" : v === "ewma" ? "returns" : String(v)];
    if (k === "estimator_window") return ["Window", `${v} bars`];
    if (k === "hard_cap_fraction") return ["Cap", `${percent(v)} of equity`];
    return [plainKey(k), String(v)];
  });
}

export function MoneyPanel({ strategyId, version, ready, onMoney }: {
  strategyId: number | null; version: number | null; ready: boolean; onMoney: (id: string) => void;
}) {
  const methods = useQuery({ queryKey: editorPanelsKeys.sizingMethods, queryFn: fetchSizingMethods, staleTime: Infinity, refetchOnWindowFocus: false });
  const chosen = useQuery({
    queryKey: editorPanelsKeys.strategy(strategyId ?? 0, version),
    queryFn: () => fetchEditorStrategy(strategyId as number),
    enabled: strategyId !== null,
    refetchOnWindowFocus: false,
  });
  if (methods.isError) {
    return <Panel title="Money"><p className="empty"><span title={serverDetail(methods.error)}>-</span></p></Panel>;
  }
  if (!methods.data) return <Panel title="Money"><p className="empty">Loading the money methods.</p></Panel>;
  const list = methods.data.methods;
  const missing = MISSING.filter(([id]) => !list.some((m) => m.id === id));
  const current = strategyId === null ? null : chosen.data?.sizing_method ?? null;
  return (
    <Panel title="Money">
      <div className="ep-money">
        {list.map((m) => {
          const on = current === m.id;
          const cap = Number(m.spec.hard_cap_fraction);
          return (
            <button
              key={m.id}
              type="button"
              className={`ep-m${on ? " on" : ""}`}
              draggable
              disabled={!ready}
              title={m.description}
              onDragStart={(e) => e.dataTransfer.setData("application/x-sizing-method", m.id)}
              onClick={() => onMoney(m.id)}
            >
              <span className="ep-mh"><b>{moneyWord(m.id)}</b><State tone={on ? "good" : "dim"}>{on ? "In the code" : "Press to use"}</State></span>
              {MONEY_ABOUT[m.id] && <p>{MONEY_ABOUT[m.id]}</p>}
              <Facts items={specFacts(m.spec).filter(([k]) => k !== "Cap")} />
              {Number.isFinite(cap) && (
                <span className="ep-cap">
                  <span>Cap for one position</span>
                  <span className="ep-bar"><i style={{ width: `${Math.max(0, Math.min(100, cap * 100))}%` }} /></span>
                  <b className="num">{`${percent(cap)} of the account`}</b>
                </span>
              )}
            </button>
          );
        })}
        {missing.map(([id, why]) => (
          <div key={id} className="ep-gone ep-off" title={why}>
            <b>{moneyWord(id)}</b>
            <State tone="dim">Not on the server's list</State>
          </div>
        ))}
      </div>
    </Panel>
  );
}

const NO_EXITS = "No server list of exits yet, so a strategy cannot pick this here. ";
const RISK: Array<[string, string, string]> = [
  ["Volatility stop", "Today the stop distance is set by the volatility target money method.", "Money method"],
  ["Profit target", "Today it is part of the exit helper's code.", "Exit helper"],
  ["Trailing stop", "Today it is part of the exit helper's code, at the money method's stop distance.", "Exit helper"],
  ["Time limit", "Today it is part of the exit helper's code.", "Exit helper"],
  ["Triple barrier", "Today event strategies use it, from their settings in the catalogue.", "Event settings"],
  ["Computed best exit", "Today only Generator theories compute it, for a calibrated stop or target.", "Generator theories"],
];

export function RiskPanel() {
  return (
    <Panel title="Risk and exits" note={<State tone="dim" hover={NO_EXITS}>Cannot be picked yet</State>}>
      <div className="ep-money">
        {RISK.map(([w, why, where]) => (
          <div key={w} className="ep-gone ep-risk" title={NO_EXITS + why}>
            <span>{w}</span>
            <b className={`ep-where ${where.split(" ")[0].toLowerCase()}`}><i />{where}</b>
          </div>
        ))}
      </div>
    </Panel>
  );
}

export function ParametersPanel({ strategyId, version }: { strategyId: number | null; version: number | null }) {
  const q = useQuery({
    queryKey: editorPanelsKeys.status(strategyId ?? 0, version),
    queryFn: () => fetchEditorStatus(strategyId as number),
    enabled: strategyId !== null,
    refetchOnWindowFocus: false,
  });
  let body;
  let note: ReactNode;
  if (strategyId === null) {
    body = <p className="empty">Save once to read the parameters the code declares</p>;
  } else if (q.isError) {
    body = <p className="empty"><span title={serverDetail(q.error)}>-</span></p>;
  } else if (!q.data) {
    body = <p className="empty">Reading the parameters.</p>;
  } else if (q.data.sweep_params.length === 0) {
    note = <State tone="dim">{`Version ${q.data.db_version}`}</State>;
    body = <p className="empty">The code declares no parameters to sweep</p>;
  } else {
    note = <State tone="dim">{`Version ${q.data.db_version}`}</State>;
    body = (
      <Table
        head={["Name", "Type", "Sweep"]}
        rows={q.data.sweep_params.map((p) => [
          { v: <span data-code className="num">{p.name}</span> },
          p.type === "int" ? "whole number" : p.type === "float" ? "number" : {
            v: <span title={`The sweep takes int or float; this code declares ${p.type}`}><StateWord word="UNKNOWN TYPE" tone="rd" /></span>,
          },
          { v: <span title="The code declares a name and a type; the values to try are set in the sweep window">-</span> },
        ])}
      />
    );
  }
  return (
    <Panel title="Settings it declares" note={note}>
      <div className="ep-params">{body}</div>
    </Panel>
  );
}

export function QuickTestDash() {
  return (
    <span title="The server cannot run a quick test on one stock yet">
      <button type="button" className="btn q" disabled>Quick test</button>
    </span>
  );
}
