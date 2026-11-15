/**
 * Display helpers for the master dashboard's cost truth (docs/ACTUAL-COSTS.md).
 *
 * The backend sends integer usd_micros; "actual" is what a provider reported it charged, "estimate"
 * what the dollar guard reserved. Spend is the actual where one is recorded, the estimate otherwise.
 * "Actual vs estimate" only compares jobs that have both numbers.
 */
import { formatUsdMicros } from './balance.mjs';

export { formatUsdMicros };

/** "81.8%", or "—" when there is nothing to divide. */
export function pctText(pct) {
  return typeof pct === 'number' && Number.isFinite(pct) ? `${pct.toFixed(1)}%` : '—';
}

/** Provider-reported minus estimated over the jobs that have both numbers. */
export function deltaText(group) {
  if (!group || !group.compared_jobs || typeof group.delta_usd_micros !== 'number') {
    return 'no job has both numbers yet';
  }
  const d = group.delta_usd_micros;
  const sign = d > 0 ? '+' : d < 0 ? '-' : '±';
  const pct = typeof group.delta_pct === 'number' ? ` (${sign}${Math.abs(group.delta_pct).toFixed(1)}%)` : '';
  const jobs = `${group.compared_jobs} job${group.compared_jobs === 1 ? '' : 's'}`;
  return `${sign}${formatUsdMicros(Math.abs(d))}${pct} over ${jobs}`;
}

/** How much of the spend is provider-reported. */
export function coverageSummary(totals) {
  if (!totals || !totals.spend_usd_micros) return { tone: 'none', text: 'No committed spend yet' };
  const spend = formatUsdMicros(totals.spend_usd_micros);
  const actual = totals.actual_usd_micros || 0;
  if (!actual) return { tone: 'none', text: `0% provider-reported: all ${spend} are the guard's estimates` };
  if (actual >= totals.spend_usd_micros) return { tone: 'full', text: `All ${spend} provider-reported` };
  return {
    tone: 'partial',
    text: `${formatUsdMicros(actual)} of ${spend} provider-reported (${pctText(totals.coverage_pct)}); the rest are estimates`,
  };
}

const ATTRIBUTION = {
  assigned: 'assigned by an admin',
  run_starter: 'started the run',
  episode_owner: 'episode owner',
  job_owner: 'owner on the job',
  unattributed_shared_login: 'shared admin login: assign a person',
  unattributed: 'nobody recorded',
};

export function attributionText(source) {
  return ATTRIBUTION[source] || source || '—';
}

/** "Video clips" from "video_clips". */
export function stageLabel(stage) {
  const s = String(stage || 'unknown').replace(/_/g, ' ');
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** "5 min ago" from an ISO timestamp. */
export function ageText(iso, now = Date.now()) {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return 'at an unknown time';
  const minutes = Math.floor(Math.max(0, now - t) / 60_000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.floor(hours / 24)} days ago`;
}

const READ_FOR = { preflight: 'before a paid step', schedule: 'scheduled check', on_demand: 'checked on demand' };
const SOURCE = {
  live: 'provider API',
  derived: 'manual entry minus spend since',
  manual: 'manual entry',
  error: 'not read',
};

/** One stored balance reading: amount (or why there is none), where it came from and when. */
export function readingLine(reading, now = Date.now()) {
  if (!reading) return { tone: 'missing', text: 'Never read', when: '' };
  const when = `${ageText(reading.read_at, now)}, ${READ_FOR[reading.read_for] || reading.read_for}`;
  if (typeof reading.balance_usd_micros !== 'number') {
    return { tone: 'missing', text: `Unknown: ${reading.error || 'no balance returned'}`, when };
  }
  return {
    tone: 'ok',
    text: `${formatUsdMicros(reading.balance_usd_micros)} (${SOURCE[reading.source] || reading.source})`,
    when,
  };
}

/** Where low-balance alerts go, without secrets. */
export function destinationText(status) {
  if (!status || !status.configured) return 'No destination configured: alerts are only recorded on this page';
  const parts = [];
  if (status.channels.includes('email') && status.email) {
    parts.push(`email to ${status.email.to.join(', ')} via ${status.email.host}:${status.email.port}`);
  }
  if (status.channels.includes('webhook') && status.webhook) parts.push(`webhook to ${status.webhook.host}`);
  return parts.join(' and ');
}

/** Each channel's outcome for one recorded alert. */
export function deliveryText(alert) {
  const channels = (alert && alert.channels) || [];
  if (!channels.length) return 'not sent: no destination configured';
  const results = alert.delivery || {};
  return channels.map((c) => `${c}: ${results[c] || 'unknown'}`).join('; ');
}

/** The answer to "Send test alert". */
export function testAlertText(result) {
  if (!result) return '';
  if (result.suppressed) return 'A test alert was sent seconds ago; wait 30 seconds before sending another.';
  if (!result.channels || !result.channels.length) {
    return 'Not sent: no alert destination is configured (BALANCE_ALERT_SMTP_* or BALANCE_ALERT_WEBHOOK_URL).';
  }
  return Object.entries(result.results || {}).map(([c, r]) => `${c}: ${r}`).join('; ');
}

/** Cost per finished episode and per finished minute. */
export function perFinishedText(pf) {
  if (!pf || !pf.finished_episodes) {
    return { episode: '—', minute: '—', note: 'No finished episode yet (final cut approved, with paid spend)' };
  }
  const episodes = `${pf.finished_episodes} finished episode${pf.finished_episodes === 1 ? '' : 's'}`;
  return {
    episode: formatUsdMicros(pf.per_episode_usd_micros),
    minute: typeof pf.per_minute_usd_micros === 'number' ? formatUsdMicros(pf.per_minute_usd_micros) : '—',
    note: `${episodes}, ${pf.finished_minutes} min of picture, ${pctText(pf.finished_coverage_pct)} provider-reported`,
  };
}

/** Month-end projection from this month's spend so far. */
export function projectionText(monthly) {
  if (!monthly) return { amount: '—', note: '' };
  return {
    amount: formatUsdMicros(monthly.projected_month_end_usd_micros),
    note: `${formatUsdMicros(monthly.spend_so_far_usd_micros)} so far in ${monthly.month}, `
      + `${pctText(monthly.coverage_pct)} provider-reported, after ${monthly.days_elapsed} of ${monthly.days_in_month} days`,
  };
}

const LEVEL = { error: 0, warning: 1, info: 2 };

/** Errors first, then warnings, then notes. */
export function sortedWarnings(list) {
  return [...(list || [])].sort((a, b) => (LEVEL[a.level] ?? 3) - (LEVEL[b.level] ?? 3));
}
