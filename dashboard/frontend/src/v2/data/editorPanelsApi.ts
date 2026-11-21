// The strategy editor's side panels read four things from the server, each once on open: the
// catalogue, the sizing methods, the strategy's chosen sizing method and the code's declared
// sweep parameters. Over the shared fetch helper in lib/api.ts.
import { apiGet } from "../../lib/api";

export const editorPanelsKeys = {
  catalogue: ["editor", "catalogue"] as const,
  sizingMethods: ["editor", "sizing-methods"] as const,
  strategy: (id: number, version: number | null) => ["editor", "strategy", id, version] as const,
  status: (id: number, version: number | null) => ["editor", "status", id, version] as const,
};

export type CatalogueHelper = { name: string; kind: string; signature: string | null; doc: string | null };
export type CatalogueSetting = { scope: string; key: string; value: unknown; value_type: string };
export type CatalogueIndicator = {
  name: string;
  description: string;
  inputs: string[];
  parameters: Record<string, string>;
  look_ahead: boolean;
};
export type EditorCatalogue = {
  helpers: CatalogueHelper[];
  settings: CatalogueSetting[];
  event_types: string[];
  vbt_indicators: { indicators: CatalogueIndicator[] };
};
export type SizingMethod = { id: string; description: string; spec: Record<string, number | string> };
export type EditorStatus = {
  db_version: number;
  sweep_params: Array<{ name: string; type: string }>;
  param_mode: string;
};

export const fetchEditorCatalogue = () => apiGet<EditorCatalogue>("/api/research/strategies/catalog");
export const fetchSizingMethods = () => apiGet<{ methods: SizingMethod[] }>("/api/research/sizing-methods");
export const fetchEditorStrategy = (id: number) =>
  apiGet<{ sizing_method: string | null }>(`/api/research/strategies/${id}`);
export const fetchEditorStatus = (id: number) => apiGet<EditorStatus>(`/api/research/strategies/${id}/status`);
