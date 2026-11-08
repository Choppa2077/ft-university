"""Draw the endterm architecture (containers, networks, trust boundaries) -> fig_arch.png."""
from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyBboxPatch, Rectangle  # noqa: E402

fig, ax = plt.subplots(figsize=(12, 8))
ax.set_xlim(0, 12)
ax.set_ylim(0, 10)
ax.axis("off")

C = {"edge": "#34495e", "app": "#2980b9", "svc": "#2c3e50", "data": "#8e44ad",
     "bank": "#16a085", "ops": "#d35400", "net": "#ecf0f1"}


def box(x, y, w, h, text, color, fc="white", fs=9, bold=False):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.04", linewidth=1.6,
                                edgecolor=color, facecolor=fc))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=fs,
            color=color, fontweight="bold" if bold else "normal")


def net_band(x, y, w, h, label, color):
    ax.add_patch(Rectangle((x, y), w, h, facecolor=color, edgecolor="none", alpha=0.10, zorder=0))
    ax.text(x + 0.12, y + h - 0.22, label, fontsize=8, color=color, style="italic")


# networks (background bands)
net_band(0.3, 6.2, 11.4, 1.5, "network: app", C["app"])
net_band(0.3, 3.6, 8.6, 2.2, "network: data (internal)", C["data"])
net_band(9.1, 3.6, 2.6, 1.3, "network: bank (internal)", C["bank"])
net_band(0.3, 0.5, 11.4, 1.6, "network: ops", C["ops"])

# clients + trust boundary
ax.text(6, 9.6, "Clients (students / staff) · workload generator", ha="center", fontsize=10, fontweight="bold")
ax.plot([0.3, 11.7], [9.1, 9.1], "--", color="#c0392b", linewidth=1.4)
ax.text(11.6, 9.18, "trust boundary (Internet)", ha="right", fontsize=8, color="#c0392b")

# gateway (PEP)
box(4.2, 7.9, 3.6, 0.9, "gateway  —  PEP\nauthn · authz · rate limit · validation", C["edge"],
    fc="#fdf2e9", fs=8.5, bold=True)
ax.annotate("", xy=(6, 7.9), xytext=(6, 9.1), arrowprops=dict(arrowstyle="->", color=C["edge"]))

# services on app+data
for i, (name, note) in enumerate([("student-a/b", "profiles,\nregistration"),
                                   ("payment-a/b", "tuition,\nledger"),
                                   ("records-a/b", "transcripts,\nbatch"),
                                   ("timetable-a/b", "weekly\nschedule")]):
    box(0.6 + i * 2.8, 6.3, 2.4, 1.2, f"{name}\n{note}", C["svc"], fs=8)
    ax.annotate("", xy=(1.8 + i * 2.8, 7.5), xytext=(5.0, 7.9),
                arrowprops=dict(arrowstyle="-", color=C["app"], alpha=0.5))

# database (data network)
box(0.6, 3.9, 3.4, 1.4, "db-a (primary)  ⇄  db-b (replica)\nsync replication · failover · epoch fence\nper-service token + SQLite authorizer",
    C["data"], fc="#f5eef8", fs=8, bold=True)
# monitor
box(4.3, 3.9, 2.4, 1.4, "monitor\nhealth 0.25 s\nDB failover", C["data"], fs=8)
# bank
box(9.3, 3.9, 2.2, 1.0, "bank (external)\nidempotent debits", C["bank"], fs=8)

ax.annotate("", xy=(2.3, 5.3), xytext=(2.3, 6.3), arrowprops=dict(arrowstyle="->", color=C["data"]))
ax.annotate("", xy=(9.3, 4.4), xytext=(6.0, 6.3), arrowprops=dict(arrowstyle="->", color=C["bank"], alpha=0.7))

# ops row
for i, (name, note) in enumerate([("Prometheus", "scrape /metrics\nalert rules"),
                                   ("Alertmanager", "routes alerts"),
                                   ("alert-sink", "timestamps\nalerts"),
                                   ("Grafana", "dashboard")]):
    box(0.6 + i * 2.9, 0.7, 2.5, 1.1, f"{name}\n{note}", C["ops"], fs=8)

ax.text(6, 2.3, "observability (ops network): metrics · structured logs · correlation IDs · 12 alert rules",
        ha="center", fontsize=8.5, color=C["ops"])

ax.set_title("Endterm architecture — one container per instance, segmented networks, "
             "gateway as policy-enforcement point", fontsize=11)
fig.tight_layout()
out = Path(__file__).resolve().parent.parent.parent / "results" / "endterm"
out.mkdir(parents=True, exist_ok=True)
fig.savefig(out / "fig_arch.png", dpi=130)
print(f"wrote {out/'fig_arch.png'}")
