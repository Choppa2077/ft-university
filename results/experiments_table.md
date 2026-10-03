| Exp | Build | Detection | Client-visible recovery | Failed (user) | Degraded | Retried OK | p95 | Data |
|---|---|---|---|---|---|---|---|---|
| E1 | baseline | 0.44 s | 20.5 s | 429/1600 | 0 | 0 | 32 ms | consistent |
| E1 | ft | 0.41 s | 0.0 s | 0/1600 | 0 | 0 | 35 ms | consistent |
| E2 | baseline | 0.29 s | 20.6 s | 666/1600 | 0 | 7 | 33 ms | 259 orphan debits; 133 double charges |
| E2 | ft | 0.27 s | 0.0 s | 0/1600 | 0 | 0 | 35 ms | consistent |
| E3 | baseline | 0.74 s | 19.9 s | 76/1600 | 0 | 43 | 5002 ms | 119 double charges |
| E3 | ft | 0.54 s | 0.0 s | 0/1600 | 134 | 0 | 39 ms | consistent |
| E4 | baseline | 0.24 s | 21.2 s | 844/1800 | 0 | 7 | 30 ms | consistent |
| E4 | ft | 0.44 s | 0.0 s | 0/1800 | 0 | 1 | 38 ms | consistent |
| E5 | baseline | not detected | 14.9 s | 8/1600 | 0 | 37 | 32 ms | 53 orphan debits; 45 double charges |
| E5 | ft | 1.08 s | 14.9 s | 10/1600 | 0 | 33 | 34 ms | consistent |
| E6 | baseline | not detected | 41.6 s | 3154/3850 | 0 | 0 | 5689 ms | 16 double charges |
| E6 | ft | 0.04 s | 4.2 s | 37/3850 | 1664 | 0 | 180 ms | consistent |

E5b baseline: {"experiment": "E5b", "mode": "baseline", "killed_at_index": 151, "total_time": 30.84, "time_lost_to_failure": null, "work_items": 451, "unique_transcripts": 300, "repeated_work": 151, "resumed_from": 0, "resumed_after": 20.63, "resumed_by": "records-a", "operator_rerun_after": 20.62}

E5b ft: {"experiment": "E5b", "mode": "ft", "killed_at_index": 150, "total_time": 10.83, "time_lost_to_failure": null, "work_items": 302, "unique_transcripts": 300, "repeated_work": 2, "resumed_from": 150, "resumed_after": 3.55, "resumed_by": "records-a", "operator_rerun_after": null}

E7 baseline: {"experiment": "E7", "mode": "baseline", "corrupted_files": 30, "silent": 25, "garbage": 5, "reads": 300, "correct": 270, "wrong_data_served": 25, "errors": 5, "repaired_on_read": 0, "wrong_examples": [{"student": "S0007", "served_gpa": 1.46, "true_gpa": 2.46}, {"student": "S0015", "served_gpa": 1.57, "true_gpa": 2.57}, {"student": "S0027", "served_gpa": 1.51, "true_gpa": 2.51}], "scrub_after": null, "repair_events": 0}

E7 ft: {"experiment": "E7", "mode": "ft", "corrupted_files": 30, "silent": 25, "garbage": 5, "reads": 300, "correct": 300, "wrong_data_served": 0, "errors": 0, "repaired_on_read": 30, "wrong_examples": [], "scrub_after": {"checked": 300, "repaired": 0, "unrecoverable": 0}, "repair_events": 30}
