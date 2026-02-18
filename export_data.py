#!/usr/bin/env python3
"""Export velocity picks and portfolio stats to JSON for the Moltys.AI website."""

import json
import re
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


def _get_aligned_bin_signal(signals, direction):
    """Return the bin signal if it agrees with the pick direction, else None."""
    for s in signals:
        if s.get("source") == "bin" and s.get("direction") == direction:
            return s
    return None


def _parse_sample_size(detail):
    """Extract sample size from bin signal detail string like 'n=44,835'."""
    m = re.search(r"n=([\d,]+)", detail or "")
    if m:
        return int(m.group(1).replace(",", ""))
    return None


def sanitize_pick(pick):
    """Strip strategy-sensitive fields from a pick before publishing.

    Only expose bin-sourced edge where bin direction agrees with pick direction.
    Includes sample size and win rate for transparency.
    """
    signals = pick.get("signals", [])
    bin_sig = _get_aligned_bin_signal(signals, pick["direction"])

    public = {
        "market_id": pick["market_id"],
        "question": pick["question"],
        "direction": pick["direction"],
        "bin_edge": bin_sig["edge"],
        "bin_win_rate": bin_sig.get("win_rate"),
        "bin_n": _parse_sample_size(bin_sig.get("detail")),
        "market_implied": pick["market_implied"],
        "n_signals": pick["n_signals"],
        "score": pick["score"],
        "hours_to_resolve": pick["hours_to_resolve"],
    }
    return public


def _get_aligned_bin_from_json(signals_json, direction):
    """Parse signals_json and return aligned bin signal dict, or None."""
    try:
        signals = json.loads(signals_json) if signals_json else []
    except (json.JSONDecodeError, TypeError):
        return None
    return _get_aligned_bin_signal(signals, direction)


def export_picks():
    """Load velocity_picks.json, publish only aligned bin-backed picks."""
    if not PICKS_SRC.exists():
        print(f"Warning: {PICKS_SRC} not found, writing empty picks.")
        PICKS_DST.write_text("[]")
        return
    picks = json.loads(PICKS_SRC.read_text())
    # Only publish picks where bin signal agrees with pick direction
    aligned = [
        p for p in picks
        if _get_aligned_bin_signal(p.get("signals", []), p["direction"]) is not None
    ]
    aligned.sort(key=lambda p: p.get("hours_to_resolve", float("inf")))
    public_picks = [sanitize_pick(p) for p in aligned]
    PICKS_DST.write_text(json.dumps(public_picks, indent=2))
    bin_total = sum(1 for p in picks if any(s["source"] == "bin" for s in p.get("signals", [])))
    print(
        f"Exported {len(public_picks)}/{len(picks)} picks "
        f"({bin_total} bin-backed, {len(public_picks)} aligned)."
    )


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

    # Load all picks and filter to aligned bin-backed only
    all_rows = conn.execute(
        "SELECT market_id, question, direction, order_price, "
        "       mid_price, first_seen, status, pnl, resolved_at, signals_json "
        "FROM tracked_picks"
    ).fetchall()

    aligned_rows = []
    for r in all_rows:
        bin_sig = _get_aligned_bin_from_json(r["signals_json"], r["direction"])
        if bin_sig is not None:
            d = dict(r)
            d["bin_edge"] = bin_sig["edge"]
            d["bin_n"] = _parse_sample_size(bin_sig.get("detail"))
            del d["signals_json"]
            aligned_rows.append(d)

    # Summary — aligned bin-backed picks only
    pending = sum(1 for r in aligned_rows if r["status"] == "pending")
    wins = sum(1 for r in aligned_rows if r["status"] == "won")
    losses = sum(1 for r in aligned_rows if r["status"] == "lost")
    total_pnl = sum(r["pnl"] for r in aligned_rows if r["status"] in ("won", "lost") and r["pnl"])
    resolved = wins + losses
    win_rate = round(wins / resolved, 4) if resolved > 0 else 0.0
    bin_edges = [r["bin_edge"] for r in aligned_rows]
    avg_bin_edge = round(sum(bin_edges) / len(bin_edges), 4) if bin_edges else None

    portfolio["summary"] = {
        "pending": pending,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_pnl": round(total_pnl, 2),
        "avg_bin_edge": avg_bin_edge,
    }

    # Pending picks — aligned bin-backed only
    portfolio["pending_picks"] = [
        {k: r[k] for k in ("market_id", "question", "direction", "order_price",
                            "mid_price", "first_seen", "bin_edge", "bin_n")}
        for r in aligned_rows if r["status"] == "pending"
    ]

    # Recent resolutions — aligned bin-backed only (last 50)
    resolved_rows = sorted(
        [r for r in aligned_rows if r["status"] in ("won", "lost")],
        key=lambda r: r["resolved_at"] or "", reverse=True,
    )[:50]
    portfolio["recent_resolutions"] = [
        {k: r[k] for k in ("market_id", "question", "direction", "status",
                            "pnl", "resolved_at", "bin_edge", "bin_n")}
        for r in resolved_rows
    ]

    conn.close()

    PORTFOLIO_DST.write_text(json.dumps(portfolio, indent=2))
    total_tracked = len(all_rows)
    bin_edge_str = f"{avg_bin_edge:.1%}" if avg_bin_edge is not None else "n/a"
    print(
        f"Exported portfolio: {len(aligned_rows)}/{total_tracked} aligned bin-backed, "
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
