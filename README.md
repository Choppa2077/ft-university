# Fault-Tolerant University Information System

Midterm project for *Fault Tolerance and Dependable Computing* (AITU, 2026–2027).

A small university platform (student registration, tuition payments, transcripts,
timetables) built twice from the same code: a **baseline** with one copy of
everything and no protection, and a **fault-tolerant (FT)** build with replicas on
two nodes, database replication with automatic failover, and software-level
recovery mechanisms. Failure-injection scripts break both builds in the same way
and measure what users see and whether the data stays correct.

## Quick start

Python 3.10+ (tested on 3.11, Linux and macOS). No Docker needed.

```bash
pip install -r requirements.txt

python -m ftuni up --mode ft          # terminal 1: start the FT build (Ctrl+C stops everything)
python -m experiments.demo db         # terminal 2: live demo, kills the database primary
python -m ftuni status                # who is up, who is the DB primary
```

Gateway: http://127.0.0.1:8000, e.g. `curl 127.0.0.1:8000/students/S0001`.

Demo scenarios: `crash` (kill a service replica), `db` (kill the DB primary),
`node` (power off node A), `bank` (bank answers after 6 s), `payment` (30% of
payments die half-way), `load` (burst of timetable requests). Run the same demo
against `--mode baseline` to see the difference.

## Architecture

```
                 clients
                    |
              [ API gateway ]  load balancing, health checks, timeouts, retries,
                    |          circuit breakers, load shedding, stale cache
      +-------------+--------------+
   node A                       node B
   student-a  payment-a         student-b  payment-b
   records-a  timetable-a       records-b  timetable-b
   db-a (primary)  ----log---->  db-b (replica)
                    |
   monitor (health, DB failover)   supervisor (restarts crashed processes)
   external bank (idempotent debits)   storage: disk1 + disk2 (RAID-1 style)
```

| Mechanism | Where |
|---|---|
| Service replication on two nodes, load balancing | `ftuni/gateway.py`, `ftuni/config.py` |
| Health checks (active + passive), self-healing restarts | `gateway.py`, `monitor.py`, `supervisor.py` |
| DB primary/replica, synchronous log shipping, failover with epochs (fencing) | `ftuni/dbnode.py`, `monitor.py`, `dbclient.py` |
| Mirrored storage with SHA-256 checksums and self-repair (RAID-1/ECC idea) | `ftuni/storage.py` |
| Timeouts, retry with exponential backoff + jitter, circuit breaker, bulkhead | `ftuni/resilience.py`, `gateway.py`, `services/payment.py` |
| Idempotency keys, duplicate-request detection | `services/payment.py`, `dbnode.py` (tx_id), `services/bank.py` |
| Checkpointing, roll-forward / roll-back recovery, compensation | `services/payment.py`, `services/records.py` (batch job) |
| Graceful degradation (queued payments, stale timetable cache, shedding) | `gateway.py`, `services/payment.py` |

The two builds differ only in `ftuni/config.py` (`BASELINE` vs `FT`).

## Experiments

```bash
python -m experiments.run_experiments          # E1-E7 in both builds (~20 min)
python -m experiments.soak --mode both         # 9-minute random-crash run per build
python -m experiments.analyze                  # tables, reliability numbers, figures
python -m pytest -q tests                      # unit tests
```

| ID | Fault |
|---|---|
| E1 | kill one application instance (student-a) |
| E2 | kill the database primary |
| E3 | bank answers after 6 s for 15 s (service-to-service timeout) |
| E4 | node A down for 20 s (all its processes, incl. the DB primary) |
| E5 | 30% of payments interrupted right after the bank debit, for 15 s |
| E5b | transcript batch job killed half-way (checkpoint/resume) |
| E6 | +150 timetable requests/s for 15 s (overload) |
| E7 | 30 stored transcripts corrupted on disk (silent data corruption) |

Raw results (every request, every event) are in `results/<experiment>_<build>/`.

### Main results

| Exp | Baseline: failed user requests / data | FT: failed user requests / data |
|---|---|---|
| E1 app crash | 429 / 1600, 20.5 s outage | 0 / 1600 |
| E2 DB primary down | 666 / 1600; 259 debits without a payment, 133 double charges | 0 / 1600, failover in 0.53 s |
| E3 slow bank | 76 / 1600; 119 double charges | 0 / 1600; 134 payments queued and completed later |
| E4 node A down | 844 / 1800 | 0 / 1800 |
| E5 interrupted payments | 53 debits without a payment, 45 double charges | consistent; every payment finished within 1.9 s |
| E6 overload | 3154 / 3850, did not recover | 37 / 3850; 1664 stale-cache answers |
| E5b batch killed | restarted from zero, 151 transcripts redone | resumed from checkpoint 150, 2 redone |
| E7 corrupted files | 25 wrong GPAs served silently | 30/30 detected and repaired |

Soak test (9 min, random crashes): baseline 61.2% availability (time), FT 99.82%; the FT build's only
outage (1 s, 7 requests) was a near-simultaneous crash of both student replicas.

## Layout

```
ftuni/            the system (one package, every process runs `python -m ftuni.run <instance>`)
  services/       student, payment, records, timetable, bank
experiments/      workload generator, fault injection, consistency checks, demo, analysis
tests/            unit tests
results/          measured data and figures
run/              runtime data and logs (created on start, wiped on the next start)
```
