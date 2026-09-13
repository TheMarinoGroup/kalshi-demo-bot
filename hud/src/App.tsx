import { useHud } from "./useHud";
import type { HudSnapshot, Tone, UtilCell, WindowCard } from "./types";

function money(n: number | null | undefined, digits = 2): string {
  if (n == null || Number.isNaN(n)) return "—";
  const sign = n < 0 ? "-" : "";
  return `${sign}$${Math.abs(n).toFixed(digits)}`;
}

function px(n: number | null | undefined): string {
  if (n == null) return "—";
  return n.toFixed(2);
}

function pct(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  return `${(n * 100).toFixed(0)}%`;
}

function countdown(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const m = Math.floor(s / 60);
  return `${String(m).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

function signedCountdown(seconds: number): string {
  const sign = seconds < 0 ? "+" : "";
  return `${sign}${countdown(Math.abs(seconds))}`;
}

function cls(...parts: Array<string | false | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

function pnlClass(n: number | null | undefined): string {
  if (n == null || n === 0) return "neutral";
  return n > 0 ? "up" : "down";
}

function Spark({ points }: { points: { mid: number }[] }) {
  if (points.length < 2) {
    return <div className="spark empty">NO TAPE</div>;
  }
  const w = 320;
  const h = 72;
  const mids = points.map((p) => p.mid);
  const min = Math.min(...mids);
  const max = Math.max(...mids);
  const span = max - min || 0.01;
  const d = mids
    .map((m, i) => {
      const x = (i / (mids.length - 1)) * w;
      const y = h - ((m - min) / span) * (h - 4) - 2;
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  const last = mids[mids.length - 1];
  const first = mids[0];
  const up = last >= first;
  return (
    <svg className={cls("spark", up ? "up" : "down")} viewBox={`0 0 ${w} ${h}`} aria-hidden>
      <path d={d} fill="none" strokeWidth="1.4" />
    </svg>
  );
}

function Meter({
  label,
  value,
  max,
  tone,
  format = "money",
  note,
}: {
  label: string;
  value: number;
  max: number;
  tone?: Tone;
  format?: "money" | "count";
  note?: string;
}) {
  const raw = max > 0 ? value / max : 0;
  const pctBar = Math.min(100, Math.max(0, raw * 100));
  const auto: Tone = raw >= 1 ? "red" : raw >= 0.8 ? "amber" : "green";
  const shown =
    format === "count" ? `${value} / ${max}` : `${money(value)} / ${money(max)}`;
  return (
    <div className="meter">
      <div className="meter-head">
        <span>{label}</span>
        <span className={cls("tone", tone ?? auto)}>
          {shown} · {pct(raw)}
        </span>
      </div>
      <div className="meter-track">
        <div className={cls("meter-fill", tone ?? auto)} style={{ width: `${pctBar}%` }} />
      </div>
      {note ? <div className="meter-note">{note}</div> : null}
    </div>
  );
}

function dash(n: number | null | undefined, digits = 2): string {
  if (n == null || Number.isNaN(n)) return "—";
  return n.toFixed(digits);
}

function UtilChip({ cell }: { cell: UtilCell }) {
  const count = cell.label.includes("window");
  return (
    <div className={cls("util-chip", cell.tone)}>
      <span>{cell.label.toUpperCase()}</span>
      <b>
        {count
          ? `${cell.value} / ${cell.max}`
          : `${money(cell.value, cell.max >= 10 ? 0 : 2)} / ${money(cell.max, 0)}`}
      </b>
      <div className="util-track">
        <div style={{ width: `${Math.min(100, cell.util * 100)}%` }} />
      </div>
    </div>
  );
}

function WindowPanel({ win }: { win: WindowCard }) {
  const gateBad = win.gate_violation || (win.last_60s && win.new_risk_allowed);
  const zone = (win.ttc_zone ?? (win.last_60s ? "RED" : "GREEN")).toLowerCase();
  const cfb = win.cfb;
  return (
    <article
      className={cls(
        "win",
        `zone-${zone}`,
        win.last_60s && "toxic",
        gateBad && "breach",
        !win.live && "next",
      )}
    >
      <header>
        <div>
          <div className="win-series">{win.series}</div>
          <div className="win-ticker">{win.ticker}</div>
        </div>
        <div className={cls("clock", `zone-${zone}`, win.last_60s && "warn", gateBad && "breach")}>
          <span>TTC · {win.ttc_zone ?? "—"}</span>
          <strong>{countdown(win.seconds_to_close)}</strong>
        </div>
      </header>
      <div className="book">
        <div>
          <label>YES BID</label>
          <b className="up">{px(win.yes_bid)}</b>
          <em>×{dash(win.yes_bid_sz, 0)}</em>
        </div>
        <div>
          <label>YES ASK</label>
          <b className="down">{px(win.yes_ask)}</b>
          <em>×{dash(win.yes_ask_sz, 0)}</em>
        </div>
        <div>
          <label>NO BID</label>
          <b className="up">{px(win.no_bid)}</b>
          <em>×{dash(win.no_bid_sz, 0)}</em>
        </div>
        <div>
          <label>NO ASK</label>
          <b className="down">{px(win.no_ask)}</b>
          <em>×{dash(win.no_ask_sz, 0)}</em>
        </div>
        <div>
          <label>SPR</label>
          <b>{px(win.spread)}</b>
        </div>
        <div>
          <label>BID Σ / ASK Σ</label>
          <b className={win.arb ? "up" : "cyan"}>
            {px(win.bid_sum)} / {px(win.ask_sum)}
          </b>
        </div>
      </div>
      <div className="tob-meta">
        <span className={cls("pill", win.arb ? "ok" : "muted")}>{win.arb ? "ARB" : "NO ARB"}</span>
        <span>
          FLOOR {win.floor_strike != null ? win.floor_strike.toLocaleString() : "—"}
        </span>
        <span>
          CFB {cfb?.index_id ?? "—"} avg60 {dash(cfb?.avg_60s, 1)} qtr {dash(cfb?.qtr_avg, 1)}
        </span>
        <span className="muted">
          live {dash(cfb?.live, 1)} · lag {dash(cfb?.lag_ms, 0)} · {cfb?.label ?? "chart≠settle"}
        </span>
      </div>
      <Spark points={win.spark} />
      <div className="spark-note">chart ≠ settle · CFB avg60/qtr is the oracle, not live_data/spot</div>
      <footer>
        {win.gate_violation ? (
          <span className="pill breach">LAST 60s VIOLATION</span>
        ) : win.last60s_lock || win.last_60s ? (
          <span className="pill warn">LAST 60s — NO NEW RISK</span>
        ) : (
          <span className="pill ok">NEW RISK ALLOWED</span>
        )}
        <span className="muted">
          {win.open?.slice(11, 19)} → {win.close?.slice(11, 19)} UTC
          {win.capital_free_at ? ` · free ${win.capital_free_at.slice(11, 19)}` : ""}
        </span>
      </footer>
    </article>
  );
}

function RiskDesk({
  snap,
  onKill,
}: {
  snap: HudSnapshot;
  onKill: () => void;
}) {
  const { risk, pnl, mode, kill, fees, gate, settle, extras } = snap;
  const last = risk.last_fill;
  const band = risk.settle_band?.length ? risk.settle_band : [60, 90];

  return (
    <aside className="col risk">
      <h2>RISK DESK v1</h2>

      <div className="panel mode-panel">
        <label>1 · MODE</label>
        <div className={cls("mode-badge", mode.hard_stop ? "hard" : mode.badge === "LIVE" ? "live" : "paper")}>
          {mode.badge}
        </div>
        <div className="panel-note">
          {mode.hard_stop
            ? "LIVE WITHOUT APPROVAL — HARD STOP"
            : mode.demo_submit
              ? "PAPER desk · demo-submit is not production LIVE"
              : "PAPER only · production LIVE requires explicit approval"}
        </div>
      </div>

      <div className="bank">
        <label>2 · BANKROLL</label>
        <strong>{money(risk.bankroll, 0)}</strong>
      </div>

      <div className="panel">
        <label>3 · CLIP / LAST FILL</label>
        <div className="kv tight">
          <div>
            <span>BAND</span>
            <b>
              {money(risk.clip_min, 0)}–{money(risk.clip_max, 0)}
            </b>
          </div>
          <div>
            <span>DEF</span>
            <b>{money(risk.clip, 0)}</b>
          </div>
        </div>
        <div className="last-fill">
          {last ? (
            <>
              <b className={cls("tone", snap.util.fill.tone)}>{money(last.notional)}</b>
              <span>
                {last.outcome.toUpperCase()} · {last.ticker.split("-").pop()} · {last.liquidity}
              </span>
            </>
          ) : (
            <span className="muted">No fills this session</span>
          )}
        </div>
      </div>

      <Meter
        label="4 · UTIL_OPEN"
        value={risk.open_notional}
        max={risk.max_open}
        tone={risk.open_tone}
        note={risk.open_notional >= risk.max_open ? `≥ ${money(risk.max_open, 0)} KILL` : undefined}
      />
      <Meter
        label="5 · UTIL_WINDOWS"
        value={risk.windows}
        max={risk.max_windows}
        format="count"
        tone={snap.util.windows.tone}
      />
      <Meter
        label="6 · UTIL_ONESIDED"
        value={risk.unpaired}
        max={risk.max_onesided}
        tone={risk.onesided_tone}
        note={
          risk.unpaired > 0
            ? `ABORT UNPAIRED · ${(risk.onesided_leg ?? "").toUpperCase()} ${risk.onesided_ticker ?? ""}`.trim()
            : "flat — no incomplete pair"
        }
      />
      <Meter
        label="7 · DAY_PNL_NET vs −KILL"
        value={Math.max(0, -risk.daily_pnl)}
        max={risk.daily_kill}
        tone={snap.util.daily_loss.tone}
        note={`net ${money(risk.day_pnl_net ?? pnl.daily)} incl. unsettled ${money(risk.unsettled_pnl)} until ${risk.unsettled_until}`}
      />

      <div className="panel">
        <label>8 · FEE DRAG</label>
        <div className="kv tight">
          <div>
            <span>TODAY</span>
            <b>{money(fees.today, 4)}</b>
          </div>
          <div>
            <span>TAKER</span>
            <b>{money(fees.taker, 4)}</b>
          </div>
          <div>
            <span>MAKER</span>
            <b className="warn">{fees.maker == null ? "—" : money(fees.maker, 4)}</b>
          </div>
          <div>
            <span>CONFIRM</span>
            <b className="warn">{fees.maker_pending_confirm ? "PEND $0" : "OK"}</b>
          </div>
        </div>
        <div className="panel-note">{fees.note}</div>
      </div>

      <div className={cls("panel", gate.violation && "breach")}>
        <label>9 · LAST-60s GATE</label>
        <div className="kv tight">
          <div>
            <span>NEW RISK</span>
            <b className={cls(gate.violation ? "down" : gate.new_risk_allowed ? "up" : "warn")}>
              {gate.violation ? "VIOLATION" : gate.new_risk_allowed ? "ALLOWED" : "BLOCKED"}
            </b>
          </div>
          <div>
            <span>GATE</span>
            <b>≤{gate.last_seconds}s</b>
          </div>
        </div>
        <ul className="gate-list">
          {gate.windows.length ? (
            gate.windows.map((w) => (
              <li key={w.ticker} className={cls(w.violation && "down", w.last_60s && !w.violation && "warn")}>
                <span>{w.series.replace("KX", "")}</span>
                <span>{signedCountdown(w.seconds_to_close)}</span>
                <span>{w.violation ? "RED" : w.new_risk_allowed ? "OK" : "HOLD"}</span>
              </li>
            ))
          ) : (
            <li className="muted">No live window</li>
          )}
        </ul>
      </div>

      <div className="panel">
        <label>10 · SETTLE BUFFER / UNLOCK</label>
        <div className="panel-note">
          <b>capital_free_at</b> = max(settlement_ts, close+{band[0]}–{band[1]}s) · recycle{" "}
          {settle.recycle_s}s · <em>NOT expected_expiration</em>
        </div>
        <ul className="gate-list">
          {settle.buffers.length ? (
            settle.buffers.map((b) => (
              <li key={b.ticker} className={cls(b.unlocked ? "up" : "cyan")}>
                <span>{b.series.replace("KX", "")}</span>
                <span>
                  {b.unlocked ? "FREE" : `T-${countdown(Math.max(0, b.seconds_to_unlock))}`}
                </span>
                <span>{(b.capital_free_at ?? b.settlement_ts)?.slice(11, 19) ?? "WAIT TS"}</span>
              </li>
            ))
          ) : (
            <li className="muted">No capital in settle lock</li>
          )}
        </ul>
      </div>

      <div className={cls("panel kill-panel", kill.active && "tripped", kill.strobe && "strobe")}>
        <label>11 · KILL SWITCH</label>
        <div className="kill-row">
          <div className={cls("kill-state", kill.active ? "tripped" : "armed")}>
            {kill.state}
          </div>
          <button
            type="button"
            className="kill-btn"
            onClick={onKill}
            disabled={kill.active}
          >
            MANUAL KILL
          </button>
        </div>
        <div className="panel-note">
          {kill.active
            ? `${(kill.code || "trip").toUpperCase()} · ${kill.reason || "latched"}`
            : "armed · reasons: loss / open / one-sided / manual"}
        </div>
      </div>

      <div className="panel extras">
        <label>EXTRAS</label>
        <div className="kv tight">
          <div>
            <span>DRAWDOWN</span>
            <b className={extras.drawdown > 0 ? "down" : "neutral"}>{money(extras.drawdown)}</b>
          </div>
          <div>
            <span>DAY HIGH</span>
            <b>{money(extras.day_high)}</b>
          </div>
          <div>
            <span>SETTLED DIR</span>
            <b>{pct(extras.settled_directional_pct)}</b>
          </div>
          <div>
            <span>MAKER-FIRST</span>
            <b className={extras.maker_first_pct >= 0.8 ? "up" : "warn"}>{pct(extras.maker_first_pct)}</b>
          </div>
          <div>
            <span>BTC MIX</span>
            <b>{money(extras.mix.BTC)}</b>
          </div>
          <div>
            <span>ETH MIX</span>
            <b>{money(extras.mix.ETH)}</b>
          </div>
        </div>
      </div>
    </aside>
  );
}

function Desk({
  snap,
  live,
  clock,
  onKill,
}: {
  snap: HudSnapshot;
  live: boolean;
  clock: Date;
  onKill: () => void;
}) {
  const { risk, pnl, mode, kill } = snap;
  const liveWins = snap.windows.filter((w) => w.live);
  const utc = clock.toISOString().slice(11, 23);

  return (
    <div className="desk">
      <div className="scan" />
      <header className="mast">
        <div className="brand">
          <span className="logo">KX</span>
          <div>
            <div className="title">KALSHI PAPER DESK</div>
            <div className="sub">CRYPTO UP/DOWN · 15M+ ONLY · SAMPLER</div>
          </div>
        </div>
        <div className="tape">
          {snap.series.map((s) => (
            <span key={s}>{s}</span>
          ))}
          <span className="dim">MIN {risk.min_window_minutes}M</span>
          <span className="dim">L={mode.latency_ms}ms</span>
          <span className="dim">RECYCLE {risk.settle_recycle_s}s</span>
        </div>
        <div className="mast-right">
          <div className="utc">
            <span>UTC</span>
            <strong>{utc}</strong>
          </div>
          <div className="badges">
            <span className={cls("badge", "mode", mode.hard_stop ? "hard" : "paper")}>{mode.badge}</span>
            <span className={cls("badge", live && "on")}>{live ? "LIVE FEED" : "SEEKING"}</span>
            <span className="badge">{mode.mock ? "MOCK" : "PROD DATA"}</span>
            <span className={cls("badge", kill.active ? "kill" : "ok")}>
              {kill.active ? `KILL ${kill.code || "ON"}` : "ARMED"}
            </span>
          </div>
        </div>
      </header>

      {mode.hard_stop ? (
        <div className="hardstop">HARD STOP · LIVE WITHOUT APPROVAL · PAPER ONLY</div>
      ) : null}
      {snap.gate.last60s_lock ? (
        <div className="norisk">NO NEW RISK · LAST-60s LOCK · FLATTEN / CANCEL ONLY</div>
      ) : null}
      {kill.unpaired_abort && !kill.active ? (
        <div className="abort-strobe">UNPAIRED ABORT · {risk.onesided_leg?.toUpperCase()} {risk.onesided_ticker ?? ""}</div>
      ) : null}
      {kill.active ? (
        <div className="killbar strobe">
          KILL SWITCH TRIPPED · {(kill.code || "latch").toUpperCase()} · {kill.reason || "ARMED"}
        </div>
      ) : null}

      <section className="util-strip" aria-label="Limit utilization">
        <span className="util-kicker">12 · LIMITS</span>
        <UtilChip cell={snap.util.fill} />
        <UtilChip cell={snap.util.open} />
        <UtilChip cell={snap.util.windows} />
        <UtilChip cell={snap.util.onesided} />
        <UtilChip cell={snap.util.daily_loss} />
      </section>

      <section className="grid">
        <RiskDesk snap={snap} onKill={onKill} />

        <main className="col books">
          <h2>TOP OF BOOK · ACTIVE 15M+</h2>
          <div className="wins">
            {liveWins.length ? (
              liveWins.map((w) => <WindowPanel key={w.ticker} win={w} />)
            ) : (
              <p className="empty">No live 15m windows. Waiting on events-first rollover.</p>
            )}
          </div>
          <h2 className="next-h">WINDOW CAROUSEL · NEXT 15M+</h2>
          <div className="carousel">
            {snap.upcoming.map((w) => (
              <div key={w.ticker} className="chip">
                <b>{w.series}</b>
                <span>{w.ticker}</span>
                <em>{w.open?.slice(11, 16)}Z</em>
              </div>
            ))}
          </div>
          <p className="desk-note">
            Sampler · 15m+ crypto Up/Down only · paper matcher joins back of book · no POST
            /portfolio/events/orders unless --demo-submit · no max-daily-notional gauge
          </p>
        </main>

        <aside className="col book2">
          <h2>INVENTORY</h2>
          <table>
            <thead>
              <tr>
                <th>TICKER</th>
                <th>YES</th>
                <th>NO</th>
                <th>UNP</th>
              </tr>
            </thead>
            <tbody>
              {snap.positions.length ? (
                snap.positions.map((p) => (
                  <tr key={p.ticker}>
                    <td>{p.ticker.replace("KXBTC15M-", "BTC ").replace("KXETH15M-", "ETH ")}</td>
                    <td>{p.yes_qty}</td>
                    <td>{p.no_qty}</td>
                    <td className={p.unpaired ? "warn" : ""}>
                      {p.unpaired} {p.unpaired_outcome ?? ""}
                    </td>
                  </tr>
                ))
              ) : (
                <tr>
                  <td colSpan={4} className="muted">
                    Flat — no incomplete pairs
                  </td>
                </tr>
              )}
            </tbody>
          </table>

          <h2>RESTING</h2>
          <table>
            <thead>
              <tr>
                <th>SIDE</th>
                <th>PX</th>
                <th>QTY</th>
                <th>LIQ</th>
              </tr>
            </thead>
            <tbody>
              {snap.resting.length ? (
                snap.resting.map((o) => (
                  <tr key={o.order_id}>
                    <td>
                      {o.outcome.toUpperCase()} · {o.ticker.split("-").pop()}
                    </td>
                    <td>{px(o.price)}</td>
                    <td>{o.remaining}</td>
                    <td>
                      <span className={cls("tag", o.liquidity)}>{o.liquidity}</span>
                    </td>
                  </tr>
                ))
              ) : (
                <tr>
                  <td colSpan={4} className="muted">
                    No resting paper
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </aside>
      </section>

      <section className="bottom">
        <div className="pnl-strip">
          <div>
            <label>REALIZED</label>
            <b className={pnlClass(pnl.realized)}>{money(pnl.realized)}</b>
          </div>
          <div>
            <label>UNREALIZED</label>
            <b className={pnlClass(pnl.unrealized)}>{money(pnl.unrealized)}</b>
          </div>
          <div>
            <label>DAY_PNL_NET</label>
            <b className={pnlClass(pnl.day_pnl_net ?? pnl.daily)}>{money(pnl.day_pnl_net ?? pnl.daily)}</b>
          </div>
          <div>
            <label>FILLS</label>
            <b>
              {pnl.fill_count}/{pnl.order_count}
            </b>
          </div>
          <div>
            <label>FILL RATE</label>
            <b>{(pnl.fill_rate * 100).toFixed(1)}%</b>
          </div>
        </div>
        <div className="fills">
          <h2>TAPE · PAPER FILLS</h2>
          <ul>
            {snap.fills.length ? (
              [...snap.fills].reverse().slice(0, 12).map((f) => (
                <li key={f.fill_id}>
                  <span className={cls("tag", f.liquidity)}>{f.liquidity}</span>
                  <span>{f.ticker}</span>
                  <span className="cyan">{f.outcome.toUpperCase()}</span>
                  <span>{px(f.price)}</span>
                  <span>×{f.count}</span>
                  <span className="muted">fee {money(f.fee, 4)}</span>
                </li>
              ))
            ) : (
              <li className="muted">Awaiting conservative matcher fills (public prints only)</li>
            )}
          </ul>
        </div>
      </section>
    </div>
  );
}

export function App() {
  const { snap, live, clock, tripKill } = useHud();
  if (!snap) {
    return (
      <div className="boot">
        <div className="brand">
          <span className="logo">KX</span>
          <div>
            <div className="title">KALSHI PAPER DESK</div>
            <div className="sub">CONNECTING TO SAMPLER…</div>
          </div>
        </div>
      </div>
    );
  }
  return (
    <Desk
      snap={snap}
      live={live}
      clock={clock}
      onKill={() => {
        if (window.confirm("Trip the paper kill switch? Entries block until restart.")) {
          void tripKill();
        }
      }}
    />
  );
}
