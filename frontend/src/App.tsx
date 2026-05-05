import { useEffect, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import "./App.css";

const API_URL = "http://localhost:8000/api/mrr";

const ACCENT = "#10b981";
const TEXT_MUTED = "#9ca3af";
const GRID = "#374151";

interface MrrRow {
  month: string; // YYYY-MM-DD
  mrr_amount: number;
  is_current: boolean;
}

const formatMonth = (iso: string, opts: { short?: boolean } = {}): string => {
  // Treat the YYYY-MM-DD as a calendar date, not midnight UTC. The Date
  // constructor on a YYYY-MM-DD string is implementation-defined; build it
  // explicitly from the components to avoid timezone shifts.
  const [y, m] = iso.split("-").map(Number);
  const d = new Date(y, m - 1, 1);
  const monthShort = d.toLocaleString("en-US", { month: "short" });
  const yearShort = String(y).slice(-2);
  return opts.short ? `${monthShort} '${yearShort}` : `${monthShort} ${y}`;
};

const formatToday = (): string =>
  new Date().toLocaleDateString("en-US", {
    month: "long",
    day: "numeric",
    year: "numeric",
  });

const formatDollars = (n: number, opts: { decimals?: 0 | 2 } = {}): string =>
  n.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: opts.decimals ?? 2,
    maximumFractionDigits: opts.decimals ?? 2,
  });

interface ChartPoint {
  isoMonth: string;
  label: string;
  value: number;
  isCurrent: boolean;
  historicalValue: number | null;
  projectedValue: number | null;
}

interface ChartTooltipProps {
  active?: boolean;
  payload?: Array<{ payload: ChartPoint }>;
}

function ChartTooltip({ active, payload }: ChartTooltipProps) {
  if (!active || !payload?.length) return null;
  const { isoMonth, value, isCurrent } = payload[0].payload;
  return (
    <div className="tooltip">
      <div className="tooltip-month">
        {formatMonth(isoMonth)}
        {isCurrent && " (now)"}
      </div>
      <div className="tooltip-value">{formatDollars(value)}</div>
    </div>
  );
}

interface DotProps {
  cx?: number;
  cy?: number;
  payload?: ChartPoint;
}

// Renders a dot only on the "current" point so the dashed segment ends in
// a visible marker without double-drawing on the Apr anchor point.
function ProjectedDot({ cx, cy, payload }: DotProps) {
  if (!payload?.isCurrent || cx == null || cy == null) {
    return <g />;
  }
  return <circle cx={cx} cy={cy} r={4} fill={ACCENT} />;
}

function App() {
  const [data, setData] = useState<MrrRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch(API_URL)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((rows: MrrRow[]) => setData(rows))
      .catch((e: Error) => setError(e.message));
  }, []);

  if (error) {
    return (
      <div className="app">
        <div className="status error">
          Failed to load MRR data
          <div className="error-detail">{error}</div>
        </div>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="app">
        <div className="status">Loading…</div>
      </div>
    );
  }

  const historical = data.filter((r) => !r.is_current);
  const current = data.find((r) => r.is_current);
  const allRows = current ? [...historical, current] : historical;

  // Two parallel value fields drive two <Line> series:
  //   historicalValue → solid line over the 6 historical points (last point
  //                     also feeds projectedValue so the dashed segment has
  //                     somewhere to start).
  //   projectedValue  → dashed line from the last historical point to the
  //                     current point; null elsewhere.
  const chartData: ChartPoint[] = [
    ...historical.map((r, i) => ({
      isoMonth: r.month,
      label: formatMonth(r.month, { short: true }),
      value: r.mrr_amount,
      isCurrent: false,
      historicalValue: r.mrr_amount,
      projectedValue: i === historical.length - 1 ? r.mrr_amount : null,
    })),
    ...(current
      ? [{
          isoMonth: current.month,
          label: formatMonth(current.month, { short: true }),
          value: current.mrr_amount,
          isCurrent: true,
          historicalValue: null,
          projectedValue: current.mrr_amount,
        }]
      : []),
  ];

  return (
    <div className="app">
      <div className="header">
        <h1>MRR</h1>
        <div className="subtitle">Updated {formatToday()}</div>
      </div>

      <div className="section">
        <div className="card chart-card">
          <ResponsiveContainer width="100%" height={350}>
            <LineChart
              data={chartData}
              margin={{ top: 12, right: 24, left: 8, bottom: 8 }}
            >
              <CartesianGrid stroke={GRID} strokeDasharray="3 3" vertical={false} />
              <XAxis
                dataKey="label"
                stroke={TEXT_MUTED}
                tick={{ fill: TEXT_MUTED, fontSize: 12 }}
                tickLine={false}
                axisLine={{ stroke: GRID }}
              />
              <YAxis
                stroke={TEXT_MUTED}
                tick={{ fill: TEXT_MUTED, fontSize: 12 }}
                tickLine={false}
                axisLine={{ stroke: GRID }}
                tickFormatter={(v: number) =>
                  formatDollars(v, { decimals: 0 })
                }
                width={72}
              />
              <Tooltip
                content={<ChartTooltip />}
                cursor={{ stroke: GRID, strokeDasharray: "3 3" }}
              />
              <Line
                type="monotone"
                dataKey="historicalValue"
                stroke={ACCENT}
                strokeWidth={2}
                dot={{ fill: ACCENT, r: 4 }}
                activeDot={{ fill: ACCENT, r: 6 }}
                connectNulls={false}
              />
              <Line
                type="monotone"
                dataKey="projectedValue"
                stroke={ACCENT}
                strokeWidth={2}
                strokeDasharray="5 5"
                dot={<ProjectedDot />}
                activeDot={{ fill: ACCENT, r: 6 }}
                connectNulls={false}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="section">
        <div className="card table-card">
          <table>
            <thead>
              <tr>
                <th>Month</th>
                {allRows.map((r) => (
                  <th key={r.month}>
                    {formatMonth(r.month, { short: true })}
                    {r.is_current && "*"}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>MRR</td>
                {allRows.map((r) => (
                  <td
                    key={r.month}
                    className={r.is_current ? "current-cell" : undefined}
                  >
                    {formatDollars(r.mrr_amount)}
                  </td>
                ))}
              </tr>
            </tbody>
          </table>
          {current && (
            <div className="table-footnote">
              * in-progress month, run-rate as of today
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default App;
