import { describe, expect, it } from "vitest";
import { formatMbps, formatMeasurement, formatNumber } from "./format";

describe("display-only number formatting", () => {
  it("formats integer and ordinary fractional Mbps values without unnecessary zeros", () => {
    expect(formatMbps(10)).toBe("10");
    expect(formatMbps(2.5)).toBe("2.5");
    expect(formatMbps(2.9971602331951326)).toBe("3.00");
  });

  it("keeps up to three useful decimal places for sub-one Mbps values", () => {
    expect(formatMbps(0.125)).toBe("0.125");
    expect(formatMbps(0.5)).toBe("0.5");
    expect(formatMbps(0.1259)).toBe("0.126");
  });

  it("trims trailing zeros", () => {
    expect(formatNumber(3.5, 2)).toBe("3.5");
    expect(formatNumber(3.0, 3)).toBe("3");
    expect(formatMbps(2.999)).toBe("3.00");
  });

  it.each([null, undefined, Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY])(
    "labels missing or invalid value %s as unavailable",
    (value) => {
      expect(formatMbps(value)).toBe("Unavailable");
      expect(formatMeasurement(value, "ms")).toBe("Unavailable");
    },
  );

  it("uses unit-appropriate precision for latency, percentages, and counts", () => {
    expect(formatMeasurement(20.6309, "ms")).toBe("20.631 ms");
    expect(formatMeasurement(29.976, "%")).toBe("29.98 %");
    expect(formatMeasurement(4, "packets")).toBe("4 packets");
  });
});
