"use client";

import { useEffect, useState } from "react";
import {
  attributionText, coverageSummary, dashboardCsv, dashboardJson, deliveryText, deltaText, destinationText,
  formatUsdMicros, pctText, perFinishedText, projectionText, readingLine, sortedWarnings, stageLabel, testAlertText,
} from "@/lib/cost-dashboard.mjs";
import type {
  AlertStatus, BalanceReading, CostGroup, CostWarning, MonthlyProjection, PerFinished, RecordedAlert, SendResult,
} from "@/lib/cost-dashboard.mjs";

// Format USD value as currency (e.g., "18.1034" -> "$18.10")
function formatUSD(value: string | number): string {
  const num = typeof value === 'string' ? parseFloat(value) : value;
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(num);
}

interface PersonRef {
  user_id: string | null;
  name: string;
  email: string | null;
}

interface EpisodeCost extends CostGroup {
  episode_id: string;
  providers: Record<string, CostGroup>;
  person: PersonRef | null;
  attribution: string;
  finished: boolean;
  final_cut_approved_at: string | null;
  duration_s: number | null;
}

interface CostTruth {
  generated_at: string;
  totals: CostGroup;
  in_flight: { jobs: number; held_usd_micros: number };
  by_actual_source: Record<string, number>;
  by_provider: Array<CostGroup & { provider: string }>;
  by_stage: Array<CostGroup & { stage: string }>;
  by_episode: EpisodeCost[];
  by_person: Array<CostGroup & PersonRef & { episodes: number; attribution: string[] }>;
  unattributed_usd_micros: Record<string, number>;
  per_finished: PerFinished;
  monthly: MonthlyProjection;
  daily: Array<{ date: string; spend_usd_micros: number; actual_usd_micros: number }>;
  provider_reported: {
    month: string;
    higgsfield_month_usd_micros: Record<"total" | "allocated" | "unmatched" | "other_api_key" | "pending", number>;
    unmatched_buckets: Array<{ day: string; model: string; unit: string; quantity: string; cost_usd_micros: number; reason: string }>;
    last_sync: { ran_at: string; ok: boolean; error: string | null; mode: string | null } | null;
  };
  rates: {
    elevenlabs: { usd_per_credit: string; env: string; configured: boolean };
    higgsfield: { usd_per_credit: string | null; env: string; problem: string | null; note: string };
  };
}

interface BalanceOverview {
  readings: Record<"higgsfield" | "elevenlabs", BalanceReading[]>;
  consistency: { entries: number; unchanged_despite_spend: boolean };
  schedule: {
    enabled: boolean;
    interval_minutes: number | null;
    problem: string | null;
    last_run_at: string | null;
    overdue: boolean;
    dry_run: boolean;
  };
}

interface DashboardData {
  dry_run: boolean;
  timestamp: string;
  user_stats: {
    total_users: number;
    active_today: number;
    active_7d: number;
    active_30d: number;
  };
  users: Array<{
    user_id: string;
    name: string;
    email: string | null;
    actual_usd: string;
    episode_count: number;
    job_count: number;
    revision_percent: number;
  }>;
  models: Array<{
    model: string;
    actual_usd: string;
  }>;
  milestones: Array<{
    message: string;
    reached_at: string;
  }>;
  balance_status: {
    manual_balance_usd: string | null;
    set_at: string | null;
    set_by: string | null;
    spent_since_entry_usd_micros: number | null;
    reserved_usd_micros: number | null;
    balance_remaining_usd_micros: number | null;
  };
  cost_truth: CostTruth | null;
  balances: BalanceOverview | null;
  alerts: { status: AlertStatus; recent: RecordedAlert[] };
  cost_warnings: CostWarning[];
}

interface StudioUser {
  user_id: string;
  name: string;
  email: string;
}

const card = "bg-white rounded-lg shadow p-6";
const h2 = "text-lg font-semibold text-gray-900";
const th = "px-3 py-2 text-xs font-medium text-gray-500 uppercase tracking-wider";
const td = "px-3 py-2 text-sm text-gray-900";
const muted = "text-xs text-gray-500";
const button = "px-3 py-1.5 rounded-md text-sm font-medium disabled:opacity-50";

