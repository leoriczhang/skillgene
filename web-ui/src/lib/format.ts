// Shared formatting helpers, mirroring the original console.html logic.

export function fmtTime(t?: string | null): string {
  if (!t) return "—";
  const d = new Date(t);
  return isNaN(d.getTime())
    ? String(t)
    : d.toLocaleString("zh-CN", { hour12: false });
}

export function fmtScore(v?: number | null, digits = 3): string {
  if (v == null || isNaN(Number(v))) return "—";
  return Number(v).toFixed(digits);
}

/** Classify a score against an optional threshold: "good" | "bad" | "". */
export function scoreClass(v?: number | null, threshold?: number | null): string {
  if (v == null || isNaN(Number(v))) return "muted";
  if (threshold == null) return "";
  return Number(v) >= threshold ? "good" : "bad";
}
