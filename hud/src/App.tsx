import { cls, countdown, dash, money, pct, px, shortTicker, signedCountdown } from "./format";
import { useHud } from "./useHud";
import type { FillRow, HudSnapshot, Tone, UtilCell, WindowCard } from "./types";
import { PnlCard, Spark, TapeCard } from "./viz";

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

function WindowPanel({ win, fills }: { win: WindowCard; fills: FillRow[] }) {
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
          <b className={win.arb_taker_eligible ? "up" : "cyan"}>
            {px(win.bid_sum)} / {px(win.ask_sum)}
          </b>
        </div>
      </div>
      <div className="tob-meta">
        {win.underround ? <span className="pill warn">UNDERROUND</span> : null}
        {win.arb_taker_eligible ? (
          <span className="pill ok">REGIME A · TAKER-ARB</span>
        ) : (
          <span className="pill muted">NO TAKER-ARB</span>
        )}
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
      <Spark points={win.spark} fills={fills.filter((f) => f.ticker === win.ticker)} />
      <div className="spark-note">chart ≠ settle · CFB avg60/qtr is the oracle, not live_data/spot</div>
      <footer>
        {win.gate_violation ? (
          <span className="pill breach">LAST 60s VIOLATION</span>
        ) : win.last60s_lock || win.last_60s ? (
          <span className="pill warn">LAST 60s — NO NEW RISK</span>
        ) : win.live && win.reconcile_ready === false ? (
          <span className="pill warn">NOT READY — NO NEW RISK</span>
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
  const { risk, pnl, mode, kill, fees, gate, settle, extras, reconcile } = snap;
  const last = risk.last_fill;
  const band = risk.settle_band?.length ? risk.settle_band : [60, 90];
  const reconStatus = reconcile?.status ?? (reconcile?.ready_to_trade ? "READY" : "RECONCILING");
  const reconReady = Boolean(reconcile?.ready_to_trade);

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

      <div className={cls("panel", !reconReady && "breach")}>
        <label>RECONCILE</label>
        <div className="kv tight">
          <div>
            <span>STATE</span>
            <b className={cls(reconReady ? "up" : "warn")}>{reconStatus}</b>
          </div>
          <div>
            <span>SRC</span>
            <b>{reconcile?.source || "—"}</b>
          </div>
        </div>
        <div className="panel-note">
          {reconReady
            ? `${reconcile?.source || "SYNC"} · pos ${reconcile?.position_count ?? 0} · rest ${reconcile?.resting_count ?? 0}`
            : reconcile?.error
              ? `NOT READY · ${reconcile.error}`
              : "RECONCILING · no new quotes until exchange snapshot"}
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
  const { risk, mode, kill, reconcile } = snap;
  const liveWins = snap.windows.filter((w) => w.live);
  const utc = clock.toISOString().slice(11, 23);
  const reconStatus = reconcile?.status ?? "RECONCILING";
  const reconReady = Boolean(reconcile?.ready_to_trade);

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
            <span
              className={cls(
                "badge",
                reconReady ? "ok" : reconStatus === "NOT READY" ? "kill" : "reconciling",
              )}
            >
              {reconStatus}
            </span>
            <span className={cls("badge", kill.active ? "kill" : "ok")}>
              {kill.active ? `KILL ${kill.code || "ON"}` : "ARMED"}
            </span>
          </div>
        </div>
      </header>

      {mode.hard_stop ? (
        <div className="hardstop">HARD STOP · LIVE WITHOUT APPROVAL · PAPER ONLY</div>
      ) : null}
      {!reconReady ? (
        <div className={cls("norisk", reconStatus === "NOT READY" && "not-ready")}>
          {reconStatus === "NOT READY"
            ? `NOT READY · RECONCILE FAILED · NO NEW RISK${reconcile?.error ? ` · ${reconcile.error}` : ""}`
            : "RECONCILING · NO NEW RISK · WAITING ON EXCHANGE SNAPSHOT"}
        </div>
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
              liveWins.map((w) => <WindowPanel key={w.ticker} win={w} fills={snap.fills} />)
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
                    <td>{shortTicker(p.ticker)}</td>
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

      <section className="activity" aria-label="Session PnL and paper tape">
        <PnlCard snap={snap} />
        <TapeCard fills={snap.fills} />
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
