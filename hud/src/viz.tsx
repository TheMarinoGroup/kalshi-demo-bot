import { useEffect, useRef, useState } from "react";
import { cls, money, pnlClass, px, shortTicker, utcMs } from "./format";
import type { FillRow, HudSnapshot, PnlPoint, SparkPoint } from "./types";

function nearestY(points: { x: number; y: number }[], x: number): number {
  if (!points.length) return 0;
  let best = points[0];
  let dist = Math.abs(points[0].x - x);
  for (const p of points) {
    const d = Math.abs(p.x - x);
    if (d < dist) {
      best = p;
      dist = d;
    }
  }
  return best.y;
}

export function Spark({
  points,
  fills = [],
}: {
  points: SparkPoint[];
  fills?: FillRow[];
}) {
  if (points.length < 2) {
    return <div className="spark empty">NO TAPE</div>;
  }
  const w = 320;
  const h = 72;
  const mids = points.map((p) => p.mid);
  const min = Math.min(...mids);
  const max = Math.max(...mids);
  const span = max - min || 0.01;
  const coords = mids.map((m, i) => {
    const x = (i / (mids.length - 1)) * w;
    const y = h - ((m - min) / span) * (h - 8) - 4;
    return { x, y };
  });
  const d = coords
    .map((c, i) => `${i === 0 ? "M" : "L"}${c.x.toFixed(1)},${c.y.toFixed(1)}`)
    .join(" ");
  const area = `${d} L${w},${h} L0,${h} Z`;
  const last = mids[mids.length - 1];
  const first = mids[0];
  const up = last >= first;
  const t0 = points[0].t;
  const t1 = points[points.length - 1].t;
  const tSpan = t1 - t0 || 1;
  const lastPt = coords[coords.length - 1];
  const marks = fills
    .filter((f) => f.ts_ms >= t0 && f.ts_ms <= t1 + 250)
    .map((f) => {
      const x = ((f.ts_ms - t0) / tSpan) * w;
      return {
        id: f.fill_id,
        x,
        y: nearestY(coords, x),
        maker: f.liquidity === "maker",
        yes: f.outcome.toLowerCase() === "yes",
      };
    });
  return (
    <svg className={cls("spark", up ? "up" : "down")} viewBox={`0 0 ${w} ${h}`} aria-hidden>
      <path className="spark-area" d={area} />
      <path className="spark-line" d={d} fill="none" strokeWidth="1.5" />
      {marks.map((m) => (
        <circle
          key={m.id}
          className={cls("spark-fill", m.maker ? "maker" : "taker", m.yes ? "yes" : "no")}
          cx={m.x.toFixed(1)}
          cy={m.y.toFixed(1)}
          r="2.4"
        />
      ))}
      <circle className="spark-last" cx={lastPt.x} cy={lastPt.y} r="2.6" />
    </svg>
  );
}

export function EquityCurve({
  points,
  fills,
}: {
  points: PnlPoint[];
  fills: FillRow[];
}) {
  const w = 640;
  const h = 148;
  const padL = 8;
  const padR = 8;
  const padT = 10;
  const padB = 16;
  if (points.length < 2) {
    return (
      <div className="equity empty" role="img" aria-label="Session equity curve empty">
        AWAITING SESSION MARKS
      </div>
    );
  }
  const values = points.map((p) => p.daily);
  let min = Math.min(0, ...values);
  let max = Math.max(0, ...values);
  if (min === max) {
    min -= 0.05;
    max += 0.05;
  }
  const span = max - min;
  const innerW = w - padL - padR;
  const innerH = h - padT - padB;
  const t0 = points[0].t;
  const t1 = points[points.length - 1].t;
  const tSpan = t1 - t0 || 1;
  const xy = (t: number, v: number) => ({
    x: padL + ((t - t0) / tSpan) * innerW,
    y: padT + (1 - (v - min) / span) * innerH,
  });
  const coords = points.map((p) => xy(p.t, p.daily));
  const d = coords
    .map((c, i) => `${i === 0 ? "M" : "L"}${c.x.toFixed(1)},${c.y.toFixed(1)}`)
    .join(" ");
  const zero = xy(t0, 0).y;
  const last = coords[coords.length - 1];
  const up = points[points.length - 1].daily >= 0;
  const area = `${d} L${last.x.toFixed(1)},${zero.toFixed(1)} L${coords[0].x.toFixed(1)},${zero.toFixed(1)} Z`;
  const marks = fills
    .filter((f) => f.ts_ms >= t0 && f.ts_ms <= t1 + 400)
    .map((f) => {
      const pt = xy(f.ts_ms, 0);
      return {
        id: f.fill_id,
        x: pt.x,
        y: nearestY(coords, pt.x),
        maker: f.liquidity === "maker",
      };
    });
  return (
    <svg
      className={cls("equity", up ? "up" : "down")}
      viewBox={`0 0 ${w} ${h}`}
      role="img"
      aria-label="Session equity curve"
    >
      <line className="equity-zero" x1={padL} x2={w - padR} y1={zero} y2={zero} />
      <path className="equity-area" d={area} />
      <path className="equity-line" d={d} fill="none" strokeWidth="1.7" />
      {marks.map((m) => (
        <rect
          key={m.id}
          className={cls("equity-fill", m.maker ? "maker" : "taker")}
          x={(m.x - 2.2).toFixed(1)}
          y={(m.y - 2.2).toFixed(1)}
          width="4.4"
          height="4.4"
          transform={`rotate(45 ${m.x.toFixed(1)} ${m.y.toFixed(1)})`}
        />
      ))}
      <circle className="equity-last" cx={last.x} cy={last.y} r="3" />
    </svg>
  );
}

