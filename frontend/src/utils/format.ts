export type DisplayNumber = number | null | undefined;

export function formatNumber(value: DisplayNumber, maximumFractionDigits = 2): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "Unavailable";
  const digits = Math.max(0, Math.min(20, Math.trunc(maximumFractionDigits)));
  return new Intl.NumberFormat("en-US", {
    useGrouping: false,
    maximumFractionDigits: digits,
  }).format(value);
}

export function formatMbps(value: DisplayNumber): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "Unavailable";
  const maximumFractionDigits = Math.abs(value) < 1 ? 3 : 2;
  const rawFractionDigits = value.toString().split(".")[1]?.length ?? 0;
  return new Intl.NumberFormat("en-US", {
    useGrouping: false,
    minimumFractionDigits: Math.min(maximumFractionDigits, rawFractionDigits),
    maximumFractionDigits,
  }).format(value);
}

export function formatMeasurement(value: DisplayNumber, unit: string): string {
  const digits = unit === "Mbps" ? null : unit === "ms" ? 3 : unit === "packets" ? 0 : 2;
  const formatted = digits === null ? formatMbps(value) : formatNumber(value, digits);
  return formatted === "Unavailable" ? formatted : `${formatted} ${unit}`;
}
