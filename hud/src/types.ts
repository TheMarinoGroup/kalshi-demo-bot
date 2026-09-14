export type SparkPoint = { t: number; mid: number; spread: number };

export type Tone = "green" | "amber" | "red";

export type CfbBlock = {
  index_id: string | null;
  avg_60s: number | null;
  qtr_avg: number | null;
  live: number | null;
  lag_ms: number | null;
  oracle: boolean;
  label: string;
};

export type WindowCard = {
  ticker: string;
  event: string;
  series: string;
  title: string;
  status: string;
  open: string | null;
  close: string | null;
  settlement_ts?: string | null;
  capital_free_at?: string | null;
  expected_expiration?: string | null;
  seconds_to_close: number;
  ttc?: number;
  ttc_zone?: "GREEN" | "AMBER" | "RED";
  seconds_since_close?: number;
  last_60s: boolean;
  last60s_lock?: boolean;
  new_risk_allowed?: boolean;
  reconcile_ready?: boolean;
  gate_violation?: boolean;
  live: boolean;
  yes_bid: number | null;
  yes_ask: number | null;
  no_bid: number | null;
  no_ask: number | null;
  yes_bid_sz?: number | null;
  no_bid_sz?: number | null;
  yes_ask_sz?: number | null;
  no_ask_sz?: number | null;
  bid_sum?: number | null;
  ask_sum?: number | null;
  ask_sum_plus_fees?: number | null;
  underround?: boolean;
  arb_taker_eligible?: boolean;
  arb?: boolean;
  mid: number | null;
  spread: number | null;
  floor_strike?: number | null;
  cfb?: CfbBlock;
  spark: SparkPoint[];
};

export type UtilCell = {
  label: string;
  value: number;
  max: number;
  util: number;
  tone: Tone;
};

export type LastFill = {
  fill_id: string;
  ticker: string;
  outcome: string;
  price: number;
  count: number;
  notional: number;
  fee: number;
  liquidity: string;
  ts_ms: number;
};

export type PnlPoint = {
  t: number;
  daily: number;
  realized: number;
  unrealized: number;
  fill_count: number;
};

export type FillRow = {
  fill_id: string;
  ticker: string;
  outcome: string;
  price: number;
  count: number;
  notional?: number;
  fee: number;
  liquidity: string;
  ts_ms: number;
};

export type HudSnapshot = {
  v: number;
  ts: string;
  mode: {
    env: string;
    dry_run: boolean;
    paper_tape: boolean;
    live_submit: boolean;
    allow_production?: boolean;
    mock: boolean;
    latency_ms: number;
    badge: "PAPER" | "LIVE";
    paper_only: boolean;
    hard_stop: boolean;
    demo_submit?: boolean;
  };
  reconcile?: {
    ready_to_trade: boolean;
    status: "RECONCILING" | "READY" | "NOT READY" | string;
    source: string;
    error: string;
    attempts: number;
    position_count: number;
    resting_count: number;
    orphan_count: number;
    cancelled_orphans?: number;
    cancel_orphans: boolean;
    paper_fills_restored?: number;
    paper_quotes_restored?: number;
    next_retry_ts?: string | null;
  };
  kill: {
    active: boolean;
    state: "ARMED" | "TRIPPED";
    reason: string;
    code: string;
    strobe?: boolean;
    unpaired_abort?: boolean;
  };
  risk: {
    bankroll: number;
    clip: number;
    clip_min: number;
    clip_max: number;
    last_fill: LastFill | null;
    open_notional: number;
    max_open: number;
    open_pct: number;
    open_tone: Tone;
    unpaired: number;
    max_onesided: number;
    onesided_ticker: string | null;
    onesided_leg: string | null;
    onesided_tone: Tone;
    abort_unpaired: boolean;
    daily_pnl: number;
    day_pnl_net?: number;
    daily_kill: number;
    unsettled_pnl: number;
    unsettled_until: string;
    windows: number;
    max_windows: number;
    last_seconds: number;
    settle_recycle_s: number;
    settle_band: number[];
    min_window_minutes: number;
  };
  fees: {
    today: number;
    maker: number | null;
    taker: number;
    maker_pending_confirm: boolean;
    note: string;
  };
  gate: {
    last_seconds: number;
    last60s_lock?: boolean;
    no_new_risk?: boolean;
    new_risk_allowed: boolean;
    ready_to_trade?: boolean;
    violation: boolean;
    windows: {
      ticker: string;
      series: string;
      seconds_to_close: number;
      last_60s: boolean;
      new_risk_allowed: boolean;
      violation: boolean;
    }[];
  };
  settle: {
    lock: string;
    not_expected_expiration: boolean;
    plan_s: number[];
    recycle_s: number;
    rare_tail: boolean;
    buffers: {
      ticker: string;
      series: string;
      close: string | null;
      settlement_ts: string | null;
      capital_free_at?: string | null;
      expected_expiration: string | null;
      expected_expiration_is_lock: boolean;
      result: string | null;
      recycle_s: number;
      seconds_since_close: number;
      seconds_to_unlock: number;
      seconds_to_settlement_ts: number | null;
      unlocked: boolean;
      free_on: string;
      locked_notional: number;
    }[];
  };
  util: {
    fill: UtilCell;
    open: UtilCell;
    windows: UtilCell;
    onesided: UtilCell;
    daily_loss: UtilCell;
  };
  extras: {
    day_high: number;
    drawdown: number;
    settled_directional_pct: number;
    maker_first_pct: number;
    maker_fills: number;
    taker_fills: number;
    mix: { BTC: number; ETH: number; OTHER: number };
    quote_mode: string;
  };
  pnl: {
    realized: number;
    unrealized: number;
    fees: number;
    daily: number;
    day_pnl_net?: number;
    fill_count: number;
    order_count: number;
    fill_rate: number;
    open_util: number;
    onesided_util: number;
    daily_loss_util: number;
    curve?: PnlPoint[];
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
  fills: FillRow[];
  series: string[];
};
