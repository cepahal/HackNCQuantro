export function time(seconds: number | null | undefined, precise = false) {
  if (seconds == null || !Number.isFinite(seconds)) return "Not assessed";
  const sign = seconds < 0 ? "-" : "";
  if (precise) {
    const tenths = Math.round(Math.abs(seconds) * 10);
    const minutes = Math.floor(tenths / 600);
    const wholeSeconds = Math.floor((tenths % 600) / 10);
    return `${sign}${minutes}:${String(wholeSeconds).padStart(2, "0")}.${tenths % 10}`;
  }
  const whole = Math.round(Math.abs(seconds));
  return `${sign}${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}
export function compact(value: number | null | undefined) { return value == null || !Number.isFinite(value) ? "Unavailable" : Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(value); }
export function percent(value: number | null | undefined) { if (value == null || !Number.isFinite(value)) return "Not assessed"; if (value === 1) return "100%"; return `${Math.round(Math.max(0, Math.min(.99, value)) * 100)}%`; }
export function date(value: string | null | undefined) { if (!value) return "Unavailable"; const parsed = new Date(value); return Number.isNaN(parsed.valueOf()) ? "Unavailable" : new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" }).format(parsed); }
export function unique(values: string[]) { return [...new Set(values.filter(Boolean))]; }
