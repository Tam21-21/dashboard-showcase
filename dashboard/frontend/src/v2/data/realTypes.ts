// The Capital page's own types, matching dashboard/backend/routes/real.py's JSON answers
// (get_account_route, get_readiness_route, get_limits_route, put_limits_route, get_kill_state_route,
// post_kill_arm). Field names are copied from that file, never guessed.

// GET /api/real/account: the configured real account id, or null when none is set.
export type RealAccount = { account_id: string | null };

// One checklist item. "item" is one of the five names below; "state" is the word the store gave
// and "evidence" is the route's plain sentence for it.
export type ReadinessItemName = "go" | "kill_switch" | "limits" | "broker" | "approved_strategy";
export type ReadinessItem = { item: ReadinessItemName; state: string; evidence: string };

// GET /api/real/readiness: five items in order, and how many are in their good state.
export type Readiness = { account_id: string | null; items: ReadinessItem[]; ready_count: number; approved_count: number };

// One of the nine real-money limits, "value" null while it is unset.
export type RealLimit = { key: string; value: number | null; unit: string; meaning: string };

// GET /api/real/limits.
export type RealLimitsResponse = { limits: RealLimit[] };

// PUT /api/real/limits: the nine rows again, whether the account is still enabled, and the ids of
// the enabling records this change superseded.
export type PutLimitsResponse = {
  limits: RealLimit[];
  enabled: boolean | null;
  enabled_reason: string | null;
  superseded_enabling_record_ids: number[];
};

// GET /api/real/kill/state?account_id=
export type KillState = {
  account_id: string;
  state: string;
  commanded_action: string | null;
  last_verified_at: string | null;
  last_verified_outcome: string | null;
  drill_state: string;
};

export type ArmAction = "halt" | "flatten";
export type ArmRequest = { account_id: string; action: ArmAction };

// POST /api/real/kill/arm
export type ArmResponse = { account_id: string; state: string; commanded_action: string | null };

// POST /api/real/kill/verify and /api/real/kill/disarm: the request and the answer.
export type AccountRequest = { account_id: string };
export type KillActionResponse = { account_id: string; state: string };
