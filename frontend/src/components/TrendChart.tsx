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
import type { DemoTelemetryPoint } from "../types";

const COLORS = ["#35231C", "#704B3A", "#A88970", "#C8AD94"];

interface TrendChartProps {
  title: string;
  subtitle: string;
  data: DemoTelemetryPoint[];
  series: { key: keyof DemoTelemetryPoint; label: string; color?: string }[];
  unit: string;
  area?: boolean;
}

export function TrendChart({
  title,
  subtitle,
  data,
  series,
  unit,
  area = false,
}: TrendChartProps) {
  const Chart = area ? AreaChart : LineChart;
  return (
    <article className="chart-card card">
      <div className="section-heading section-heading--compact">
        <div>
          <h3>{title}</h3>
          <p>{subtitle}</p>
        </div>
        <span className="unit-label">{unit}</span>
      </div>
      <div className="chart-frame" role="img" aria-label={`${title}, Demo Data`}>
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
                dot={false}
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
                dot={false}
                activeDot={{ r: 4, fill: "#35231C" }}
              />
            ))}
          </Chart>
        </ResponsiveContainer>
      </div>
      <div className="chart-legend">
        {series.map((item, index) => (
          <span key={item.key}>
            <i style={{ backgroundColor: item.color ?? COLORS[index % COLORS.length] }} />
            {item.label}
          </span>
        ))}
        <span className="chart-caption">Synthetic demo samples, not measurements</span>
      </div>
    </article>
  );
}
