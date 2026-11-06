"use client";

import { use, useEffect, useState } from "react";
import Link from "next/link";

interface DashboardData {
  dry_run: boolean;
  timestamp: string;
  user_stats: {
    total_users: number;
    active_today: number;
    active_7d: number;
    active_30d: number;
  };
  spend_summary: {
    total_estimated_usd: string;
    total_actual_usd: string;
    variance_usd: string;
    variance_percent: number;
    failed_spend_usd: string;
    cost_tracking_coverage_percent: number;
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
  episodes: Array<{
    episode_id: string;
    user_id: string;
    actual_usd: string;
    variance_percent: number;
  }>;
  stages: Array<{
    stage: string;
    actual_usd: string;
  }>;
  providers: Array<{
    provider: string;
    actual_usd: string;
  }>;
  models: Array<{
    model: string;
    actual_usd: string;
  }>;
  daily_series: Array<{
    date: string;
    actual_usd: string;
  }>;
  monthly_runrate: {
    month: string;
    current_spend_usd: string;
    daily_rate_usd: string;
    projected_month_end_usd: string;
  };
  milestones: Array<{
    message: string;
    reached_at: string;
  }>;
  balance_status: {
    manual_balance_usd: string | null;
    set_at: string | null;
  };
}

export default function Dashboard() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [exportFormat, setExportFormat] = useState<"json" | "csv">("csv");

