import { useHud } from "./useHud";
import type { HudSnapshot, WindowCard } from "./types";

function money(n: number | null | undefined, digits = 2): string {
  if (n == null || Number.isNaN(n)) return "—";
  const sign = n < 0 ? "-" : "";
  return `${sign}$${Math.abs(n).toFixed(digits)}`;
}

function px(n: number | null | undefined): string {
  if (n == null) return "—";
  return n.toFixed(2);
}

function countdown(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const m = Math.floor(s / 60);
  return `${String(m).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
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
  const w = 220;
  const h = 48;
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
  warn,
}: {
  label: string;
  value: number;
  max: number;
  warn?: boolean;
}) {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  return (
    <div className="meter">
      <div className="meter-head">
        <span>{label}</span>
        <span>
          {money(value)} <em>/ {money(max)}</em>
        </span>
      </div>
      <div className="meter-track">
        <div
          className={cls("meter-fill", pct >= 80 || warn ? "hot" : pct >= 50 ? "warm" : "ok")}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

function WindowPanel({ win }: { win: WindowCard }) {
  return (
    <article className={cls("win", win.last_60s && "toxic", !win.live && "next")}>
      <header>
        <div>
          <div className="win-series">{win.series}</div>
          <div className="win-ticker">{win.ticker}</div>
        </div>
        <div className={cls("clock", win.last_60s && "warn")}>
          <span>{win.live ? "TO CLOSE" : "OPENS"}</span>
          <strong>{countdown(win.seconds_to_close)}</strong>
        </div>
      </header>
      <div className="book">
        <div>
          <label>YES BID</label>
          <b className="up">{px(win.yes_bid)}</b>
        </div>
        <div>
          <label>YES ASK</label>
          <b className="down">{px(win.yes_ask)}</b>
        </div>
        <div>
          <label>NO BID</label>
          <b className="up">{px(win.no_bid)}</b>
        </div>
        <div>
          <label>NO ASK</label>
          <b className="down">{px(win.no_ask)}</b>
        </div>
        <div>
          <label>MID</label>
          <b className="cyan">{px(win.mid)}</b>
        </div>
        <div>
          <label>SPR</label>
          <b>{px(win.spread)}</b>
        </div>
      </div>
      <Spark points={win.spark} />
      <footer>
        {win.last_60s ? <span className="pill warn">LAST 60s — NO NEW RISK</span> : null}
        <span className="muted">
          {win.open?.slice(11, 19)} → {win.close?.slice(11, 19)} UTC
        </span>
      </footer>
    </article>
  );
}

function Desk({ snap, live, clock }: { snap: HudSnapshot; live: boolean; clock: Date }) {
  const { risk, pnl, mode, kill } = snap;
  const liveWins = snap.windows.filter((w) => w.live);
  const nextWins = snap.windows.filter((w) => !w.live).slice(0, 4);
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
            <span className={cls("badge", live && "on")}>{live ? "LIVE FEED" : "SEEKING"}</span>
            <span className="badge">{mode.paper_tape ? "PAPER TAPE" : "ORDERS"}</span>
            <span className="badge">{mode.dry_run ? "DRY-RUN" : "SUBMIT"}</span>
            <span className="badge">{mode.mock ? "MOCK" : "PROD DATA"}</span>
            <span className={cls("badge", kill.active ? "kill" : "ok")}>
              {kill.active ? "KILL ON" : "RISK OK"}
            </span>
          </div>
        </div>
      </header>

      {kill.active ? <div className="killbar">KILL SWITCH · {kill.reason || "ARMED"}</div> : null}

      <section className="grid">
        <aside className="col risk">
          <h2>RISK DESK v1</h2>
          <div className="bank">
            <label>BANKROLL</label>
            <strong>{money(risk.bankroll, 0)}</strong>
          </div>
          <Meter label="OPEN NOTIONAL" value={risk.open_notional} max={risk.max_open} />
          <Meter label="ONE-SIDED" value={risk.unpaired} max={risk.max_onesided} />
          <Meter
            label="DAILY LOSS (incl. unsettled)"
            value={Math.max(0, -risk.daily_pnl)}
            max={risk.daily_kill}
            warn={pnl.daily_loss_util >= 0.7}
          />
          <Meter
            label="WINDOWS"
            value={risk.windows}
            max={risk.max_windows}
          />
          <div className="kv">
            <div>
              <span>CLIP</span>
              <b>{money(risk.clip, 0)}</b>
            </div>
            <div>
              <span>LAST-60s</span>
              <b>{risk.last_seconds}s</b>
            </div>
            <div>
              <span>DAILY PNL</span>
              <b className={pnlClass(pnl.daily)}>{money(pnl.daily)}</b>
            </div>
            <div>
              <span>FEES</span>
              <b>{money(pnl.fees, 4)}</b>
            </div>
          </div>
        </aside>

        <main className="col books">
          <h2>TOP OF BOOK · ACTIVE 15M+</h2>
          <div className="wins">
            {liveWins.length ? liveWins.map((w) => <WindowPanel key={w.ticker} win={w} />) : (
              <p className="empty">No live 15m windows. Waiting on events-first rollover.</p>
            )}
          </div>
          <h2 className="next-h">WINDOW CAROUSEL · NEXT</h2>
          <div className="carousel">
            {(nextWins.length ? nextWins : snap.upcoming).map((w) => (
              <div key={w.ticker} className="chip">
                <b>{w.series}</b>
                <span>{w.ticker}</span>
                <em>{w.open?.slice(11, 16)}Z</em>
              </div>
            ))}
          </div>
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
            <label>SESSION</label>
            <b className={pnlClass(pnl.daily)}>{money(pnl.daily)}</b>
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
  const { snap, live, clock } = useHud();
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
  return <Desk snap={snap} live={live} clock={clock} />;
}
