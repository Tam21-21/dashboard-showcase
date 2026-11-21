import { useCallback, useState } from "react";
import { Block, PageHeader } from "../../kit";
import type { PageProps } from "../../shell/sections";
import type { GarminActivity } from "../../../types";
import { NOT_CONNECTED, useGarminActivityList, useGarminDays, whyNot } from "./data";
import { ActivityWindow } from "./sports/activity";
import { localIso } from "./sports/derive";
import { buildModel } from "./sports/model";
import { SportsView } from "./sports/View";
import "./sports.css";

// Read-only. Every figure is a Garmin field, a sum, a mean or an average the page works out from the cached rows and sessions
// (sports/model.ts); where there is none, a dash with its reason. The days route serves at most 90 days, so the fitness line
// starts from them. No market or economy figure is drawn here.

const NOT_SET_UP = "Garmin is not set up on the server";
const NO_DAY = "No Garmin day synced yet";
const DAYS = 90;
const ACTIVITIES = 300;

export default function Sports({ app, section }: PageProps) {
  const daysQ = useGarminDays(DAYS);
  const actQ = useGarminActivityList(ACTIVITIES);
  const [open, setOpen] = useState<GarminActivity | null>(null);
  const close = useCallback(() => setOpen(null), []);

  const daysWhy = whyNot(daysQ) ?? (daysQ.data && !daysQ.data.configured ? NOT_CONNECTED : null);
  const actWhy = whyNot(actQ) ?? (actQ.data && !actQ.data.configured ? NOT_CONNECTED : null);
  const today = localIso(new Date());
  const rows = daysWhy === null && daysQ.data ? daysQ.data.snapshots : null;
  const acts = actWhy === null && actQ.data ? actQ.data.activities : null;

  const why = daysWhy === NOT_CONNECTED && actWhy === NOT_CONNECTED ? NOT_SET_UP : daysWhy ?? actWhy ?? (rows !== null && rows.length === 0 && acts !== null && acts.length === 0 ? NO_DAY : null);
  const model = why === null && rows !== null && acts !== null ? buildModel(rows, acts, today) : null;

  return (
    <div className="v2-page spt">
      <PageHeader crumb={app.name} title={section.label} />
      {model ? <SportsView m={model} onOpen={setOpen} /> : <Block title="Sports"><p className="empty" title={why ?? undefined}>{why ?? "Loading"}</p></Block>}
      {open && <ActivityWindow activity={open} onClose={close} />}
    </div>
  );
}
