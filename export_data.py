#!/usr/bin/env python3
"""Export velocity picks and portfolio stats to JSON for the Moltys.AI website."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
POLY_DIR = SCRIPT_DIR.parent / "Polymarket"

PICKS_SRC = POLY_DIR / "velocity_picks.json"
MONITOR_DB = POLY_DIR / "velocity_monitor.sqlite"

DATA_DIR = SCRIPT_DIR / "data"
PICKS_DST = DATA_DIR / "picks.json"
PORTFOLIO_DST = DATA_DIR / "portfolio.json"


def _extract_bin_edge(signals):
    """Return the bin signal's edge if present, else None."""
    for s in signals:
        if s.get("source") == "bin":
            return s["edge"]
    return None


def sanitize_pick(pick):
    """Strip strategy-sensitive fields from a pick before publishing.

    Only expose bin-sourced edge (empirically grounded). Model-only
    edges are omitted.
    """
    signals = pick.get("signals", [])
    bin_edge = _extract_bin_edge(signals)

    public = {
        "market_id": pick["market_id"],
        "question": pick["question"],
        "direction": pick["direction"],
        "bin_edge": bin_edge,
        "market_implied": pick["market_implied"],
        "n_signals": pick["n_signals"],
        "score": pick["score"],
        "hours_to_resolve": pick["hours_to_resolve"],
        "signals": [
            {"source": s["source"], "direction": s["direction"], "edge": s["edge"]}
            for s in signals
            if s["source"] == "bin"
        ],
    }
    return public


def _extract_bin_edge_from_json(signals_json):
    """Parse signals_json string and return bin edge if present."""
    try:
        signals = json.loads(signals_json) if signals_json else []
    except (json.JSONDecodeError, TypeError):
        return None
    return _extract_bin_edge(signals)


def export_picks():
    """Load velocity_picks.json, publish only bin-backed picks."""
    if not PICKS_SRC.exists():
        print(f"Warning: {PICKS_SRC} not found, writing empty picks.")
        PICKS_DST.write_text("[]")
        return
    picks = json.loads(PICKS_SRC.read_text())
    # Only publish picks with empirical bin data, sorted by soonest to resolve
    bin_picks = [p for p in picks if _extract_bin_edge(p.get("signals", [])) is not None]
    bin_picks.sort(key=lambda p: p.get("hours_to_resolve", float("inf")))
    public_picks = [sanitize_pick(p) for p in bin_picks]
    PICKS_DST.write_text(json.dumps(public_picks, indent=2))
    print(f"Exported {len(public_picks)}/{len(picks)} picks (bin-backed only).")


def export_portfolio():
    """Extract portfolio stats from velocity_monitor.sqlite and write data/portfolio.json."""
    now = datetime.now(timezone.utc).isoformat()

    portfolio = {
        "updated_at": now,
        "summary": {
            "pending": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "avg_bin_edge": None,
        },
        "pending_picks": [],
        "recent_resolutions": [],
    }

    if not MONITOR_DB.exists():
        print(f"Warning: {MONITOR_DB} not found, writing empty portfolio.")
        PORTFOLIO_DST.write_text(json.dumps(portfolio, indent=2))
        return

    conn = sqlite3.connect(str(MONITOR_DB))
    conn.row_factory = sqlite3.Row

    # Load all picks and filter to bin-backed only
    all_rows = conn.execute(
        "SELECT market_id, question, direction, order_price, "
        "       mid_price, first_seen, status, pnl, resolved_at, signals_json "
        "FROM tracked_picks"
    ).fetchall()

    bin_rows = []
    for r in all_rows:
        be = _extract_bin_edge_from_json(r["signals_json"])
        if be is not None:
            d = dict(r)
            d["bin_edge"] = be
            del d["signals_json"]
            bin_rows.append(d)

    # Summary — bin-backed picks only
    pending = sum(1 for r in bin_rows if r["status"] == "pending")
    wins = sum(1 for r in bin_rows if r["status"] == "won")
    losses = sum(1 for r in bin_rows if r["status"] == "lost")
    total_pnl = sum(r["pnl"] for r in bin_rows if r["status"] in ("won", "lost") and r["pnl"])
    resolved = wins + losses
    win_rate = round(wins / resolved, 4) if resolved > 0 else 0.0
    bin_edges = [r["bin_edge"] for r in bin_rows]
    avg_bin_edge = round(sum(bin_edges) / len(bin_edges), 4) if bin_edges else None

    portfolio["summary"] = {
        "pending": pending,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_pnl": round(total_pnl, 2),
        "avg_bin_edge": avg_bin_edge,
    }

    # Pending picks — bin-backed only
    portfolio["pending_picks"] = [
        {k: r[k] for k in ("market_id", "question", "direction", "order_price",
                            "mid_price", "first_seen", "bin_edge")}
        for r in bin_rows if r["status"] == "pending"
    ]

    # Recent resolutions — bin-backed only (last 50)
    resolved_rows = sorted(
        [r for r in bin_rows if r["status"] in ("won", "lost")],
        key=lambda r: r["resolved_at"] or "", reverse=True,
    )[:50]
    portfolio["recent_resolutions"] = [
        {k: r[k] for k in ("market_id", "question", "direction", "status",
                            "pnl", "resolved_at", "bin_edge")}
        for r in resolved_rows
    ]

    conn.close()

    PORTFOLIO_DST.write_text(json.dumps(portfolio, indent=2))
    total_tracked = len(all_rows)
    bin_edge_str = f"{avg_bin_edge:.1%}" if avg_bin_edge is not None else "n/a"
    print(
        f"Exported portfolio: {len(bin_rows)}/{total_tracked} bin-backed, "
        f"{pending} pending, {wins}W/{losses}L, "
        f"PnL={total_pnl:+.2f}, avg bin edge={bin_edge_str}"
    )


def main():
    DATA_DIR.mkdir(exist_ok=True)
    export_picks()
    export_portfolio()
    print(f"Done. Data written to {DATA_DIR}")


if __name__ == "__main__":
    main()