export function PnlCard({ snap }: { snap: HudSnapshot }) {
  const { pnl, extras } = snap;
  const net = pnl.day_pnl_net ?? pnl.daily;
  const curve = pnl.curve ?? [];
  const minutes =
    curve.length >= 2 ? Math.max(1, Math.round((curve[curve.length - 1].t - curve[0].t) / 60_000)) : 0;
  return (
    <article className="card pnl-card">
      <header className="card-head">
        <h2>SESSION P&amp;L</h2>
        <span className="card-meta">
          {minutes ? `${minutes}m window` : "live marks"} · equity + fill ticks
        </span>
      </header>
      <div className="pnl-stats">
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
          <b className={pnlClass(net)}>{money(net)}</b>
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
        <div>
          <label>FEES</label>
          <b>{money(pnl.fees, 4)}</b>
        </div>
      </div>
      <EquityCurve points={curve} fills={snap.fills} />
      <footer className="card-foot">
        <span>
          HIGH <b>{money(extras.day_high)}</b>
        </span>
        <span>
          DD <b className={extras.drawdown > 0 ? "down" : "neutral"}>{money(extras.drawdown)}</b>
        </span>
        <span>
          MTM incl. unsettled until <b>settlement_ts</b>
        </span>
      </footer>
    </article>
  );
}

export function TapeCard({ fills }: { fills: FillRow[] }) {
  const [fresh, setFresh] = useState<Set<string>>(() => new Set());
  const seen = useRef<Set<string>>(new Set());
  const primed = useRef(false);
  const rows = [...fills].reverse();

  useEffect(() => {
    if (!primed.current) {
      fills.forEach((f) => seen.current.add(f.fill_id));
      primed.current = true;
      return;
    }
    const next = fills.filter((f) => !seen.current.has(f.fill_id)).map((f) => f.fill_id);
    if (!next.length) return;
    next.forEach((id) => seen.current.add(id));
    setFresh((prev) => new Set([...prev, ...next]));
    const timer = window.setTimeout(() => {
      setFresh((prev) => {
        const copy = new Set(prev);
        next.forEach((id) => copy.delete(id));
        return copy;
      });
    }, 1800);
    return () => window.clearTimeout(timer);
  }, [fills]);

  return (
    <article className="card tape-card">
      <header className="card-head">
        <h2>TAPE · PAPER FILLS</h2>
        <span className="card-meta">{fills.length ? `${fills.length} prints` : "public prints only"}</span>
      </header>
      <div className="tape-scroll" role="log" aria-live="polite" aria-label="Paper fill tape">
        <ul>
          {rows.length ? (
            rows.map((f) => (
              <li key={f.fill_id} className={cls("tape-row", fresh.has(f.fill_id) && "fresh")}>
                <time>{utcMs(f.ts_ms)}</time>
                <span className={cls("tag", f.liquidity)}>{f.liquidity}</span>
                <span className="tape-sym">{shortTicker(f.ticker)}</span>
                <span className="cyan">{f.outcome.toUpperCase()}</span>
                <span>{px(f.price)}</span>
                <span>×{f.count}</span>
                <span className={cls("tape-notional", pnlClass(f.notional))}>
                  {money(f.notional ?? f.price * f.count)}
                </span>
                <span className="muted">fee {money(f.fee, 4)}</span>
              </li>
            ))
          ) : (
            <li className="muted tape-empty">
              Awaiting conservative matcher fills (public prints only)
            </li>
          )}
        </ul>
      </div>
    </article>
  );
}
