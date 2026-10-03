"""Turn results/ into tables, reliability numbers and figures.

    python -m experiments.analyze
writes results/experiments_table.md, results/reliability.json, results/reliability.md, results/fig_*.png
"""
from __future__ import annotations

import json
from collections import defaultdict

from ftuni import config

from .run_experiments import RESULTS, SCENARIOS
from .workload import Rec, load_csv

BLUE, ORANGE = "#2a78d6", "#eb6834"          # FT, baseline (validated pair)
INK, INK2, MUTED, GRID, BAND = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#f0efec"
KINDS = ["db", "student", "payment", "records", "timetable"]


def load(name: str) -> dict | None:
    p = RESULTS / name / "summary.json"
    return json.loads(p.read_text()) if p.exists() else None


# ---------------------------------------------------------------- user-level numbers

def user_level(records: list[Rec]) -> tuple[int, int]:
    """A payment counts once per click-intent (the retry is part of the same attempt to pay)."""
    pays: dict = defaultdict(list)
    total = failed = 0
    for r in records:
        if r.op == "pay":
            pays[r.intent].append(r)
        else:
            total += 1
            failed += not r.ok
    total += len(pays)
    failed += sum(not any(x.ok for x in rs) for rs in pays.values())
    return total, failed


# ---------------------------------------------------------------- per-second series

def per_second(records: list[Rec], duration: float, key=lambda r: True):
    n = int(duration) + 1
    tot, ok = [0] * n, [0] * n
    for r in records:
        if not key(r):
            continue
        b = int(r.t0)
        if 0 <= b < n:
            tot[b] += 1
            ok[b] += r.ok
    return tot, ok


def outages(tot: list[int], ok: list[int], threshold: float = 0.5, merge_gap: int = 2) -> list[tuple[int, int]]:
    """A service is down in a 1-second window if fewer than half of its requests succeeded.
    Down windows are merged into outage episodes; gaps of up to 2 s inside an outage do not split it."""
    up = []
    prev = True
    for t, o in zip(tot, ok):
        cur = prev if t == 0 else (o / t >= threshold)
        up.append(cur)
        prev = cur
    eps, start = [], None
    for i, u in enumerate(up):
        if not u and start is None:
            start = i
        if u and start is not None:
            eps.append((start, i))
            start = None
    if start is not None:
        eps.append((start, len(up)))
    return merge(eps, merge_gap)


def merge(eps: list[tuple[int, int]], gap: int = 0) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for a, b in sorted(eps):
        if out and a - out[-1][1] <= gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


# ---------------------------------------------------------------- reliability from the soak runs

def component_metrics(events: list[dict], origin: float, duration: float, mode: str) -> dict:
    t0, t1 = origin, origin + duration
    insts = [i for i in config.topology(mode) if i.node in ("A", "B")]
    per = {}
    for inst in insts:
        evs = [e for e in events if e["inst"] == "monitor" and e.get("target") == inst.name and e["ev"] in ("DOWN", "UP")]
        intervals, cur = [], None
        for e in evs:
            if e["ev"] == "DOWN":
                cur = e.get("first_failed_check") or e["ts"]
            elif cur is not None:
                intervals.append((cur, e["ts"]))
                cur = None
        if cur is not None:
            intervals.append((cur, t1))
        intervals = [(max(a, t0), min(b, t1)) for a, b in intervals if b > t0 and a < t1]
        down = sum(b - a for a, b in intervals)
        per[inst.name] = {"kind": inst.kind, "failures": len(intervals), "downtime": round(down, 2),
                          "repairs": [round(b - a, 2) for a, b in intervals]}
    kinds = {}
    for k in KINDS:
        rows = [v for v in per.values() if v["kind"] == k]
        T = duration * len(rows)
        n = sum(v["failures"] for v in rows)
        down = sum(v["downtime"] for v in rows)
        up = T - down
        kinds[k] = {"replicas": len(rows), "failures": n, "downtime_s": round(down, 2),
                    "MTTF_s": round(up / n, 1) if n else None, "MTTR_s": round(down / n, 2) if n else None,
                    "MTBF_s": round(T / n, 1) if n else None, "availability": round(up / T, 5),
                    "failure_rate_per_h": round(3600 * n / up, 2)}
    return {"instances": per, "kinds": kinds}


