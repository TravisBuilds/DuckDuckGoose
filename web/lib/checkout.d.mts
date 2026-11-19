export interface Price {
  free: boolean;
  words: string;
  approve: number;
}

export interface PriceRow {
  label: string;
  value: string;
}

export interface VideoPrice extends Price {
  rows: PriceRow[];
}

export interface OfflineEstimate {
  total_usd_micros: number;
  per_shot?: Record<string, { stills_usd_micros?: number | null; clip_usd_micros?: number | null; pre_approved?: boolean }>;
  unpriced?: string[];
}

export type MoneyProblemKind = 'price_rose' | 'no_price' | 'balance_check' | 'balance' | 'budget';

export interface MoneyProblem {
  kind: MoneyProblemKind;
  text: string;
  action: string;
}

export function priceText(micros: number): string;
export function quote(estimate: { dry_run?: boolean | null; total_usd_micros: number } | null | undefined): Price | null;
export function headline(what: string, price: Price | null): string;
export function approvedRequest<T extends object>(body: T, estimate: { dry_run?: boolean | null; total_usd_micros: number } | null | undefined,
  field?: 'approved_total_usd_micros' | 'approved_usd_micros'): T & Record<string, number>;
export const PROMISE: string;
export const FREE_NOTE: string;
export function promise(price: Price | null, options?: { kept?: boolean }): string | null;
export function videoQuote(estimate: OfflineEstimate | null | undefined, options?: { free?: boolean }): VideoPrice | null;
export function clipsPrice(estimate: OfflineEstimate | null | undefined): string | null;
export function clipPrice(estimate: OfflineEstimate | null | undefined, shotId: string): string | null;
export function moneyProblem(message: unknown): MoneyProblem | null;
export function calmText(message: unknown): string;
export const LEDGER_WORDS: string[];
export function ledgerWords(text: string): string[];
