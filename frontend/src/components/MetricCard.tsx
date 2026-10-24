import type { LucideIcon } from "lucide-react";

interface MetricCardProps {
  title: string;
  value: string;
  unit?: string;
  note: string;
  icon: LucideIcon;
  demo?: boolean;
}

export function MetricCard({
  title,
  value,
  unit,
  note,
  icon: Icon,
  demo = false,
}: MetricCardProps) {
  return (
    <article className="metric-card card">
      <div className="metric-card__top">
        <span>{title}</span>
        <span className="icon-tile" aria-hidden="true"><Icon size={17} /></span>
      </div>
      <div className="metric-card__value">
        {value}
        {unit && <span className="metric-card__unit">{unit}</span>}
      </div>
      <div className="metric-card__note">
        {demo && <span className="tiny-demo">Demo</span>}
        {note}
      </div>
    </article>
  );
}
