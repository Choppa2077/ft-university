## Fault-injection experiments (mean ± sd over repetitions)

**X1 — Service instance failure (payment-a killed)**
**X2 — Dependency timeout (database primary frozen 12 s)**
**X3 — Slow external dependency (bank 6 s for 15 s)**
**X4 — Load spike (+180 timetable req/s for 15 s)**
**X7 — Recovery and consistency (30% payments interrupted 15 s)**
**X8 — Network partition (primary cut from the data network 12 s)**

| Exp | Build | Reps | Failed reqs | Detect (alert) s | Detect (monitor) s | Recovery s | p95 ms | Consistent | PASS |
|---|---|---|---|---|---|---|---|---|---|
| X1 | baseline | 1 | 307 | 3.12 | 0.50 | 21.1 | 21 | 1/1 | 0/1 |
| X1 | ft | 3 | 0 | - | 0.38 ± 0.04 | 0.0 | 19 ± 0 | 3/3 | 3/3 |
| X1 | endterm | 3 | 0 | - | 0.43 ± 0.09 | 0.0 | 22 ± 0 | 3/3 | 3/3 |
| X2 | baseline | 3 | 251 ± 2 | 4.05 ± 0.52 | 0.66 ± 0.10 | 12.5 ± 0.1 | 5003 | 0/3 | 0/3 |
| X2 | ft | 3 | 0 | 1.77 ± 0.49 | 0.67 ± 0.11 | 0.0 | 21 ± 0 | 3/3 | 3/3 |
| X2 | endterm | 3 | 0 | 1.95 ± 0.28 | 0.68 ± 0.05 | 0.0 | 41 ± 21 | 3/3 | 3/3 |
| X3 | baseline | 3 | 166 ± 1 | 4.48 ± 0.13 | - | 20.4 ± 0.0 | 5003 ± 0 | 0/3 | 0/3 |
| X3 | ft | 3 | 0 | 4.12 ± 0.29 | 1.80 ± 0.00 | 0.0 | 21 ± 0 | 3/3 | 3/3 |
| X3 | endterm | 3 | 0 | 4.37 ± 0.37 | 2.17 ± 0.01 | 0.0 | 26 ± 0 | 3/3 | 3/3 |
| X4 | baseline | 3 | 2966 ± 33 | 5.43 ± 0.35 | - | 30.0 ± 0.4 | 5005 ± 0 | 0/3 | 0/3 |
| X4 | ft | 3 | 13 ± 9 | 3.92 ± 0.26 | 0.04 ± 0.00 | 0.6 ± 0.4 | 102 ± 12 | 3/3 | 3/3 |
| X4 | endterm | 3 | 10 ± 2 | 4.03 ± 0.84 | 0.04 ± 0.01 | 0.7 ± 0.7 | 113 ± 16 | 3/3 | 3/3 |
| X7 | baseline | 3 | 37 ± 2 | - | 0.72 ± 0.29 | 15.1 ± 0.3 | 23 ± 0 | 0/3 | 0/3 |
| X7 | ft | 3 | 39 ± 6 | 16.96 ± 0.19 | 1.00 ± 0.02 | 15.3 ± 0.5 | 27 ± 1 | 3/3 | 3/3 |
| X7 | endterm | 3 | 34 ± 5 | - | 0.94 ± 0.01 | 15.7 ± 0.2 | 54 ± 3 | 3/3 | 3/3 |
| X8 | baseline | 3 | 460 ± 1 | 4.15 ± 0.63 | 0.51 ± 0.09 | 12.2 ± 0.0 | 21 ± 0 | 0/3 | 0/3 |
| X8 | ft | 3 | 0 | 1.75 ± 0.63 | 0.51 ± 0.03 | 0.0 | 20 ± 0 | 3/3 | 3/3 |
| X8 | endterm | 3 | 0 | 1.96 ± 0.38 | 0.49 ± 0.03 | 0.0 | 22 ± 0 | 3/3 | 3/3 |


## Security experiments

| Exp | Build | Cases passed | Data unchanged | Audit chain | Audit records | Alerts | PASS |
|---|---|---|---|---|---|---|---|
| X5 | endterm | 17/17 | yes | ok | 15 | AuthFailureSpike | PASS |
| X6 | endterm | 10/10 | yes | ok | 25 | - | PASS |


## Requirements verification (repetitions meeting the requirement / total)

| Req | Statement | Experiments | baseline | ft | endterm |
|---|---|---|---|---|---|
| R1 | a single crash fails at most 1% of requests | X1 | 0/1 | 3/3 | 3/3 |
| R2 | failure detected within 1 s, user-visible interruption at most 2 s | X1, X2 | 0/4 | 6/6 | 6/6 |
| R4 | money and records stay consistent | X2, X3, X7 | 0/9 | 9/9 | 9/9 |
| R5 | core p95 below 1 s under the load spike | X4 | 0/3 | 3/3 | 3/3 |
| R9 | an incident that outlasts the alert window raises an alert | X2, X3, X4, X8 | 12/12 | 12/12 | 12/12 |
| R12 | network partition: no split brain, data consistent | X8 | 0/3 | 3/3 | 3/3 |
| R7 | every unauthorized action denied, data unchanged | X5 | n/a (no auth) | n/a (no auth) | PASS |
| R8 | malformed and abusive input rejected, service up, data unchanged | X6 | n/a (no auth) | n/a (no auth) | PASS |
| R10 | audit trail complete and tamper-evident | X5 | n/a (no auth) | n/a (no auth) | PASS |
