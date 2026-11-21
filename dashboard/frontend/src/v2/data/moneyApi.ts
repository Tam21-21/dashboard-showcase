// The Money and Budget pages' own typed HTTP calls, typed after lane MY1's reads
// (dashboard/backend/money.py: overview, budget, targets; routes/money.py).
// Reads and the plan edits go to /api/money. The shared lib/api.ts exports only apiGet and apiPost,
// so the two plan edits that need PUT and DELETE use a small sender here that raises the same
// ApiError (the server's own words in .body.detail).
import { ApiError, apiGet } from "../../lib/api";

export interface MoneyAccount {
  account_id: string;
  name: string;
  account_type: string;
  currency: string;
  balance: number | null;
  value_eur: number;
  institution: string | null;
  /** Finary's connection status; null for an account with no connection. */
  sync_status: string | null;
  last_successful_sync_at: string | null;
  is_debt: boolean;
  share_of_net_worth: number | null;
}

export interface MoneyGoal {
  goal_id: string;
  name: string;
  target_amount: number | null;
  current_amount: number | null;
  currency: string | null;
  status: string | null;
  target_date: string | null;
}

export interface MoneyOverview {
  synced: boolean;
  read_at: string | null;
  stale: boolean | null;
  net_worth: number | null;
  gross_assets: number | null;
  liabilities: number | null;
  financial_assets: number | null;
  unvalued_liability_count: number | null;
  allocation: Array<{ category: string; amount: number }>;
  accounts: MoneyAccount[];
  debts: MoneyAccount[];
  savings_eur: number | null;
  series: Array<{ read_at: string; net_worth: number }>;
  goals: MoneyGoal[];
}

export interface BudgetCategory {
  slug: string;
  label: string | null;
  income: number;
  spent: number;
  target: number | null;
  pace: number | null;
  /** A fraction: 0.424 is 42.4 percent ahead of pace. */
  ahead: number | null;
  warn: boolean;
  /** The 12-month average spending, offered as a plan suggestion. */
  suggestion: number | null;
}

export interface BudgetMonthView {
  from: string;
  to: string;
  is_partial: boolean;
  elapsed_days: number;
  total_days: number;
  income: number;
  expenses: number;
  net_cashflow: number;
  savings_rate_percent: number | null;
  categories: BudgetCategory[];
}

export interface BudgetMonth {
  month: string;
  income: number;
  expenses: number;
  net_cashflow: number;
  complete: boolean;
}

export interface LastMonthCategory {
  slug: string;
  label: string | null;
  spent: number;
  income: number;
}

export interface MoneyBudget {
  synced: boolean;
  read_at: string | null;
  stale: boolean | null;
  budget: BudgetMonthView | null;
  /** Why there is no budget for the month, in words. */
  reason: string | null;
  savings_eur: number | null;
  cushion_months: number | null;
  months: BudgetMonth[];
  last_month: {
    from: string;
    to: string;
    income: number;
    expenses: number;
    net_cashflow: number;
    categories: LastMonthCategory[];
  } | null;
  recurring: { monthly_equivalent: number; count: number } | null;
}

export function getOverview(): Promise<MoneyOverview> {
  return apiGet<MoneyOverview>("/api/money/overview");
}

export function getBudget(): Promise<MoneyBudget> {
  return apiGet<MoneyBudget>("/api/money/budget");
}

async function send<T>(method: "PUT" | "DELETE" | "POST", url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method,
    credentials: "same-origin",
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const parsed: unknown = await res.json().then(
      (j: unknown) => j,
      () => undefined,
    );
    const detail =
      parsed && typeof parsed === "object" && "detail" in parsed && typeof parsed.detail === "string" ? `: ${parsed.detail}` : "";
    throw new ApiError(`${res.status} ${res.statusText}${detail}`, res.status, parsed, url);
  }
  return res.json() as Promise<T>;
}

/** Ask the server to start a Finary read now (lane FR1a). The answer names the run to open if it stalls. */
export function postRefresh(): Promise<{ asked_at: string; run_url: string }> {
  return send("POST", "/api/money/refresh");
}

/** Set one category's monthly plan, in euros. */
export function putTarget(slug: string, amount: number): Promise<{ slug: string; amount_eur: number }> {
  return send("PUT", `/api/money/targets/${encodeURIComponent(slug)}`, { amount_eur: amount });
}

/** Remove one category's monthly plan. */
export function deleteTarget(slug: string): Promise<{ deleted: string }> {
  return send("DELETE", `/api/money/targets/${encodeURIComponent(slug)}`);
}