  useEffect(() => {
    fetchDashboard();
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

  function exportToCSV() {
    if (!data) return;

    // Export users data
    const headers = ["User ID", "Name", "Email", "Actual Spend (USD)", "Episodes", "Jobs", "Revision %"];
    const rows = data.users.map(u => [
      u.user_id,
      u.name,
      u.email || "",
      u.actual_usd,
      u.episode_count.toString(),
      u.job_count.toString(),
      u.revision_percent.toFixed(1)
    ]);
    
    const csv = [headers.join(","), ...rows.map(r => r.join(","))].join("\n");
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
    
    const json = JSON.stringify(data, null, 2);
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

  return (
    <div className="min-h-screen bg-gray-50 dark:bg-gray-900 py-8">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="flex justify-between items-center mb-8">
          <div>
            <h1 className="text-3xl font-bold text-gray-900 dark:text-white">
              Admin Dashboard
            </h1>
            <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
              {data.dry_run && (
                <span className="inline-block px-2 py-1 bg-yellow-100 dark:bg-yellow-900 text-yellow-800 dark:text-yellow-200 rounded text-xs font-medium mr-2">
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

        {/* Summary Cards */}
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6 mb-8">
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-6">
            <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400">Total Spend</h3>
            <p className="text-2xl font-bold text-gray-900 dark:text-white mt-2">
              ${data.spend_summary.total_actual_usd}
            </p>
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
              Estimated: ${data.spend_summary.total_estimated_usd}
            </p>
            {data.spend_summary.variance_percent !== 0 && (
              <p className={`text-xs mt-1 ${data.spend_summary.variance_percent > 0 ? 'text-red-600' : 'text-green-600'}`}>
                {data.spend_summary.variance_percent > 0 ? '+' : ''}
                {data.spend_summary.variance_percent}% variance
              </p>
            )}
          </div>

          <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-6">
            <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400">Active Users</h3>
            <p className="text-2xl font-bold text-gray-900 dark:text-white mt-2">
              {data.user_stats.active_7d}
            </p>
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
              Total: {data.user_stats.total_users} users
            </p>
            <p className="text-xs text-gray-500 dark:text-gray-400">
              Today: {data.user_stats.active_today} | 30d: {data.user_stats.active_30d}
            </p>
          </div>

          <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-6">
            <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400">Monthly Projection</h3>
            <p className="text-2xl font-bold text-gray-900 dark:text-white mt-2">
              ${data.monthly_runrate.projected_month_end_usd}
            </p>
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
              Current: ${data.monthly_runrate.current_spend_usd}
            </p>
            <p className="text-xs text-gray-500 dark:text-gray-400">
              Daily rate: ${data.monthly_runrate.daily_rate_usd}
            </p>
          </div>

          <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-6">
            <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400">Cost Accuracy</h3>
            <p className="text-2xl font-bold text-gray-900 dark:text-white mt-2">
              {data.spend_summary.cost_tracking_coverage_percent}%
            </p>
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
              Tracking coverage
            </p>
            <p className="text-xs text-gray-500 dark:text-gray-400">
              Failed: ${data.spend_summary.failed_spend_usd}
            </p>
          </div>
        </div>

        {/* Daily Spend Chart */}
        <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-6 mb-8">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white mb-4">
            Daily Spend (Last 30 Days)
          </h2>
          <div className="h-64 flex items-end gap-1">
            {data.daily_series.map((day, i) => {
              const maxSpend = Math.max(...data.daily_series.map(d => parseFloat(d.actual_usd)));
              const height = maxSpend > 0 ? (parseFloat(day.actual_usd) / maxSpend) * 100 : 0;
              
              return (
                <div
                  key={day.date}
                  className="flex-1 bg-blue-500 hover:bg-blue-600 rounded-t transition-colors relative group"
                  style={{ height: `${height}%`, minHeight: height > 0 ? '2px' : '0' }}
                  title={`${day.date}: $${day.actual_usd}`}
                >
                  <div className="absolute bottom-full mb-2 left-1/2 transform -translate-x-1/2 bg-gray-900 text-white text-xs rounded px-2 py-1 opacity-0 group-hover:opacity-100 transition-opacity whitespace-nowrap pointer-events-none">
                    {day.date}<br/>${day.actual_usd}
                  </div>
                </div>
              );
            })}
          </div>
          <div className="flex justify-between text-xs text-gray-500 dark:text-gray-400 mt-2">
            <span>{data.daily_series[0]?.date}</span>
            <span>{data.daily_series[data.daily_series.length - 1]?.date}</span>
          </div>
        </div>

        {/* Top Spenders */}
        <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-6 mb-8">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white mb-4">
            Top Spenders
          </h2>
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
              <thead>
                <tr>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                    User
                  </th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                    Email
                  </th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                    Spend
                  </th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                    Episodes
                  </th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                    Jobs
                  </th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                    Revision %
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
                {data.users.slice(0, 10).map((user) => (
                  <tr key={user.user_id} className="hover:bg-gray-50 dark:hover:bg-gray-700">
                    <td className="px-4 py-3 text-sm text-gray-900 dark:text-white">
                      {user.name}
                    </td>
                    <td className="px-4 py-3 text-sm text-gray-500 dark:text-gray-400">
                      {user.email || '—'}
                    </td>
                    <td className="px-4 py-3 text-sm text-gray-900 dark:text-white text-right font-medium">
                      ${user.actual_usd}
                    </td>
                    <td className="px-4 py-3 text-sm text-gray-500 dark:text-gray-400 text-right">
                      {user.episode_count}
                    </td>
                    <td className="px-4 py-3 text-sm text-gray-500 dark:text-gray-400 text-right">
                      {user.job_count}
                    </td>
                    <td className="px-4 py-3 text-sm text-right">
                      <span className={`inline-block px-2 py-1 rounded text-xs ${
                        user.revision_percent > 70 ? 'bg-red-100 dark:bg-red-900 text-red-800 dark:text-red-200' :
                        user.revision_percent > 40 ? 'bg-yellow-100 dark:bg-yellow-900 text-yellow-800 dark:text-yellow-200' :
                        'bg-green-100 dark:bg-green-900 text-green-800 dark:text-green-200'
                      }`}>
                        {user.revision_percent.toFixed(1)}%
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        {/* Stage & Provider Breakdown */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-8 mb-8">
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-6">
            <h2 className="text-lg font-semibold text-gray-900 dark:text-white mb-4">
              Spend by Stage
            </h2>
            <div className="space-y-3">
              {data.stages.filter(s => parseFloat(s.actual_usd) > 0).map((stage) => (
                <div key={stage.stage}>
                  <div className="flex justify-between text-sm mb-1">
                    <span className="text-gray-700 dark:text-gray-300 capitalize">
                      {stage.stage.replace(/_/g, ' ')}
                    </span>
                    <span className="font-medium text-gray-900 dark:text-white">
                      ${stage.actual_usd}
                    </span>
                  </div>
                  <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-2">
                    <div
                      className="bg-blue-600 h-2 rounded-full"
                      style={{
                        width: `${(parseFloat(stage.actual_usd) / parseFloat(data.spend_summary.total_actual_usd) * 100).toFixed(1)}%`
                      }}
                    />
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-6">
            <h2 className="text-lg font-semibold text-gray-900 dark:text-white mb-4">
              Spend by Provider
            </h2>
            <div className="space-y-4">
              {data.providers.map((provider) => (
                <div key={provider.provider}>
                  <div className="flex justify-between text-sm mb-2">
                    <span className="text-gray-700 dark:text-gray-300 capitalize">
                      {provider.provider}
                    </span>
                    <span className="font-medium text-gray-900 dark:text-white">
                      ${provider.actual_usd}
                    </span>
                  </div>
                </div>
              ))}
              <div className="pt-4 border-t border-gray-200 dark:border-gray-700">
                <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                  Top Models
                </h3>
                {data.models.slice(0, 5).map((model) => (
                  <div key={model.model} className="flex justify-between text-sm py-1">
                    <span className="text-gray-600 dark:text-gray-400">{model.model}</span>
                    <span className="text-gray-900 dark:text-white">${model.actual_usd}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>

        {/* Recent Milestones */}
        {data.milestones && data.milestones.length > 0 && (
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-6 mb-8">
            <h2 className="text-lg font-semibold text-gray-900 dark:text-white mb-4">
              Recent Milestones
            </h2>
            <div className="space-y-2">
              {data.milestones.map((milestone, i) => (
                <div key={i} className="flex justify-between items-center py-2 border-b border-gray-200 dark:border-gray-700 last:border-b-0">
                  <span className="text-sm text-gray-700 dark:text-gray-300">
                    {milestone.message}
                  </span>
                  <span className="text-xs text-gray-500 dark:text-gray-400">
                    {new Date(milestone.reached_at).toLocaleDateString()}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Balance Status */}
        {data.balance_status.manual_balance_usd && (
          <div className="bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded-lg p-4">
            <div className="flex items-start">
              <div className="flex-shrink-0">
                <svg className="h-5 w-5 text-blue-400" fill="currentColor" viewBox="0 0 20 20">
                  <path fillRule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a1 1 0 000 2v3a1 1 0 001 1h1a1 1 0 100-2v-3a1 1 0 00-1-1H9z" clipRule="evenodd" />
                </svg>
              </div>
              <div className="ml-3">
                <h3 className="text-sm font-medium text-blue-800 dark:text-blue-200">
                  Manual Balance: ${data.balance_status.manual_balance_usd}
                </h3>
                {data.balance_status.set_at && (
                  <p className="text-xs text-blue-700 dark:text-blue-300 mt-1">
                    Last updated: {new Date(data.balance_status.set_at).toLocaleString()}
                  </p>
                )}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