def system_metrics(records: list[Rec], duration: float) -> dict:
    groups = {"student": lambda r: r.op in ("student", "enroll"), "payment": lambda r: r.op == "pay",
              "records": lambda r: r.op == "transcript", "timetable": lambda r: r.op == "timetable"}
    T = int(duration) + 1

    def stats(eps: list[tuple[int, int]], sel: list[Rec]) -> dict:
        down = sum(b - a for a, b in eps)
        return {"outages": len(eps), "downtime_s": down,
                "MTTF_s": round((T - down) / len(eps), 1) if eps else None,
                "MTTR_s": round(down / len(eps), 2) if eps else None,
                "availability_time": round((T - down) / T, 4),
                "availability_requests": round(sum(r.ok for r in sel) / max(1, len(sel)), 4),
                "episodes": eps}

    out, all_eps = {}, []
    for name, key in groups.items():
        tot, ok = per_second(records, duration, key)
        eps = outages(tot, ok)
        all_eps += eps
        out[name] = stats(eps, [r for r in records if key(r)])
    # the system is "up" only while every service is up
    out["system"] = stats(merge(all_eps), records)
    total, failed = user_level(records)
    out["user_level"] = {"requests": total, "failed": failed, "success": round(1 - failed / max(1, total), 4)}
    return out


def predicted(kinds: dict, mode: str) -> dict:
    """RBD prediction from the measured component availabilities: services in series,
    replicas of one service in parallel (perfect, instant failover assumed)."""
    a = {k: v["availability"] for k, v in kinds.items()}
    blocks = {k: (1 - (1 - a[k]) ** 2) if mode == "ft" else a[k] for k in KINDS}
    sys = 1.0
    for v in blocks.values():
        sys *= v
    return {"blocks": {k: round(v, 6) for k, v in blocks.items()}, "system": round(sys, 6)}


def reliability() -> dict | None:
    out = {}
    for mode in ("baseline", "ft"):
        d = RESULTS / f"soak_{mode}"
        if not (d / "meta.json").exists():
            return None
        meta = json.loads((d / "meta.json").read_text())
        events = [json.loads(line) for line in (d / "events.jsonl").read_text().splitlines() if line.strip()]
        records = load_csv(d / "requests.csv")
        comp = component_metrics(events, meta["origin"], meta["duration"], mode)
        out[mode] = {"duration_s": meta["duration"], "kills_attempted": len(meta["kills"]),
                     "kills_done": sum(k["killed"] for k in meta["kills"]),
                     "components": comp["kinds"], "instances": comp["instances"],
                     "system": system_metrics(records, meta["duration"]),
                     "predicted": predicted(comp["kinds"], mode)}
    (RESULTS / "reliability.json").write_text(json.dumps(out, indent=2))
    return out


# ---------------------------------------------------------------- experiment table

def detection_text(s: dict) -> str:
    d = s.get("detection_time")
    return f"{d:.2f} s" if isinstance(d, (int, float)) else "not detected"


def experiment_rows() -> list[dict]:
    rows = []
    for sid in ["E1", "E2", "E3", "E4", "E5", "E6"]:
        for mode in ("baseline", "ft"):
            s = load(f"{sid}_{mode}")
            if not s:
                continue
            records = load_csv(RESULTS / f"{sid}_{mode}" / "requests.csv")
            total, failed = user_level(records)
            c = s["consistency"]
            problems = []
            if c["orphan_debits"]:
                problems.append(f"{c['orphan_debits']} orphan debits")
            if c["double_charged_intents"]:
                problems.append(f"{c['double_charged_intents']} double charges")
            if c["stuck_payments"]:
                problems.append(f"{c['stuck_payments']} stuck payments")
            if c.get("lost_acked_payments") or c.get("lost_acked_enrollments"):
                problems.append("lost acknowledged writes")
            if "replica" in c and not c["replica"].get("identical"):
                problems.append("replica differs")
            rows.append({"exp": sid, "mode": mode, "title": s["title"], "detection": detection_text(s),
                         "recovery": s["client_visible_recovery"], "requests": total, "failed": failed,
                         "attempt_failures": s["failed"], "degraded": s["degraded"],
                         "retried_ok": s["recovered_by_retry"], "p95": s["latency_p95"],
                         "consistency": "consistent" if c["consistent"] else "; ".join(problems),
                         "restored": s.get("redundancy_restored_after"), "summary": s})
    return rows


