# Superseded results

`X3_bindmount/` — first run of X3 (slow bank), all three builds, with the cluster's run
directory on a **host bind mount** (Docker Desktop file sharing, virtiofs).

During X3 / endterm / rep3, at t ≈ 42 s, writes from the records service to the shared
transcript storage started failing (HTTP 500, then 503 once the gateway's breaker opened),
the post-run replica consistency query failed, and afterwards the host could not remove the
run directory (`run-docker/logs` was left with an invalid inode: mode drwx------, 65535 links).
This is a fault of the test harness's host file sharing under the write load of 17
containers, not of the system under test: the database and payments kept working until the
end of the run.

Fix: the run directory was moved to a named Docker volume (ext4 inside the Docker VM) and
evidence is copied out with `docker cp` after each run. X3 was then re-run in full on the new
backend so that all three builds of an experiment share the same environment. X1 and X2 had
completed cleanly on the bind mount and were kept.
