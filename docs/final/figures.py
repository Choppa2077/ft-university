"""Static figures for the final report (data flow of the critical workflow).

    python docs/final/figures.py        # -> results/final/fig_payment_flow.png
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

OUT = Path(__file__).resolve().parent.parent.parent / "results" / "final"

LANES = ["Student (client)", "Gateway", "Payment replica", "DB primary", "DB replica", "Bank"]
X = {name: i * 2.2 for i, name in enumerate(LANES)}
C, G, P, DP, DR, B = LANES
NOTES_X = X[B] + 0.9            # failure-handling notes live in their own column, right of the lanes

INK, MUTED, ACCENT, WARN = "#1f2933", "#52606d", "#2563eb", "#b45309"


def arrow(ax, y, a, b, text, color=INK, dashed=False):
    ax.annotate("", xy=(X[b], y), xytext=(X[a], y),
                arrowprops=dict(arrowstyle="-|>", color=color, lw=1.4, linestyle="--" if dashed else "-"))
    ax.text((X[a] + X[b]) / 2, y + 0.1, text, ha="center", va="bottom", fontsize=8.4, color=color)


def lane_note(ax, y, lane, text):
    ax.text(X[lane] + 0.1, y, text, ha="left", va="center", fontsize=8.0, color=MUTED,
            bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="#9aa5b1", lw=0.7))


def failure_note(ax, y, text):
    ax.plot([X[B] + 0.15, NOTES_X - 0.1], [y, y], color=WARN, lw=0.8, linestyle=":")
    ax.text(NOTES_X, y, text, ha="left", va="center", fontsize=8.0, color=WARN,
            bbox=dict(boxstyle="round,pad=0.35", fc="#fffbeb", ec=WARN, lw=0.8))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(17, 8.2))
    y = 11.6
    steps = []

    def at(dy):
        nonlocal y
        y -= dy
        return y

    arrow(ax, at(0), C, G, "1  POST /payments  (Bearer JWT, Idempotency-Key)")
    lane_note(ax, at(0.8), G, "2  authenticate · authorize (own record) · validate · rate-limit ·\n"
                              "   bulkhead · pick a healthy replica · add X-Request-ID")
    arrow(ax, at(0.95), G, P, "3  forward (timeout 4.5 s)")
    failure_note(ax, y, "replica crashed → refused connection → replica marked down,\n"
                        "request retried on the other replica; Docker restarts it  (X1)")
    arrow(ax, at(0.8), P, DP, "4  key already known?  → return / finish the original payment", color=ACCENT)
    arrow(ax, at(0.8), P, DP, "5  INSERT status = PENDING  (checkpoint before money moves)", color=ACCENT)
    arrow(ax, at(0.7), DP, DR, "6  replicate (LSN, epoch)", color=ACCENT)
    failure_note(ax, y, "primary frozen or partitioned → monitor promotes the in-sync replica,\n"
                        "epoch + 1 fences the old primary; DB client retries ≤ 4 s  (X2, X8)")
    arrow(ax, at(0.55), DR, DP, "ack", color=ACCENT, dashed=True)
    arrow(ax, at(0.55), DP, P, "commit", color=ACCENT, dashed=True)
    arrow(ax, at(0.9), P, B, "7  POST /debit  (same key; timeout 0.8 s, 2 retries with jitter, breaker)")
    failure_note(ax, y, "bank slow → timeouts open the circuit breaker → 202 QUEUED,\n"
                        "finished later by the recovery worker with the same key  (X3)")
    arrow(ax, at(0.6), B, P, "debit ref  (a repeated key returns the first debit)", dashed=True)
    arrow(ax, at(0.9), P, DP, "8  UPDATE COMPLETED + INSERT ledger  (one transaction)", color=ACCENT)
    failure_note(ax, y, "crash between 7 and 8 → the client's retry (same key) or the recovery\n"
                        "worker rolls forward; no debit after 10 s → roll back  (X7)")
    arrow(ax, at(0.6), DP, DR, "replicate · ack · commit", color=ACCENT)
    arrow(ax, at(0.8), P, G, "201 COMPLETED", dashed=True)
    arrow(ax, at(0.55), G, C, "201", dashed=True)
    bottom = y - 0.5

    top = 12.1
    for name, x in X.items():
        ax.text(x, top + 0.1, name, ha="center", va="bottom", fontsize=10.5, fontweight="bold", color=INK)
        ax.plot([x, x], [bottom, top], color="#cbd2d9", lw=1.0, zorder=0)
    ax.text(NOTES_X, top + 0.1, "Failure handling (experiment)", ha="left", va="bottom", fontsize=10.5,
            fontweight="bold", color=WARN)

    ax.set_xlim(-1.0, NOTES_X + 6.3)
    ax.set_ylim(bottom - 0.1, top + 0.8)
    ax.axis("off")
    ax.set_title("Critical workflow: tuition payment — data flow and where each mechanism acts "
                 "(blue = synchronously replicated database writes)", fontsize=11.5, color=INK, loc="left")
    fig.tight_layout()
    fig.savefig(OUT / "fig_payment_flow.png", dpi=200)
    print("wrote", OUT / "fig_payment_flow.png")


if __name__ == "__main__":
    main()