def write_table(rows: list[dict]) -> None:
    lines = ["| Exp | Build | Detection | Client-visible recovery | Failed (user) | Degraded | Retried OK | p95 | Data |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['exp']} | {r['mode']} | {r['detection']} | {r['recovery']:.1f} s | {r['failed']}/{r['requests']} "
                     f"| {r['degraded']} | {r['retried_ok']} | {r['p95'] * 1000:.0f} ms | {r['consistency']} |")
    for name in ("E5b", "E7"):
        for mode in ("baseline", "ft"):
            s = load(f"{name}_{mode}")
            if s:
                lines.append(f"\n{name} {mode}: " + json.dumps({k: v for k, v in s.items() if k not in ("title",)}))
    (RESULTS / "experiments_table.md").write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------- figures

def style(ax) -> None:
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#c3c2b7")
        ax.spines[side].set_linewidth(0.8)
    ax.tick_params(colors=MUTED, labelsize=8, length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def fig_timelines() -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 3, figsize=(10, 5.4), sharey=True)
    for ax, sid in zip(axes.flat, ["E1", "E2", "E3", "E4", "E5", "E6"]):
        s_ft = load(f"{sid}_ft")
        if not s_ft:
            continue
        dur = SCENARIOS[sid]["duration"]
        t_inj = s_ft["t_inject"]
        end = s_ft["marks"].get("revert", {}).get("t") or (t_inj + (20 if sid == "E4" else 15 if sid == "E6" else 0.6))
        ax.axvspan(t_inj, end, color=BAND, zorder=0)
        for mode, color, label in (("baseline", ORANGE, "Baseline"), ("ft", BLUE, "Fault-tolerant")):
            recs = load_csv(RESULTS / f"{sid}_{mode}" / "requests.csv")
            tot, ok = per_second(recs, dur)
            xs = [i + 0.5 for i, t in enumerate(tot) if t]
            ys = [100 * o / t for o, t in zip(ok, tot) if t]
            ax.plot(xs, ys, color=color, linewidth=2, solid_joinstyle="round", solid_capstyle="round", label=label)
        style(ax)
        ax.set_title(f"{sid}  {SCENARIOS[sid]['title'].split(' (')[0]}", fontsize=9, color=INK, loc="left")
        ax.set_ylim(-3, 103)
        ax.set_xlim(0, dur)
    for ax in axes[:, 0]:
        ax.set_ylabel("requests OK (%)", fontsize=8, color=INK2)
    for ax in axes[1]:
        ax.set_xlabel("time (s)", fontsize=8, color=INK2)
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=2, frameon=False, fontsize=9, labelcolor=INK)
    fig.text(0.99, 0.01, "grey band = fault active", ha="right", fontsize=7.5, color=MUTED)
    fig.tight_layout(rect=(0, 0.02, 1, 0.94))
    fig.savefig(RESULTS / "fig_timelines.png", dpi=200)
    plt.close(fig)


def fig_load_latency() -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(6.5, 2.8))
    s = load("E6_ft")
    if not s:
        return
    ax.axvspan(10, 25, color=BAND, zorder=0)
    for mode, color, label in (("baseline", ORANGE, "Baseline"), ("ft", BLUE, "Fault-tolerant")):
        recs = [r for r in load_csv(RESULTS / f"E6_{mode}" / "requests.csv") if r.op != "timetable" and r.t0 < 40]
        by = defaultdict(list)
        for r in recs:
            by[int(r.t0)].append(r.latency if r.ok else 5.0)
        xs = sorted(by)
        ys = [sorted(by[x])[int(0.95 * (len(by[x]) - 1))] * 1000 for x in xs]
        ax.plot([x + 0.5 for x in xs], ys, color=color, linewidth=2, label=label)
    style(ax)
    ax.set_yscale("log")
    ax.set_ylabel("p95 latency, ms (log)", fontsize=8, color=INK2)
    ax.set_xlabel("time (s) - non-timetable requests only; failures counted as 5 s", fontsize=8, color=INK2)
    ax.set_xlim(0, 40)
    ax.legend(frameon=False, fontsize=8, labelcolor=INK, loc="center right")
    fig.tight_layout()
    fig.savefig(RESULTS / "fig_e6_latency.png", dpi=200)
    plt.close(fig)


