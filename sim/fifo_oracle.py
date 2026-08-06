"""Cost-basis dual oracle — a SECOND FIFO implementation, on purpose.

The book's lot machinery is incremental: global seq numbers, zombie lots
kept at qty 0 for Phase 5 reversals, per-event lot_ops, plan-then-commit
consumption. If a bug lives in any of that machinery, a checker built on
the same design would inherit it. So this oracle is structurally different
in every dimension that can differ while the ACCOUNTING rule stays the
same:

  * recomputed from scratch from the event log, never incremental;
  * plain per-(cid, symbol) python lists of lot records in append order,
    ordered by this oracle's OWN arrival counter — no global seq numbers,
    no cross-symbol index structure;
  * only first-delivered payloads of events that actually POSTED are read
    (event_id present in book.events) — rejected fills, duplicates, and
    unknown types contribute nothing, exactly the graded semantics.

Phase 4 taught it the three corporate actions with a lot effect; Phase 5
teaches it REVERSALS, again with its own mechanics: the oracle keeps its
own per-event op log (op records holding direct references to its own lot
objects), and on a posted reversal event it undoes the original's oracle-
ops in reverse:

  * add    -> zero the lot's remainder (qty and cost), clamped by nature —
              whatever later sells left behind is what goes;
  * consume-> restore the recorded take onto the same lot object, quantity
              scaled by (the lot's current multiplier ÷ the multiplier the
              oracle itself recorded at consumption), cost verbatim;
  * split  -> restore each touched lot's recorded prior quantity and
              divide its multiplier by the recorded ratio;
  * rekey  -> move the recorded lots back under the old symbol, from
              wherever each now lives.

One structural consequence: fully-consumed lots are no longer popped —
they stay as empty shells (qty 0, cost 0) so a later reversal can restore
in place and later splits keep their multiplier current. The sell walk
steps over them; they contribute nothing to any total.

What is deliberately IDENTICAL is the graded relief convention:
partial relief = money(lot_cost_total × sold_qty ÷ lot_qty), remainder
stays with the lot; full consumption takes the lot's entire remaining
cost (M5 — no lost cents). Same law, different machine: agreement
cent-for-cent is evidence about the law, not the machinery.

Read-only on the book it is handed.
"""
from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

from book import qnum
from tariff import money

ZERO = Decimal("0.00")
FILL_TYPES = ("order_partially_filled", "order_filled")