const WARNING_STYLE: Record<string, string> = {
  error: "bg-red-50 border-red-300 text-red-800",
  warning: "bg-yellow-50 border-yellow-300 text-yellow-900",
  info: "bg-blue-50 border-blue-200 text-blue-800",
};

const COVERAGE_STYLE: Record<string, string> = {
  none: "text-red-600",
  partial: "text-yellow-700",
  full: "text-green-700",
};

async function callApi(path: string, init: RequestInit = {}) {
  const response = await fetch(path, { method: "POST", credentials: "include", ...init });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `${path}: HTTP ${response.status}`);
  return body;
}

function CostTable({ title, rows, label }: {
  title: string;
  rows: Array<CostGroup & { key: string }>;
  label: string;
}) {
  return (
    <div className={card}>
      <h2 className={`${h2} mb-1`}>{title}</h2>
      <p className={`${muted} mb-3`}>
        Spend is what the provider reported where recorded, the estimate otherwise. The difference only
        counts jobs that have both numbers.
      </p>
      <div className="overflow-x-auto">
        <table className="min-w-full divide-y divide-gray-200">
          <thead>
            <tr>
              <th className={`${th} text-left`}>{label}</th>
              <th className={`${th} text-right`}>Jobs</th>
              <th className={`${th} text-right`}>Spend</th>
              <th className={`${th} text-right`}>Provider-reported</th>
              <th className={`${th} text-right`}>Estimated</th>
              <th className={`${th} text-right`}>Coverage</th>
              <th className={`${th} text-right`}>Actual vs estimate</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-200">
            {rows.map((row) => (
              <tr key={row.key}>
                <td className={td}>{row.key}</td>
                <td className={`${td} text-right`}>{row.jobs}</td>
                <td className={`${td} text-right font-medium`}>{formatUsdMicros(row.spend_usd_micros)}</td>
                <td className={`${td} text-right`}>
                  {formatUsdMicros(row.actual_usd_micros)}
                  {row.backfilled_usd_micros > 0 && (
                    <span className={`${muted} block`}>{formatUsdMicros(row.backfilled_usd_micros)} backfilled</span>
                  )}
                </td>
                <td className={`${td} text-right`}>{formatUsdMicros(row.estimate_usd_micros)}</td>
                <td className={`${td} text-right`}>{pctText(row.coverage_pct)}</td>
                <td className={`${td} text-right`}>{deltaText(row)}</td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr><td className={`${td} text-gray-500`} colSpan={7}>No committed spend yet</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function Dashboard() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [studioUsers, setStudioUsers] = useState<StudioUser[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const [testAlert, setTestAlert] = useState<SendResult | null>(null);

  useEffect(() => {
    fetchDashboard();
    fetchUsers();
  }, []);

  async function fetchDashboard() {
    try {
      const response = await fetch("/api/dashboard", {
        credentials: "include",
      });
      if (!response.ok) {
        throw new Error(`Failed to fetch dashboard: ${response.statusText}`);
      }
      const dashboardData = await response.json();
      setData(dashboardData);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  }

  async function fetchUsers() {
    try {
      const response = await fetch("/api/users", { credentials: "include" });
      if (response.ok) setStudioUsers((await response.json()).users || []);
    } catch {
      setStudioUsers([]);
    }
  }

  async function runAction(name: string, action: () => Promise<string>) {
    setBusy(name);
    setActionMessage(null);
    try {
      setActionMessage(await action());
      await fetchDashboard();
    } catch (err) {
      setActionMessage(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  const checkBalances = () => runAction("balances", async () => {
    const result = await callApi("/api/balance/check");
    const problems = [...(result.errors || []), ...(result.warnings || [])];
    return problems.length ? `Balances read. ${problems.join(" ")}` : "Balances read.";
  });

  const syncUsage = () => runAction("sync", async () => {
    const result = await callApi("/api/costs/sync");
    if (result.skipped) return `Usage sync skipped: ${result.skipped}`;
    if (!result.ok) return `Usage sync failed: ${result.error}`;
    return `Usage report read: ${result.rows} rows, ${result.jobs_updated} jobs given provider-reported costs.`;
  });

  const sendTestAlert = () => runAction("test-alert", async () => {
    const result: SendResult = await callApi("/api/alerts/test");
    setTestAlert(result);
    return testAlertText(result);
  });

  const assignSpend = (episodeId: string, userId: string) => runAction(`assign-${episodeId}`, async () => {
    if (userId) {
      const saved = await callApi(`/api/dashboard/episodes/${encodeURIComponent(episodeId)}/attribution`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: userId }),
      });
      return `${episodeId} spend attributed to ${saved.name || userId}.`;
    }
    await callApi(`/api/dashboard/episodes/${encodeURIComponent(episodeId)}/attribution`, { method: "DELETE" });
    return `${episodeId} spend goes back to automatic attribution.`;
  });

  function exportToCSV() {
    if (!data) return;

    const csv = dashboardCsv(data);
    const blob = new Blob([csv], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `dashboard-${new Date().toISOString().split("T")[0]}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  function exportToJSON() {
    if (!data) return;
    
    const json = dashboardJson(data);
    const blob = new Blob([json], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `dashboard-${new Date().toISOString().split("T")[0]}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="text-lg">Loading dashboard...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="text-red-600">Error: {error}</div>
      </div>
    );
  }

  if (!data) {
    return null;
  }

  const truth = data.cost_truth;
  const coverage = coverageSummary(truth?.totals);
  const projection = projectionText(truth?.monthly);
  const finished = perFinishedText(truth?.per_finished);
  const warnings = sortedWarnings(data.cost_warnings);
  const now = Date.now();
  const maxDaily = Math.max(0, ...(truth?.daily || []).map(d => d.spend_usd_micros));

  return (
    <div className="min-h-screen bg-gray-50 py-8">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="flex flex-wrap justify-between items-center gap-3 mb-8">
          <div>
            <h1 className="text-3xl font-bold text-gray-900">
              Admin Dashboard
            </h1>
            <p className="text-sm text-gray-500 mt-1">
              {data.dry_run && (
                <span className="inline-block px-2 py-1 bg-yellow-100 text-yellow-800 rounded text-xs font-medium mr-2">
                  DRY RUN MODE
                </span>
              )}
              Last updated: {new Date(data.timestamp).toLocaleString()}
            </p>
          </div>
          <div className="flex gap-2">
            <button
              onClick={exportToCSV}
              className="px-4 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700 text-sm"
            >
              Export CSV
            </button>
            <button
              onClick={exportToJSON}
              className="px-4 py-2 bg-gray-600 text-white rounded-md hover:bg-gray-700 text-sm"
            >
              Export JSON
            </button>
          </div>
        </div>

        {/* Problems the numbers below depend on */}
        {warnings.length > 0 && (
          <div className="space-y-2 mb-6" data-testid="cost-warnings">
            {warnings.map((w) => (
              <div key={`${w.code}-${w.message}`} className={`border rounded-md px-4 py-2 text-sm ${WARNING_STYLE[w.level] || WARNING_STYLE.info}`}>
                {w.message}
              </div>
            ))}
          </div>
        )}
        {actionMessage && (
          <div className="mb-6 border rounded-md px-4 py-2 text-sm bg-white border-gray-300 text-gray-800">
            {actionMessage}
          </div>
        )}

        {/* Summary Cards */}
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6 mb-8">
          <div className={card}>
            <h3 className="text-sm font-medium text-gray-500">Spend (all committed jobs)</h3>
            <p className="text-2xl font-bold text-gray-900 mt-2">
              {truth ? formatUsdMicros(truth.totals.spend_usd_micros) : "—"}
            </p>
            <p className={`text-xs mt-1 ${COVERAGE_STYLE[coverage.tone]}`}>{coverage.text}</p>
            {truth && truth.in_flight.jobs > 0 && (
              <p className={muted}>
                {truth.in_flight.jobs} job(s) in flight holding {formatUsdMicros(truth.in_flight.held_usd_micros)}
              </p>
            )}
          </div>

          <div className={card}>
            <h3 className="text-sm font-medium text-gray-500">Actual vs estimate</h3>
            <p className="text-2xl font-bold text-gray-900 mt-2">
              {truth ? pctText(truth.totals.delta_pct) : "—"}
            </p>
            <p className={`${muted} mt-1`}>{truth ? deltaText(truth.totals) : ""}</p>
            {truth && (
              <p className={muted}>
                Provider-reported {formatUsdMicros(truth.totals.compared_actual_usd_micros)} vs estimated{" "}
                {formatUsdMicros(truth.totals.compared_estimate_usd_micros)}
              </p>
            )}
          </div>

          <div className={card}>
            <h3 className="text-sm font-medium text-gray-500">Month-end projection</h3>
            <p className="text-2xl font-bold text-gray-900 mt-2">{projection.amount}</p>
            <p className={`${muted} mt-1`}>{projection.note}</p>
          </div>

          <div className={card}>
            <h3 className="text-sm font-medium text-gray-500">Per finished episode</h3>
            <p className="text-2xl font-bold text-gray-900 mt-2">{finished.episode}</p>
            <p className={`${muted} mt-1`}>Per finished minute: {finished.minute}</p>
            <p className={muted}>{finished.note}</p>
            {truth?.per_finished.all_spend_per_finished_episode_usd_micros != null && (
              <p className={muted}>
                All spend per finished episode:{" "}
                {formatUsdMicros(truth.per_finished.all_spend_per_finished_episode_usd_micros)}
              </p>
            )}
          </div>
        </div>

        {/* Balances */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-8 mb-8">
          <div className={card} data-testid="balances">
            <div className="flex justify-between items-center mb-3">
              <h2 className={h2}>Provider balances</h2>
              <button
                onClick={checkBalances}
                disabled={busy !== null}
                className={`${button} bg-blue-600 text-white hover:bg-blue-700`}
              >
                {busy === "balances" ? "Checking…" : "Check now"}
              </button>
            </div>
            {(["higgsfield", "elevenlabs"] as const).map((provider) => {
              const readings = data.balances?.readings[provider] || [];
              const latest = readingLine(readings[0], now);
              return (
                <div key={provider} className="py-2 border-b border-gray-200 last:border-b-0">
                  <div className="flex justify-between text-sm">
                    <span className="font-medium text-gray-700 capitalize">{provider}</span>
                    <span className={latest.tone === "ok" ? "text-gray-900" : "text-red-600"}>
                      {latest.text}
                    </span>
                  </div>
                  <p className={muted}>
                    {readings[0] ? `Read ${latest.when} (${new Date(readings[0].read_at).toLocaleString()})` : "Never read"}
                  </p>
                  {readings.length > 1 && (
                    <details className="mt-1">
                      <summary className={`${muted} cursor-pointer`}>Earlier readings</summary>
                      <ul className="mt-1 space-y-0.5">
                        {readings.slice(1).map((r) => {
                          const line = readingLine(r, now);
                          return (
                            <li key={`${r.read_at}-${r.read_for}`} className={muted}>
                              {new Date(r.read_at).toLocaleString()}: {line.text}, {line.when}
                            </li>
                          );
                        })}
                      </ul>
                    </details>
                  )}
                </div>
              );
            })}
            {data.balance_status.manual_balance_usd && (
              <p className={`${muted} mt-3`}>
                Higgsfield manual entry {formatUSD(data.balance_status.manual_balance_usd)}
                {data.balance_status.set_at && ` set ${new Date(data.balance_status.set_at).toLocaleString()}`}
                {data.balance_status.set_by && ` by ${data.balance_status.set_by}`}
                {data.balance_status.balance_remaining_usd_micros != null && (
                  <>; balance gate: {formatUsdMicros(data.balance_status.balance_remaining_usd_micros)} left for new jobs after committed and in-flight spend since</>
                )}
              </p>
            )}
            {data.balances && (
              <p className={`${muted} mt-1`}>
                Scheduled check:{" "}
                {data.balances.schedule.enabled
                  ? `every ${data.balances.schedule.interval_minutes ?? "?"} min, last ${
                    data.balances.schedule.last_run_at ? new Date(data.balances.schedule.last_run_at).toLocaleString() : "never"}`
                  : "disabled"}
              </p>
            )}
          </div>

          {/* Alerts */}
          <div className={card} data-testid="alerts">
            <div className="flex justify-between items-center mb-3">
              <h2 className={h2}>Low-balance alerts</h2>
              <button
                onClick={sendTestAlert}
                disabled={busy !== null}
                className={`${button} bg-gray-700 text-white hover:bg-gray-800`}
              >
                {busy === "test-alert" ? "Sending…" : "Send test alert"}
              </button>
            </div>
            <p className={`text-sm ${data.alerts.status.configured ? "text-gray-700" : "text-red-600"}`}>
              {destinationText(data.alerts.status)}
            </p>
            {testAlert && <p className={`${muted} mt-1`}>Test alert: {testAlertText(testAlert)}</p>}
            <ul className="mt-3 space-y-1">
              {data.alerts.recent.map((a) => (
                <li key={a.id} className="text-sm">
                  <span className="text-gray-900">{a.subject}</span>
                  {a.is_test && <span className={`${muted} ml-1`}>(test)</span>}
                  <span className={`${muted} block`}>
                    {new Date(a.sent_at).toLocaleString()}: {deliveryText(a)}
                  </span>
                </li>
              ))}
              {data.alerts.recent.length === 0 && <li className={muted}>No alert has been recorded yet.</li>}
            </ul>
          </div>
        </div>

        {/* Actual vs estimate */}
        {truth && (
          <div className="space-y-8 mb-8">
            <CostTable
              title="Actual vs estimate by provider"
              label="Provider"
              rows={truth.by_provider.map((p) => ({ ...p, key: p.provider }))}
            />
            <CostTable
              title="Actual vs estimate by stage"
              label="Stage"
              rows={truth.by_stage.map((s) => ({ ...s, key: stageLabel(s.stage) }))}
            />

            <div className={card} data-testid="episodes">
              <h2 className={`${h2} mb-1`}>Actual vs estimate by episode</h2>
              <p className={`${muted} mb-3`}>
                Spend goes to whoever started the run. Runs started with the shared admin login have no person:
                assign one here (this does not change who may edit the episode).
              </p>
              <div className="overflow-x-auto">
                <table className="min-w-full divide-y divide-gray-200">
                  <thead>
                    <tr>
                      <th className={`${th} text-left`}>Episode</th>
                      <th className={`${th} text-right`}>Spend</th>
                      <th className={`${th} text-right`}>Higgsfield</th>
                      <th className={`${th} text-right`}>ElevenLabs</th>
                      <th className={`${th} text-right`}>Coverage</th>
                      <th className={`${th} text-right`}>Actual vs estimate</th>
                      <th className={`${th} text-left`}>Person</th>
                      <th className={`${th} text-left`}>Assign</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-200">
                    {truth.by_episode.map((ep) => (
                      <tr key={ep.episode_id}>
                        <td className={td}>
                          {ep.episode_id}
                          {ep.finished && (
                            <span className={`${muted} block`}>
                              finished{ep.duration_s ? `, ${(ep.duration_s / 60).toFixed(1)} min` : ""}
                            </span>
                          )}
                        </td>
                        <td className={`${td} text-right font-medium`}>{formatUsdMicros(ep.spend_usd_micros)}</td>
                        {(["higgsfield", "elevenlabs"] as const).map((p) => (
                          <td key={p} className={`${td} text-right`}>
                            {ep.providers[p] ? (
                              <>
                                {formatUsdMicros(ep.providers[p].spend_usd_micros)}
                                <span className={`${muted} block`}>{pctText(ep.providers[p].coverage_pct)} reported</span>
                              </>
                            ) : "—"}
                          </td>
                        ))}
                        <td className={`${td} text-right`}>{pctText(ep.coverage_pct)}</td>
                        <td className={`${td} text-right`}>{deltaText(ep)}</td>
                        <td className={td}>
                          {ep.person?.name || "—"}
                          <span className={`${muted} block`}>{attributionText(ep.attribution)}</span>
                        </td>
                        <td className={td}>
                          <select
                            aria-label={`Attribute ${ep.episode_id} spend to`}
                            disabled={busy !== null}
                            value={ep.attribution === "assigned" ? ep.person?.user_id || "" : ""}
                            onChange={(e) => assignSpend(ep.episode_id, e.target.value)}
                            className="text-sm rounded border-gray-300"
                          >
                            <option value="">Automatic</option>
                            {studioUsers.map((u) => (
                              <option key={u.user_id} value={u.user_id}>{u.name}</option>
                            ))}
                          </select>
                        </td>
                      </tr>
                    ))}
                    {truth.by_episode.length === 0 && (
                      <tr><td className={`${td} text-gray-500`} colSpan={8}>No committed spend yet</td></tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>

            <div className={card} data-testid="people">
              <h2 className={`${h2} mb-3`}>Spend by person</h2>
              <div className="overflow-x-auto">
                <table className="min-w-full divide-y divide-gray-200">
                  <thead>
                    <tr>
                      <th className={`${th} text-left`}>Person</th>
                      <th className={`${th} text-right`}>Spend</th>
                      <th className={`${th} text-right`}>Provider-reported</th>
                      <th className={`${th} text-right`}>Coverage</th>
                      <th className={`${th} text-right`}>Episodes</th>
                      <th className={`${th} text-left`}>Attributed by</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-200">
                    {truth.by_person.map((p) => (
                      <tr key={p.user_id || "unattributed"}>
                        <td className={td}>
                          {p.name}
                          {p.email && <span className={`${muted} block`}>{p.email}</span>}
                        </td>
                        <td className={`${td} text-right font-medium`}>{formatUsdMicros(p.spend_usd_micros)}</td>
                        <td className={`${td} text-right`}>{formatUsdMicros(p.actual_usd_micros)}</td>
                        <td className={`${td} text-right`}>{pctText(p.coverage_pct)}</td>
                        <td className={`${td} text-right`}>{p.episodes}</td>
                        <td className={`${td} text-gray-500`}>
                          {p.attribution.map(attributionText).join(", ")}
                        </td>
                      </tr>
                    ))}
                    {truth.by_person.length === 0 && (
                      <tr><td className={`${td} text-gray-500`} colSpan={6}>No committed spend yet</td></tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>

            {/* Higgsfield usage report */}
            <div className={card} data-testid="provider-report">
              <div className="flex justify-between items-center mb-3">
                <h2 className={h2}>Higgsfield usage report ({truth.provider_reported.month})</h2>
                <button
                  onClick={syncUsage}
                  disabled={busy !== null}
                  className={`${button} bg-blue-600 text-white hover:bg-blue-700`}
                >
                  {busy === "sync" ? "Syncing…" : "Sync now"}
                </button>
              </div>
              <div className="grid grid-cols-2 md:grid-cols-5 gap-4 text-sm">
                {([
                  ["Reported", "total"], ["Matched to jobs", "allocated"], ["Unmatched", "unmatched"],
                  ["Other API keys", "other_api_key"], ["Not settled yet", "pending"],
                ] as const).map(([label, key]) => (
                  <div key={key}>
                    <p className={muted}>{label}</p>
                    <p className="font-medium text-gray-900">
                      {formatUsdMicros(truth.provider_reported.higgsfield_month_usd_micros[key])}
                    </p>
                  </div>
                ))}
              </div>
              <p className={`${muted} mt-3`}>
                {truth.provider_reported.last_sync
                  ? `Last read ${new Date(truth.provider_reported.last_sync.ran_at).toLocaleString()}${
                    truth.provider_reported.last_sync.ok ? "" : `: failed (${truth.provider_reported.last_sync.error})`}`
                  : "Never read."}{" "}
                Rates: ElevenLabs ${truth.rates.elevenlabs.usd_per_credit} per credit ({truth.rates.elevenlabs.env});
                Higgsfield bills in USD{truth.rates.higgsfield.usd_per_credit
                  ? `, credits at $${truth.rates.higgsfield.usd_per_credit}` : ""}.
              </p>
              {truth.provider_reported.unmatched_buckets.length > 0 && (
                <div className="mt-3">
                  <h3 className="text-sm font-medium text-gray-700 mb-1">Unmatched provider spend</h3>
                  <ul className="space-y-0.5">
                    {truth.provider_reported.unmatched_buckets.map((b) => (
                      <li key={`${b.day}-${b.model}-${b.unit}`} className={muted}>
                        {b.day} {b.model}: {b.quantity} × {b.unit}, {formatUsdMicros(b.cost_usd_micros)}. {b.reason}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>

            {/* Daily spend */}
            <div className={card}>
              <h2 className={`${h2} mb-4`}>Daily spend (last 30 days with spend)</h2>
              <div className="h-48 flex items-end gap-1">
                {truth.daily.map((day) => {
                  const height = maxDaily > 0 ? (day.spend_usd_micros / maxDaily) * 100 : 0;
                  const reported = day.spend_usd_micros > 0 ? (day.actual_usd_micros / day.spend_usd_micros) * 100 : 0;
                  return (
                    <div
                      key={day.date}
                      className="flex-1 bg-blue-200 rounded-t relative flex flex-col justify-end"
                      style={{ height: `${height}%`, minHeight: height > 0 ? "2px" : "0" }}
                      title={`${day.date}: ${formatUsdMicros(day.spend_usd_micros)} (${formatUsdMicros(day.actual_usd_micros)} provider-reported)`}
                    >
                      <div className="bg-blue-600 rounded-t" style={{ height: `${reported}%` }} />
                    </div>
                  );
                })}
              </div>
              <p className={`${muted} mt-2`}>Dark: provider-reported. Light: estimates.</p>
            </div>
          </div>
        )}

        {/* Users */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mb-8">
          <div className={card}>
            <h3 className="text-sm font-medium text-gray-500">Active Users</h3>
            <p className="text-2xl font-bold text-gray-900 mt-2">
              {data.user_stats.active_7d}
            </p>
            <p className="text-xs text-gray-500 mt-1">
              Total: {data.user_stats.total_users} users
            </p>
            <p className="text-xs text-gray-500">
              Today: {data.user_stats.active_today} | 30d: {data.user_stats.active_30d}
            </p>
          </div>
          <div className={card}>
            <h3 className="text-sm font-medium text-gray-500">Top models (ledger)</h3>
            {(data.models || []).slice(0, 5).map((model) => (
              <div key={model.model} className="flex justify-between text-sm py-1">
                <span className="text-gray-600 truncate mr-2">{model.model}</span>
                <span className="text-gray-900">{formatUSD(model.actual_usd)}</span>
              </div>
            ))}
          </div>
        </div>

        {/* Recent Milestones */}
        {data.milestones && data.milestones.length > 0 && (
          <div className="bg-white rounded-lg shadow p-6 mb-8">
            <h2 className="text-lg font-semibold text-gray-900 mb-4">
              Recent Milestones
            </h2>
            <div className="space-y-2">
              {data.milestones.map((milestone, i) => (
                <div key={i} className="flex justify-between items-center py-2 border-b border-gray-200 last:border-b-0">
                  <span className="text-sm text-gray-700">
                    {milestone.message}
                  </span>
                  <span className="text-xs text-gray-500">
                    {new Date(milestone.reached_at).toLocaleDateString()}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
