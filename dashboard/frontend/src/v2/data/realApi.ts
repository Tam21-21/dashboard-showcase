// The Capital page's own typed HTTP calls over the shared fetch helpers. The writes are the
// limits (PUT) and the kill switch arm, verify and disarm (POST); nothing here places, cancels or
// closes an order except through the server's own kill routes, which refuse while no broker is wired.
import { apiGet, apiPost, apiPut } from "../../lib/api";
import type {
  AccountRequest,
  ArmRequest,
  ArmResponse,
  KillActionResponse,
  KillState,
  PutLimitsResponse,
  RealAccount,
  RealLimitsResponse,
  Readiness,
} from "./realTypes";

// GET /api/real/account: the one configured real account id, or null.
export function getRealAccount(): Promise<RealAccount> {
  return apiGet<RealAccount>("/api/real/account");
}

// GET /api/real/readiness: the five-item checklist, read only.
export function getReadiness(): Promise<Readiness> {
  return apiGet<Readiness>("/api/real/readiness");
}

// GET /api/real/limits: the nine limits as stored.
export function getRealLimits(): Promise<RealLimitsResponse> {
  return apiGet<RealLimitsResponse>("/api/real/limits");
}

// PUT /api/real/limits: any subset of the nine, keyed by the full stored key.
export function putRealLimits(values: Record<string, number>): Promise<PutLimitsResponse> {
  return apiPut<PutLimitsResponse>("/api/real/limits", values);
}

// GET /api/real/kill/state?account_id=
export function getKillState(accountId: string): Promise<KillState> {
  return apiGet<KillState>(`/api/real/kill/state?account_id=${encodeURIComponent(accountId)}`);
}

// POST /api/real/kill/arm: halt or flatten. 503 while no broker is wired.
export function armKill(body: ArmRequest): Promise<ArmResponse> {
  return apiPost<ArmResponse>("/api/real/kill/arm", body);
}

// POST /api/real/kill/verify: ask the server to check that the armed switch really works. 503
// while no broker is wired.
export function verifyKill(body: AccountRequest): Promise<KillActionResponse> {
  return apiPost<KillActionResponse>("/api/real/kill/verify", body);
}

// POST /api/real/kill/disarm: only from armed-verified-effective; the server refuses otherwise.
export function disarmKill(body: AccountRequest): Promise<KillActionResponse> {
  return apiPost<KillActionResponse>("/api/real/kill/disarm", body);
}
