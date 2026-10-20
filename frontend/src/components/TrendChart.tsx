import {
  Area,
  AreaChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
const COLORS = ["#35231C", "#704B3A", "#A88970", "#C8AD94"];

export interface TelemetryChartPoint {
  time: string;
  [key: string]: string | number | undefined;
}

interface TrendChartProps {
  title: string;
  subtitle: string;
  data: TelemetryChartPoint[];
  series: { key: string; label: string; color?: string }[];
  unit: string;
  area?: boolean;
  caption: string;
  emptyMessage: string;
}

export function TrendChart({
  title,
  subtitle,
  data,
  series,
  unit,
  area = false,
  caption,
  emptyMessage,
}: TrendChartProps) {
  const Chart = area ? AreaChart : LineChart;
  const hasPlottableData = series.some((item) => data.some((point) => typeof point[item.key] === "number"));
  return (
    <article className="chart-card card">
      <div className="section-heading section-heading--compact">
        <div>
          <h3>{title}</h3>
          <p>{subtitle}</p>
        </div>
        <span className="unit-label">{unit}</span>
      </div>
      {data.length && hasPlottableData ? (
        <div className="chart-frame" role="img" aria-label={`${title}, validated recorded telemetry at ${data.map((point) => point.time).join(", ")}`}>
          <ResponsiveContainer width="100%" height="100%">
            <Chart data={data} margin={{ top: 10, right: 8, left: -18, bottom: 0 }}>
              <CartesianGrid stroke="#D6BFA9" strokeDasharray="3 5" vertical={false} />
              <XAxis dataKey="time" tickLine={false} axisLine={false} tick={{ fill: "#704B3A", fontSize: 11 }} />
              <YAxis tickLine={false} axisLine={false} tick={{ fill: "#704B3A", fontSize: 11 }} />
              <Tooltip
                contentStyle={{ border: "1px solid #704B3A", borderRadius: 10, background: "#F4EDE4" }}
                labelStyle={{ color: "#35231C", fontWeight: 700 }}
                formatter={(value) => [`${value} ${unit}`, ""]}
              />
              {series.map((item, index) => area ? (
                <Area
                  key={item.key}
                  type="monotone"
                  dataKey={item.key}
                  name={item.label}
                  stroke={item.color ?? COLORS[index % COLORS.length]}
                  fill={item.color ?? COLORS[index % COLORS.length]}
                  fillOpacity={0.08}
                  strokeWidth={2}
                  dot={data.length === 1}
                  activeDot={{ r: 4, fill: "#35231C" }}
                />
              ) : (
                <Line
                  key={item.key}
                  type="monotone"
                  dataKey={item.key}
                  name={item.label}
                  stroke={item.color ?? COLORS[index % COLORS.length]}
                  strokeWidth={2}
                  dot={data.length === 1}
                  activeDot={{ r: 4, fill: "#35231C" }}
                />
              ))}
            </Chart>
          </ResponsiveContainer>
        </div>
      ) : (
        <div className="empty-chart" role="status">
          <div className="empty-chart__icon" aria-hidden="true">—</div>
          <strong>No measurement plotted</strong>
          <span>{emptyMessage}</span>
        </div>
      )}
      <div className="chart-legend">
        {series.map((item, index) => (
          <span key={item.key}>
            <i style={{ backgroundColor: item.color ?? COLORS[index % COLORS.length] }} />
            {item.label}
          </span>
        ))}
        <span className="chart-caption">{caption}</span>
      </div>
    </article>
  );
}
