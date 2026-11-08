"""Generate the Grafana dashboard (deploy/grafana/dashboards/ftuni.json) - dashboard as code."""
from __future__ import annotations

import json
from pathlib import Path

DS = {"type": "prometheus", "uid": "prom"}
GW = 'job="gateway", service!="gateway"'
panels: list[dict] = []
y = 0


def row(title: str) -> None:
    global y
    panels.append({"type": "row", "title": title, "collapsed": False, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
                   "id": len(panels) + 1, "panels": []})
    y += 1


def ts(title: str, targets: list[tuple[str, str]], x: int, w: int = 8, h: int = 7, unit: str = "short",
       stack: bool = False, max_: float | None = None, draw: str = "line") -> None:
    p = {"type": "timeseries", "title": title, "datasource": DS, "id": len(panels) + 1,
         "gridPos": {"x": x, "y": y, "w": w, "h": h},
         "targets": [{"datasource": DS, "expr": e, "legendFormat": l, "refId": chr(65 + i), "interval": "1s"}
                     for i, (e, l) in enumerate(targets)],
         "fieldConfig": {"defaults": {"unit": unit, "custom": {"drawStyle": draw, "lineWidth": 2, "fillOpacity": 10,
                                                                "stacking": {"mode": "normal" if stack else "none"}}},
                         "overrides": []},
         "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}}
    if max_ is not None:
        p["fieldConfig"]["defaults"]["max"] = max_
        p["fieldConfig"]["defaults"]["min"] = 0
    panels.append(p)


def state(title: str, expr: str, legend: str, x: int, w: int = 12, h: int = 8) -> None:
    panels.append({"type": "state-timeline", "title": title, "datasource": DS, "id": len(panels) + 1,
                   "gridPos": {"x": x, "y": y, "w": w, "h": h},
                   "targets": [{"datasource": DS, "expr": expr, "legendFormat": legend, "refId": "A", "interval": "1s"}],
                   "fieldConfig": {"defaults": {"color": {"mode": "thresholds"},
                                                "thresholds": {"mode": "absolute", "steps": [
                                                    {"color": "red", "value": None}, {"color": "green", "value": 1}]},
                                                "mappings": [{"type": "value", "options": {
                                                    "0": {"text": "DOWN"}, "1": {"text": "UP"}}}]}, "overrides": []},
                   "options": {"showValue": "never", "rowHeight": 0.8, "mergeValues": True}})


row("Users' view (gateway)")
ts("Requests per second by service", [(f'sum by (service) (rate(ftuni_http_requests_total{{{GW}}}[10s]))', "{{service}}")], 0)
ts("Error ratio (5xx) by service", [(f'sum by (service) (rate(ftuni_http_requests_total{{{GW}, status=~"5.."}}[10s])) '
                                     f'/ sum by (service) (rate(ftuni_http_requests_total{{{GW}}}[10s]))', "{{service}}")],
   8, unit="percentunit", max_=1)
ts("p95 latency by service", [(f'histogram_quantile(0.95, sum by (le, service) '
                               f'(rate(ftuni_http_request_duration_seconds_bucket{{{GW}}}[10s])))', "{{service}}")],
   16, unit="s")
y += 7
ts("Degraded / shed / retried per second", [
    ('sum by (service) (rate(ftuni_gateway_degraded_total[10s]))', "degraded {{service}}"),
    ('sum by (service) (rate(ftuni_gateway_shed_total[10s]))', "shed {{service}}"),
    ('sum by (service) (rate(ftuni_gateway_retries_total[10s]))', "retried {{service}}")], 0, w=12)
ts("Firing alerts", [('sum by (alertname) (ALERTS{alertstate="firing"})', "{{alertname}}")], 12, w=12, draw="bars")
y += 7

row("Instances")
state("Scrape status (Prometheus up)", 'up{job=~"ftuni|gateway"}', "{{name}}", 0)
state("Gateway view of replicas", "ftuni_gateway_upstream_up", "{{upstream}}", 12)
y += 8
ts("5xx ratio per replica (masked failures show up here)", [
    ('sum by (name) (rate(ftuni_http_requests_total{job="ftuni", status=~"5.."}[10s])) '
     '/ sum by (name) (rate(ftuni_http_requests_total{job="ftuni"}[10s]))', "{{name}}")], 0, w=12, unit="percentunit")
ts("CPU per instance (cores)", [('rate(process_cpu_seconds_total{job=~"ftuni|gateway"}[10s])', "{{name}}")], 12, w=12)
y += 7

row("Database and payments")
ts("Database role (1 = primary) and epoch", [("ftuni_db_is_primary{kind=\"db\"}", "primary {{name}}"),
                                             ("ftuni_db_epoch{kind=\"db\"}", "epoch {{name}}")], 0)
ts("Replication in sync (primary)", [("ftuni_db_replication_ok{kind=\"db\"}", "{{name}}")], 8, max_=1)
ts("Unfinished payments / open breakers", [("max by (status) (ftuni_payments_unfinished)", "{{status}}"),
                                           ("ftuni_breaker_open", "breaker {{name}}->{{target}}")], 16)
y += 7

row("Security")
ts("Refused requests per second by reason", [("sum by (reason) (rate(ftuni_auth_denied_total[10s]))", "{{reason}}")], 0)
ts("Rate-limited per second", [("sum by (scope) (rate(ftuni_rate_limited_total[10s]))", "{{scope}}")], 8)
ts("Audit records per second", [("sum by (event) (rate(ftuni_audit_records_total[10s]))", "{{event}}")], 16)
y += 7

dash = {"uid": "ftuni", "title": "FT University - endterm", "tags": ["ftuni"], "timezone": "browser",
        "schemaVersion": 39, "version": 1, "refresh": "2s", "time": {"from": "now-5m", "to": "now"},
        "panels": panels, "templating": {"list": []}, "annotations": {"list": []}}

out = Path(__file__).resolve().parent / "dashboards" / "ftuni.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(dash, indent=1))
print(f"wrote {out} ({len(panels)} panels)")
