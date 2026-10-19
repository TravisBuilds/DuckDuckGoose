export interface LimitSource { url: string; checked: string }
export interface Limit { value: unknown; note?: string | null; basis?: string; sources?: LimitSource[] }
export interface ModelView { model_id: string; kind: string; status: string; limits: Record<string, Limit> }
export interface LimitRow { label: string; value: string; source: string | null; note: string | null }
export declare const LIMIT_LABELS: Record<string, string>;
export declare function formatLimitValue(name: string, value: unknown): string;
export declare function formatLimits(model: ModelView): LimitRow[];
export declare function summarizePreflight(
  shots: Record<string, { ok: boolean; errors: string[]; warnings: string[] }>
): { passed: number; failed: number; failedIds: string[] };
