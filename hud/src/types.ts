export type SparkPoint = { t: number; mid: number; spread: number };

export type WindowCard = {
  ticker: string;
  event: string;
  series: string;
  title: string;
  status: string;
  open: string | null;
  close: string | null;
  seconds_to_close: number;
  last_60s: boolean;
  live: boolean;
  yes_bid: number | null;
  yes_ask: number | null;
  no_bid: number | null;
  no_ask: number | null;
  mid: number | null;
  spread: number | null;
  spark: SparkPoint[];
};

export type HudSnapshot = {
  v: number;
  ts: string;
  mode: {
    env: string;
    dry_run: boolean;
    paper_tape: boolean;
    live_submit: boolean;
    mock: boolean;
    latency_ms: number;
  };
  kill: { active: boolean; reason: string };
  risk: {
    bankroll: number;
    clip: number;
    open_notional: number;
    max_open: number;
    unpaired: number;
    max_onesided: number;
    daily_pnl: number;
    daily_kill: number;
    windows: number;
    max_windows: number;
    last_seconds: number;
    settle_recycle_s: number;
    min_window_minutes: number;
  };
  pnl: {
    realized: number;
    unrealized: number;
    fees: number;
    daily: number;
    fill_count: number;
    order_count: number;
    fill_rate: number;
    open_util: number;
    onesided_util: number;
    daily_loss_util: number;
  };
  windows: WindowCard[];
  upcoming: { ticker: string; series: string; open: string | null; close: string | null }[];
  positions: {
    ticker: string;
    yes_qty: number;
    no_qty: number;
    yes_avg: number | null;
    no_avg: number | null;
    unpaired: number;
    unpaired_outcome: string | null;
    unpaired_notional: number;
    locked_pnl: number;
    fees: number;
  }[];
  resting: {
    order_id: string;
    ticker: string;
    outcome: string;
    price: number;
    remaining: number;
    liquidity: string;
  }[];
  fills: {
    fill_id: string;
    ticker: string;
    outcome: string;
    price: number;
    count: number;
    fee: number;
    liquidity: string;
    ts_ms: number;
  }[];
  series: string[];
};
