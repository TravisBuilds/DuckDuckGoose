import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  ageText, attributionText, coverageSummary, dashboardCsv, dashboardJson, deliveryText, deltaText, destinationText,
  exportHeadline, pctText, perFinishedText, projectionText, readingLine, sortedWarnings, stageLabel, testAlertText,
} from './cost-dashboard.mjs';

const NOW = Date.parse('2026-11-15T12:00:00Z');

test('percentages show one decimal, nothing to divide is a dash', () => {
  assert.equal(pctText(81.8), '81.8%');
  assert.equal(pctText(0), '0.0%');
  for (const v of [null, undefined, NaN]) assert.equal(pctText(v), '—');
});

test('actual vs estimate only speaks about jobs with both numbers', () => {
  assert.equal(deltaText({ compared_jobs: 2, delta_usd_micros: 20_600, delta_pct: 5.1 }), '+$0.02 (+5.1%) over 2 jobs');
  assert.equal(deltaText({ compared_jobs: 1, delta_usd_micros: -1_250_000, delta_pct: -12.5 }),
    '-$1.25 (-12.5%) over 1 job');
  assert.equal(deltaText({ compared_jobs: 3, delta_usd_micros: 0, delta_pct: 0 }), '±$0.00 (±0.0%) over 3 jobs');
  for (const g of [null, { compared_jobs: 0, delta_usd_micros: null }]) {
    assert.equal(deltaText(g), 'no job has both numbers yet');
  }
});

test('coverage says how much is provider-reported', () => {
  assert.deepEqual(coverageSummary({ spend_usd_micros: 0 }), { tone: 'none', text: 'No committed spend yet' });
  assert.deepEqual(coverageSummary({ spend_usd_micros: 16_650_000, actual_usd_micros: 0, coverage_pct: 0 }),
    { tone: 'none', text: "0% provider-reported: all $16.65 are the guard's estimates" });
  assert.deepEqual(coverageSummary({ spend_usd_micros: 549_600, actual_usd_micros: 449_600, coverage_pct: 81.8 }),
    { tone: 'partial', text: '$0.45 of $0.55 provider-reported (81.8%); the rest are estimates' });
  assert.equal(coverageSummary({ spend_usd_micros: 1_000_000, actual_usd_micros: 999_700, coverage_pct: 100 }).tone,
    'partial', 'rounding to 100% is not full coverage');
  assert.equal(coverageSummary({ spend_usd_micros: 5, actual_usd_micros: 5, coverage_pct: 100 }).tone, 'full');
});

test('attribution and stage labels read as words', () => {
  assert.equal(attributionText('run_starter'), 'started the run');
  assert.equal(attributionText('unattributed_shared_login'), 'shared admin login: assign a person');
  assert.equal(attributionText('something_new'), 'something_new');
  assert.equal(attributionText(null), '—');
  assert.equal(stageLabel('video_clips'), 'Video clips');
  assert.equal(stageLabel(undefined), 'Unknown');
});

test('reading ages', () => {
  assert.equal(ageText('2026-11-15T11:59:40+00:00', NOW), 'just now');
  assert.equal(ageText('2026-11-15T11:55:00+00:00', NOW), '5 min ago');
  assert.equal(ageText('2026-11-15T09:00:00+00:00', NOW), '3 h ago');
  assert.equal(ageText('2026-11-10T12:00:00+00:00', NOW), '5 days ago');
  assert.equal(ageText('not a time', NOW), 'at an unknown time');
});

test('a balance reading shows amount, source and when it was read', () => {
  assert.deepEqual(readingLine({ read_at: '2026-11-15T11:55:00+00:00', read_for: 'preflight', source: 'derived',
    balance_usd_micros: 97_400_000, error: null }, NOW),
  { tone: 'ok', text: '$97.40 (manual entry minus spend since)', when: '5 min ago, before a paid step' });
  assert.deepEqual(readingLine({ read_at: '2026-11-15T09:00:00+00:00', read_for: 'schedule', source: 'live',
    balance_usd_micros: null, error: 'ELEVENLABS_API_KEY not set' }, NOW),
  { tone: 'missing', text: 'Unknown: ELEVENLABS_API_KEY not set', when: '3 h ago, scheduled check' });
  assert.deepEqual(readingLine(undefined, NOW), { tone: 'missing', text: 'Never read', when: '' });
});

test('alert destinations and outcomes', () => {
  assert.equal(destinationText({ configured: false, channels: [] }),
    'No destination configured: alerts are only recorded on this page');
  assert.equal(destinationText({
    configured: true, channels: ['email', 'webhook'],
    email: { configured: true, host: 'smtp.example.test', port: 587, to: ['o***@example.test'], tls: 'starttls' },
    webhook: { configured: true, host: 'hooks.example.test' },
  }), 'email to o***@example.test via smtp.example.test:587 and webhook to hooks.example.test');
  assert.equal(deliveryText({ channels: [], delivery: { none: 'not sent' } }), 'not sent: no destination configured');
  assert.equal(deliveryText({ channels: ['email', 'webhook'], delivery: { email: 'failed: timeout', webhook: 'sent' } }),
    'email: failed: timeout; webhook: sent');
});

