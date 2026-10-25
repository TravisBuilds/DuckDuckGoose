export interface HiggsfieldBalance {
  balance_usd_micros: number | null;
  balance_remaining_usd_micros?: number | null;
  set_at: string | null;
  set_by?: string | null;
  fresh: boolean;
  age_hours: number | null;
  max_age_hours: number | null;
  reason?: string | null;
}
export declare function formatUsdMicros(micros: number): string;
export declare function balanceDisplay(balance: HiggsfieldBalance | null | undefined): {
  text: string;
  tone: 'ok' | 'warn' | 'missing';
  liveBlocked: boolean;
};
export declare function parseBalanceInput(raw: unknown): string | null;
