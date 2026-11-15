export { formatUsdMicros } from './balance.mjs';

export interface CostGroup {
  jobs: number;
  spend_usd_micros: number;
  actual_usd_micros: number;
  jobs_with_actual: number;
  coverage_pct: number | null;
  estimate_usd_micros: number;
  jobs_with_estimate: number;
  compared_jobs: number;
  compared_estimate_usd_micros: number;
  compared_actual_usd_micros: number;
  delta_usd_micros: number | null;
  delta_pct: number | null;
  backfilled_usd_micros: number;
}

export interface BalanceReading {
  read_at: string;
  read_for: string;
  source: string;
  balance_usd_micros: number | null;
  balance_usd: string | null;
  details: Record<string, unknown>;
  error: string | null;
}

export interface AlertStatus {
  configured: boolean;
  channels: string[];
  email: { configured: boolean; host: string | null; port: number; to: string[]; tls: string } | null;
  webhook: { configured: boolean; host: string | null } | null;
  dedupe_window_seconds: number;
  warnings: string[];
}

export interface RecordedAlert {
  id: number;
  alert_type: string;
  severity: string;
  subject: string;
  message: string;
  sent_at: string;
  channels: string[];
  delivery: Record<string, string>;
  delivered: boolean | null;
  is_test: boolean;
}

export interface SendResult {
  suppressed: boolean;
  delivered: boolean;
  channels: string[];
  results: Record<string, string>;
}

export interface PerFinished {
  finished_episodes: number;
  finished_with_duration: number;
  finished_minutes: number;
  finished_spend_usd_micros: number;
  finished_coverage_pct: number | null;
  per_episode_usd_micros: number | null;
  per_minute_usd_micros: number | null;
  all_spend_per_finished_episode_usd_micros: number | null;
  definition: string;
}

export interface MonthlyProjection {
  month: string;
  days_elapsed: number;
  days_in_month: number;
  spend_so_far_usd_micros: number;
  actual_so_far_usd_micros: number;
  coverage_pct: number | null;
  daily_rate_usd_micros: number;
  projected_month_end_usd_micros: number;
  basis: string;
}

export interface CostWarning {
  level: 'error' | 'warning' | 'info';
  code: string;
  message: string;
}

export interface ExportHeadline {
  as_of: string;
  spend_usd_micros: number;
  provider_reported_usd_micros: number;
  coverage_pct: number | null;
  in_flight_jobs: number;
  in_flight_held_usd_micros: number;
  month: string;
  month_spend_so_far_usd_micros: number;
  projected_month_end_usd_micros: number;
  projection_basis: string;
}

export interface ExportCostTruth {
  generated_at: string;
  totals: CostGroup;
  in_flight: { jobs: number; held_usd_micros: number };
  monthly: MonthlyProjection;
  by_person: Array<CostGroup & { name: string; email: string | null; episodes: number; attribution: string[] }>;
}

export interface DashboardExport {
  cost_truth: ExportCostTruth | null;
  users: Array<{
    user_id: string;
    name: string;
    email: string | null;
    episode_count: number;
    job_count: number;
    revision_percent: number;
  }>;
}

export declare function pctText(pct: number | null | undefined): string;
export declare function deltaText(group: Partial<CostGroup> | null | undefined): string;
export declare function coverageSummary(totals: Partial<CostGroup> | null | undefined): {
  tone: 'none' | 'partial' | 'full';
  text: string;
};
export declare function attributionText(source: string | null | undefined): string;
export declare function stageLabel(stage: string | null | undefined): string;
export declare function ageText(iso: string, now?: number): string;
export declare function readingLine(reading: BalanceReading | null | undefined, now?: number): {
  tone: 'ok' | 'missing';
  text: string;
  when: string;
};
export declare function destinationText(status: AlertStatus | null | undefined): string;
export declare function deliveryText(alert: Pick<RecordedAlert, 'channels' | 'delivery'> | null | undefined): string;
export declare function testAlertText(result: SendResult | null | undefined): string;
export declare function perFinishedText(pf: PerFinished | null | undefined): {
  episode: string;
  minute: string;
  note: string;
};
export declare function projectionText(monthly: MonthlyProjection | null | undefined): {
  amount: string;
  note: string;
};
export declare function exportHeadline(truth: ExportCostTruth | null | undefined): ExportHeadline | null;
export declare function dashboardCsv(data: DashboardExport | null | undefined): string;
export declare function dashboardJson(data: DashboardExport | null | undefined): string;
export declare function sortedWarnings(list: CostWarning[] | null | undefined): CostWarning[];
