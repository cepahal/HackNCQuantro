export function time(seconds: number | null | undefined, precise = false) {
  if (seconds == null || !Number.isFinite(seconds)) return "Not assessed";
  const whole = Math.floor(seconds);
  const base = `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
  return precise ? `${base}.${Math.round((seconds - whole) * 10)}` : base;
}
export function compact(value: number | null | undefined) { return value == null ? "Unavailable" : Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(value); }
export function percent(value: number | null | undefined) { if (value == null) return "Not assessed"; const floored = value < 1 && value >= .995 ? 99 : Math.round(value * 100); return `${value === 1 ? 100 : Math.min(99, floored)}%`; }
export function date(value: string | null | undefined) { if (!value) return "Unavailable"; const parsed = new Date(value); return Number.isNaN(parsed.valueOf()) ? "Unavailable" : new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" }).format(parsed); }
export function unique(values: string[]) { return [...new Set(values.filter(Boolean))]; }