test('the test alert answer says what happened', () => {
  assert.match(testAlertText({ suppressed: true, delivered: false, channels: ['webhook'], results: {} }), /wait 30 seconds/);
  assert.match(testAlertText({ suppressed: false, delivered: false, channels: [], results: { none: 'x' } }),
    /no alert destination is configured/);
  assert.equal(testAlertText({ suppressed: false, delivered: true, channels: ['webhook'], results: { webhook: 'sent' } }),
    'webhook: sent');
  assert.equal(testAlertText(null), '');
});

test('cost per finished episode and minute', () => {
  assert.deepEqual(perFinishedText({ finished_episodes: 0 }),
    { episode: '—', minute: '—', note: 'No finished episode yet (final cut approved, with paid spend)' });
  assert.deepEqual(perFinishedText({ finished_episodes: 2, finished_minutes: 1.5, finished_coverage_pct: 75,
    per_episode_usd_micros: 400_000, per_minute_usd_micros: 400_000 }),
  { episode: '$0.40', minute: '$0.40', note: '2 finished episodes, 1.5 min of picture, 75.0% provider-reported' });
  assert.equal(perFinishedText({ finished_episodes: 1, finished_minutes: 0, per_episode_usd_micros: 1,
    per_minute_usd_micros: null }).minute, '—');
});

test('the projection comes from spend so far', () => {
  assert.deepEqual(projectionText({ month: '2026-11', days_elapsed: 14.5, days_in_month: 30,
    spend_so_far_usd_micros: 16_650_000, coverage_pct: 0, projected_month_end_usd_micros: 34_448_275 }),
  { amount: '$34.45', note: '$16.65 so far in 2026-11, 0.0% provider-reported, after 14.5 of 30 days' });
  assert.deepEqual(projectionText(null), { amount: '—', note: '' });
});

const TRUTH = {
  generated_at: '2026-11-15T12:00:00+00:00',
  totals: { spend_usd_micros: 16_650_000, actual_usd_micros: 449_600, coverage_pct: 2.7 },
  in_flight: { jobs: 1, held_usd_micros: 1_200_000 },
  monthly: { month: '2026-11', days_elapsed: 14.5, days_in_month: 30, spend_so_far_usd_micros: 16_650_000,
    actual_so_far_usd_micros: 449_600, coverage_pct: 2.7, daily_rate_usd_micros: 1_148_275,
    projected_month_end_usd_micros: 34_448_275, basis: 'committed spend so far this month' },
  by_person: [{ name: 'Producer, One', email: 'p1@example.test', attribution: ['run_starter', 'assigned'],
    spend_usd_micros: 1_234_567_890, actual_usd_micros: 0, coverage_pct: 0, episodes: 2, jobs: 5 }],
};
const USERS = [{ user_id: 'u1', name: 'Producer, One', email: null, actual_usd: '0.0000', episode_count: 2,
  job_count: 5, revision_percent: 12.345 }];

test('the exports lead with the headline the page shows', () => {
  assert.deepEqual(exportHeadline(TRUTH), {
    as_of: '2026-11-15T12:00:00+00:00', spend_usd_micros: 16_650_000, provider_reported_usd_micros: 449_600,
    coverage_pct: 2.7, in_flight_jobs: 1, in_flight_held_usd_micros: 1_200_000, month: '2026-11',
    month_spend_so_far_usd_micros: 16_650_000, projected_month_end_usd_micros: 34_448_275,
    projection_basis: 'committed spend so far this month',
  });
  assert.equal(exportHeadline(null), null);

  assert.deepEqual(dashboardCsv({ cost_truth: TRUTH, users: USERS }).split('\n'), [
    'Headline,USD,Detail',
    `Spend (all committed jobs),16.65,${coverageSummary(TRUTH.totals).text}`,
    'Provider-reported,0.45,2.7% of spend',
    `Month-end projection,${projectionText(TRUTH.monthly).amount.slice(1)},"${projectionText(TRUTH.monthly).note}"`,
    'Held by jobs in flight (not spend yet),1.20,1 job(s)',
    '',
    'Person,Email,Attribution,Spend (USD),Provider-reported (USD),Coverage,Episodes,Jobs',
    '"Producer, One",p1@example.test,started the run; assigned by an admin,1234.57,0.00,0.0%,2,5',
    '',
    'User ID,Name,Email,Episodes,Jobs,Revision %',
    'u1,"Producer, One",,2,5,12.3',
  ]);
  assert.equal(projectionText(TRUTH.monthly).amount, '$34.45');
  assert.deepEqual(dashboardCsv({ cost_truth: null, users: [] }).split('\n').slice(0, 2),
    ['Headline,USD,Detail', 'Spend (all committed jobs),,cost figures unavailable']);

  const exported = JSON.parse(dashboardJson({ cost_truth: TRUTH, users: USERS }));
  assert.equal(Object.keys(exported)[0], 'headline');
  assert.deepEqual(exported.headline, exportHeadline(TRUTH));
  assert.deepEqual(exported.users, USERS);
  assert.equal(JSON.parse(dashboardJson({ cost_truth: null, users: [] })).headline, null);
});

test('errors come before warnings and notes', () => {
  const sorted = sortedWarnings([{ level: 'info', code: 'a' }, { level: 'warning', code: 'b' },
    { level: 'error', code: 'c' }, { level: 'warning', code: 'd' }]);
  assert.deepEqual(sorted.map((w) => w.code), ['c', 'b', 'd', 'a']);
  assert.deepEqual(sortedWarnings(undefined), []);
});