def fig_soak() -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 1, figsize=(10, 4.2), sharex=True)
    for ax, (mode, color, label) in zip(axes, (("baseline", ORANGE, "Baseline"), ("ft", BLUE, "Fault-tolerant"))):
        d = RESULTS / f"soak_{mode}"
        if not (d / "meta.json").exists():
            return
        meta = json.loads((d / "meta.json").read_text())
        recs = load_csv(d / "requests.csv")
        tot, ok = per_second(recs, meta["duration"])
        xs = [i + 0.5 for i, t in enumerate(tot) if t]
        ys = [100 * o / t for o, t in zip(ok, tot) if t]
        ax.plot(xs, ys, color=color, linewidth=1.6)
        for k in meta["kills"]:
            if k["killed"]:
                ax.axvline(k["t"], color=MUTED, linewidth=0.8, ymin=0, ymax=0.06)
        style(ax)
        ax.set_ylim(-3, 103)
        ax.set_ylabel("OK (%)", fontsize=8, color=INK2)
        ax.set_title(f"{label}: {sum(k['killed'] for k in meta['kills'])} process crashes (ticks at the bottom)",
                     fontsize=9, color=INK, loc="left")
    axes[-1].set_xlabel("time (s)", fontsize=8, color=INK2)
    fig.tight_layout()
    fig.savefig(RESULTS / "fig_soak.png", dpi=200)
    plt.close(fig)


def main() -> None:
    rows = experiment_rows()
    write_table(rows)
    print((RESULTS / "experiments_table.md").read_text())
    rel = reliability()
    if rel:
        for mode, r in rel.items():
            print(f"--- {mode}: kills {r['kills_done']}/{r['kills_attempted']}")
            for k, v in r["components"].items():
                print(f"  {k:10} {v}")
            for k, v in r["system"].items():
                print(f"  {k:22} {({kk: vv for kk, vv in v.items() if kk != 'episodes'})}")
            print("  predicted", r["predicted"])
    fig_timelines()
    fig_load_latency()
    fig_soak()
    report_data()



def report_data() -> None:
    """Everything the report quotes, collected in one file."""
    rows = experiment_rows()
    data = {"experiments": [{k: v for k, v in r.items() if k != "summary"} | {
        "event_counts": r["summary"].get("event_counts", {}),
        "first_failure_after": r["summary"].get("first_failure_after"),
        "latency_p50": r["summary"]["latency_p50"], "latency_p99": r["summary"]["latency_p99"],
        "timeouts": r["summary"]["timeouts"],
        "interrupt": {k: r["summary"].get(k) for k in ("interrupted", "interrupted_resolved", "detection_time",
                                                        "resolution_time_mean", "resolution_time_max")},
        "cons": {k: v for k, v in r["summary"]["consistency"].items() if k != "replica"},
        "replica": r["summary"]["consistency"].get("replica"),
        "marks": {k: v["t"] for k, v in r["summary"]["marks"].items()}} for r in rows]}
    for name in ("E5b", "E7"):
        for mode in ("baseline", "ft"):
            data[f"{name}_{mode}"] = load(f"{name}_{mode}")
    rel = RESULTS / "reliability.json"
    if rel.exists():
        r = json.loads(rel.read_text())
        for mode in r:
            r[mode].pop("instances", None)
            for g in r[mode]["system"].values():
                g.pop("episodes", None)
        data["reliability"] = r
    (RESULTS / "report_data.json").write_text(json.dumps(data, indent=1))


if __name__ == "__main__":
    main()
