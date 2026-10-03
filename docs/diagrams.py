"""Draws the architecture diagram and the reliability block diagrams (results/fig_*.png)."""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

OUT = Path(__file__).resolve().parent.parent / "results"
INK, INK2, MUTED = "#0b0b0b", "#52514e", "#898781"
FILL, NODE, BLUE, BLUE_W = "#f4f3f0", "#fafaf8", "#2a78d6", "#e6effa"


def box(ax, x, y, w, h, text, fill=FILL, edge=INK2, size=8.5, weight="normal", ls="-", lw=1.0, color=INK):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=0.12",
                                facecolor=fill, edgecolor=edge, linewidth=lw, linestyle=ls))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=size, color=color, weight=weight,
            linespacing=1.3)


def arrow(ax, a, b, color=INK2, ls="-", lw=1.1, both=False, rad=0.0):
    ax.add_patch(FancyArrowPatch(a, b, arrowstyle="<|-|>" if both else "-|>", mutation_scale=9, color=color,
                                 linewidth=lw, linestyle=ls, connectionstyle=f"arc3,rad={rad}"))


def architecture() -> None:
    fig, ax = plt.subplots(figsize=(10, 6.4))
    ax.set_xlim(0, 20)
    ax.set_ylim(0, 13)
    ax.axis("off")

    box(ax, 7.0, 11.6, 6.0, 0.9, "Clients (students, staff) / workload generator", fill="white")
    box(ax, 4.6, 9.7, 10.8, 1.2, "API gateway / load balancer\nhealth checks · timeouts · retries · circuit breakers · "
        "load shedding · stale cache", fill=BLUE_W, edge=BLUE, weight="normal")
    arrow(ax, (10, 11.6), (10, 10.9))

    for x0, name, db in ((0.6, "Node A", "db-a  (primary)"), (10.6, "Node B", "db-b  (replica)")):
        ax.add_patch(FancyBboxPatch((x0, 2.3), 8.8, 6.7, boxstyle="round,pad=0,rounding_size=0.2",
                                    facecolor=NODE, edgecolor=MUTED, linewidth=1.0, linestyle=(0, (4, 3))))
        ax.text(x0 + 0.25, 8.65, name, fontsize=9.5, color=INK, weight="bold", va="top")
        s = name[-1].lower()
        box(ax, x0 + 0.4, 6.4, 3.8, 1.3, f"student-{s}\nprofiles, registration")
        box(ax, x0 + 4.6, 6.4, 3.8, 1.3, f"payment-{s}\ntuition payments")
        box(ax, x0 + 0.4, 4.7, 3.8, 1.3, f"records-{s}\ntranscripts, batch job")
        box(ax, x0 + 4.6, 4.7, 3.8, 1.3, f"timetable-{s}\ntimetable generation")
        box(ax, x0 + 1.9, 2.75, 5.0, 1.3, f"{db}\nSQLite + write log", fill="white", edge=INK, weight="bold")
        arrow(ax, (x0 + 4.4, 4.7), (x0 + 4.4, 4.05), color=MUTED)
    arrow(ax, (7.6, 3.4), (12.4, 3.4), color=BLUE, lw=1.6)
    for yy, t in ((3.75, "synchronous log shipping"), (3.0, "failover: promote + new epoch")):
        ax.text(10.0, yy, t, ha="center", va="center", fontsize=7.5, color=INK2,
                bbox=dict(facecolor="white", edgecolor="none", pad=1.5))

    for x in (5.0, 15.0):
        arrow(ax, (10 + (x - 10) * 0.25, 9.7), (x, 9.0), color=INK2)

    box(ax, 0.6, 0.35, 5.6, 1.4, "monitor: health every 0.25 s, DB failover\nsupervisor: restarts crashed processes",
        fill="white", size=7.8)
    box(ax, 7.2, 0.35, 5.6, 1.4, "shared storage: disk1 + disk2\nmirrored, SHA-256 per file (RAID-1 idea)",
        fill="white", size=7.8)
    box(ax, 13.8, 0.35, 5.6, 1.4, "external bank (payment provider)\nidempotent debits by key", fill="white",
        size=7.8, ls=(0, (3, 2)))
    ax.text(0.6, 1.95, "shared and external components", fontsize=8, color=MUTED)
    fig.tight_layout()
    fig.savefig(OUT / "fig_architecture.png", dpi=200)
    plt.close(fig)


def rbd() -> None:
    fig, axes = plt.subplots(2, 1, figsize=(10, 3.6), gridspec_kw={"height_ratios": [1.6, 3]})
    names = ["gateway", "db", "student", "payment", "records", "timetable"]
    for ax, ft in zip(axes, (False, True)):
        ax.set_xlim(0, 20)
        ax.set_ylim(0, 3 if ft else 1.6)
        ax.axis("off")
        ax.text(0, (2.75 if ft else 1.45), "FT build" if ft else "Baseline", fontsize=9, weight="bold", color=INK)
        x = 0.9
        y_mid = 1.3 if ft else 0.6
        ax.plot([0.2, x], [y_mid, y_mid], color=INK2, linewidth=1)
        for n in names:
            w = 2.3
            if ft and n != "gateway":
                for dy, suf in ((0.55, "a"), (-0.55, "b")):
                    box(ax, x + 0.3, y_mid + dy - 0.3, w, 0.6, f"{n}-{suf}", size=7.5)
                    ax.plot([x, x + 0.3], [y_mid + dy, y_mid + dy], color=INK2, linewidth=1)
                    ax.plot([x + 0.3 + w, x + 0.6 + w], [y_mid + dy, y_mid + dy], color=INK2, linewidth=1)
                ax.plot([x, x], [y_mid - 0.55, y_mid + 0.55], color=INK2, linewidth=1)
                ax.plot([x + 0.6 + w, x + 0.6 + w], [y_mid - 0.55, y_mid + 0.55], color=INK2, linewidth=1)
                x += w + 0.6
            else:
                box(ax, x, y_mid - 0.3, w, 0.6, n if not ft or n != "gateway" else "gateway", size=7.5)
                x += w
            ax.plot([x, x + 0.35], [y_mid, y_mid], color=INK2, linewidth=1)
            x += 0.35
    fig.tight_layout()
    fig.savefig(OUT / "fig_rbd.png", dpi=200)
    plt.close(fig)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    architecture()
    rbd()
