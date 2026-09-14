export function money(n: number | null | undefined, digits = 2): string {
  if (n == null || Number.isNaN(n)) return "—";
  const sign = n < 0 ? "-" : "";
  return `${sign}$${Math.abs(n).toFixed(digits)}`;
}

export function px(n: number | null | undefined): string {
  if (n == null) return "—";
  return n.toFixed(2);
}

export function pct(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  return `${(n * 100).toFixed(0)}%`;
}

export function countdown(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const m = Math.floor(s / 60);
  return `${String(m).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

export function signedCountdown(seconds: number): string {
  const sign = seconds < 0 ? "+" : "";
  return `${sign}${countdown(Math.abs(seconds))}`;
}

export function cls(...parts: Array<string | false | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

export function pnlClass(n: number | null | undefined): string {
  if (n == null || n === 0) return "neutral";
  return n > 0 ? "up" : "down";
}

export function dash(n: number | null | undefined, digits = 2): string {
  if (n == null || Number.isNaN(n)) return "—";
  return n.toFixed(digits);
}

export function utcMs(ts: number | null | undefined): string {
  if (ts == null || ts <= 0) return "—";
  return new Date(ts).toISOString().slice(11, 23);
}

export function shortTicker(ticker: string): string {
  return ticker
    .replace("KXBTC15M-", "BTC ")
    .replace("KXETH15M-", "ETH ")
    .replace(/^KX/, "");
}