def check_cost_basis(book) -> list[str]:
    """Replay every posted fill, corporate action AND reversal through the
    naive FIFO above, then compare per-(cid, symbol) total quantity and
    total cost cent-for-cent against the book's lots/lot_index aggregation.
    Empty list == green."""
    out: list[str] = []
    holdings: dict = {}   # (cid, symbol) -> [lot, ...] in arrival order
    ops: dict = {}        # eid -> [op, ...] — this oracle's OWN undo log
    arrivals = 0          # oracle's OWN lot ordering — never the book's seq

    def new_lot(key, qty, cost) -> dict:
        nonlocal arrivals
        arrivals += 1
        lot = {"qty": qty, "cost": cost, "arr": arrivals,
               "mult": Fraction(1), "cid": key[0], "sym": key[1]}
        holdings.setdefault(key, []).append(lot)
        return lot

    for ev in book.event_log:
        eid = ev["event_id"]
        rec = book.events.get(eid)
        if rec is None:
            continue                      # never posted
        t = rec["type"]
        p = rec["payload"]                # the stored, first-delivered truth
        oplist = ops.setdefault(eid, [])

        if t in FILL_TYPES:
            key = (p["customer_id"], p["symbol"])
            qty = qnum(p["quantity"])
            if p["side"] == "buy":
                oplist.append(("add", new_lot(key, qty, money(p["principal"]))))
                continue
            # sell: walk by arrival. An empty shell (fully consumed or a
            # reverse-split rounding husk) yields nothing — step over it.
            remaining = qty
            for lot in sorted(holdings.get(key, []), key=lambda l: l["arr"]):
                if remaining <= 0:
                    break
                if lot["qty"] <= 0:
                    continue              # shell: cost (if any) stays put
                take = min(lot["qty"], remaining)
                if take == lot["qty"]:    # full: entire remaining cost (M5)
                    relieved = lot["cost"]
                else:                     # partial: graded relief formula
                    relieved = money(lot["cost"] * take / lot["qty"])
                lot["qty"] = qnum(lot["qty"] - take)
                lot["cost"] = lot["cost"] - relieved
                oplist.append(("consume", lot, take, relieved, lot["mult"]))
                remaining = qnum(remaining - take)
            if remaining > 0:
                # The book POSTED a sell the oracle has no shares for:
                # either the oversell pre-check failed or consumption lost
                # quantity somewhere.
                out.append(f"fifo-oracle {eid}: posted sell of {qty} "
                           f"{key[1]} for {key[0]} exceeds oracle holding "
                           f"by {remaining}")

        elif t == "dividend_reinvested":
            # A reinvest lot queues exactly like a bought one (L8).
            oplist.append(("add", new_lot((p["customer_id"], p["symbol"]),
                                          qnum(p["reinvest_quantity"]),
                                          money(p["net_amount"]))))

        elif t == "stock_split":
            # Scale qty per lot, 6 dp; cost UNCHANGED; drop nothing. The
            # exact Fraction multiplier is this oracle's own split tracking
            # for reversal-across-a-split restores.
            r_to = Decimal(str(p["ratio_to"]))
            r_from = Decimal(str(p["ratio_from"]))
            ratio = Fraction(r_to) / Fraction(r_from)
            touched = []
            for lot in holdings.get((p["customer_id"], p["symbol"]), []):
                touched.append((lot, lot["qty"]))
                lot["qty"] = qnum(lot["qty"] * r_to / r_from)
                lot["mult"] = lot["mult"] * ratio
            oplist.append(("split", touched, ratio))

        elif t == "symbol_change":
            # Move the whole (cid, old) list; merge by arrival order —
            # this oracle's independent derivation of FIFO seniority.
            cid = p["customer_id"]
            moved = holdings.pop((cid, p["old_symbol"]), None) or []
            for lot in moved:
                lot["sym"] = p["new_symbol"]
            if moved:
                key = (cid, p["new_symbol"])
                holdings[key] = sorted(holdings.get(key, []) + moved,
                                       key=lambda l: l["arr"])
            oplist.append(("rekey", p["old_symbol"], moved))

        elif t == "reversal":
            # Undo the original's ORACLE-ops in reverse, with the oracle's
            # own recorded quantities, costs and multipliers. A posted
            # reversal implies the original posted (the book validated).
            for op in reversed(ops.get(p.get("reverses_event_id"), [])):
                kind = op[0]
                if kind == "add":
                    lot = op[1]
                    lot["qty"] = qnum(0)  # zero the REMAINDER, shell stays
                    lot["cost"] = ZERO
                elif kind == "consume":
                    _k, lot, take, relieved, m0 = op
                    ratio = lot["mult"] / m0
                    lot["qty"] = qnum(lot["qty"] + take
                                      * Decimal(ratio.numerator)
                                      / Decimal(ratio.denominator))
                    lot["cost"] = lot["cost"] + relieved
                elif kind == "split":
                    # Undo semantics (mirrors the book, derived from A8):
                    # a lot untouched since the split restores its recorded
                    # prior quantity exactly; a lot that has since been
                    # sold un-scales what is actually there, so consumed
                    # shares are never resurrected as a phantom position.
                    _k, touched, ratio = op
                    num = Decimal(ratio.numerator)
                    den = Decimal(ratio.denominator)
                    for lot, prior in touched:
                        post = qnum(prior * num / den)
                        lot["qty"] = (prior if lot["qty"] == post
                                      else qnum(lot["qty"] * den / num))
                        lot["mult"] = lot["mult"] / ratio
                else:                     # rekey: move back, wherever each is
                    _k, old_sym, moved = op
                    for lot in moved:
                        cur = (lot["cid"], lot["sym"])
                        lst = holdings.get(cur, [])
                        if lot in lst:
                            lst.remove(lot)
                            if not lst:
                                holdings.pop(cur, None)
                        lot["sym"] = old_sym
                        holdings.setdefault((lot["cid"], old_sym),
                                            []).append(lot)

    # -- compare against the book's lot book ------------------------------
    book_agg: dict = {}
    for (cid, symbol), index in book.lot_index.items():
        q = sum((book.lots[i]["qty"] for i in index), qnum(0))
        c = sum((book.lots[i]["cost_total"] for i in index), ZERO)
        book_agg[(cid, symbol)] = (q, c)

    for key in sorted(set(book_agg) | set(holdings)):
        bq, bc = book_agg.get(key, (qnum(0), ZERO))
        lst = holdings.get(key, [])
        oq = sum((l["qty"] for l in lst), qnum(0))
        oc = sum((l["cost"] for l in lst), ZERO)
        cid, symbol = key
        if bq != oq:
            out.append(f"fifo-oracle qty[{cid},{symbol}]: book {bq} "
                       f"!= oracle {oq}")
        if bc != oc:
            out.append(f"fifo-oracle cost[{cid},{symbol}]: book {bc} "
                       f"!= oracle {oc}")
    return out
