import type { FillRow, HudSnapshot, PnlPoint } from "./types";

/** Overlay demo fills + equity marks on a live snapshot. Paper/demo only. */
export function withPreviewActivity(snap: HudSnapshot): HudSnapshot {
  const now = Date.now();
  const curve: PnlPoint[] = Array.from({ length: 56 }, (_, i) => {
    const t = now - (55 - i) * 12_000;
    const wave = Math.sin(i / 7) * 0.35;
    const drift = i * 0.018;
    const daily = Number((wave + drift - 0.2).toFixed(4));
    return {
      t,
      daily,
      realized: Number((daily * 0.72).toFixed(4)),
      unrealized: Number((daily * 0.28).toFixed(4)),
      fill_count: Math.floor(i / 7),
    };
  });
  const last = curve[curve.length - 1];
  const live = snap.windows.filter((w) => w.live);
  const tickers = live.length ? live.map((w) => w.ticker) : ["KXBTC15M-MOCK", "KXETH15M-MOCK"];
  const fills: FillRow[] = Array.from({ length: 18 }, (_, i) => {
    const t = now - (17 - i) * 38_000;
    const ticker = tickers[i % tickers.length];
    const yes = i % 3 !== 1;
    const price = Number((0.46 + (i % 5) * 0.01).toFixed(2));
    const count = 10 + (i % 4) * 5;
    return {
      fill_id: `preview-${i}`,
      ticker,
      outcome: yes ? "yes" : "no",
      price,
      count,
      notional: Number((price * count).toFixed(2)),
      fee: i % 4 === 3 ? 0.0175 : 0,
      liquidity: i % 4 === 3 ? "taker" : "maker",
      ts_ms: t,
    };
  });
  const lastFill = fills[fills.length - 1];
  return {
    ...snap,
    risk: {
      ...snap.risk,
      last_fill: {
        fill_id: lastFill.fill_id,
        ticker: lastFill.ticker,
        outcome: lastFill.outcome,
        price: lastFill.price,
        count: lastFill.count,
        notional: lastFill.notional ?? lastFill.price * lastFill.count,
        fee: lastFill.fee,
        liquidity: lastFill.liquidity,
        ts_ms: lastFill.ts_ms,
      },
    },
    extras: {
      ...snap.extras,
      day_high: Math.max(...curve.map((p) => p.daily), 0.4),
      drawdown: Math.max(0, Math.max(...curve.map((p) => p.daily)) - last.daily),
      maker_fills: fills.filter((f) => f.liquidity === "maker").length,
      taker_fills: fills.filter((f) => f.liquidity === "taker").length,
      maker_first_pct: fills.filter((f) => f.liquidity === "maker").length / fills.length,
    },
    pnl: {
      ...snap.pnl,
      realized: last.realized,
      unrealized: last.unrealized,
      daily: last.daily,
      day_pnl_net: last.daily,
      fill_count: fills.length,
      order_count: Math.max(snap.pnl.order_count, fills.length + 2),
      fill_rate: fills.length / Math.max(snap.pnl.order_count, fills.length + 2),
      curve,
    },
    fills,
  };
}
