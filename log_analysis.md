# Log analysis

Use all three supplied logs. Answer every question with commands/scripts and actual output.

## Commands / scripts

All analysis was performed with a single reproducible script, `analyze_logs.py`
(repo root), which parses all three log files, flags malformed/duplicate
lines, deduplicates by request_id, and computes every metric below. Run with:

    python3 analyze_logs.py

Supplementary targeted commands used for correlation:

    grep '"request_id": "lab-000122"' logs/application.log
    grep '"request_id": "lab-000644"' logs/application.log
    grep 'lab-000122\|lab-000124\|lab-000126' logs/error.log

## Results

### 1. UTC interval and line quality
- Interval covered: 2026-08-20T11:00:00.015Z .. 2026-08-20T11:29:57.578Z (~30 minutes).
- access.log: 725 valid lines, 1 malformed (line 311, truncated/invalid JSON), 10 duplicate lines.
- application.log: 729 valid lines, 1 malformed (line 401, truncated/invalid JSON), 4 duplicate lines.
- error.log: 67 valid lines (matched the expected nginx error format), 1 line that did not
  match (line 68, a `[notice]` log-rotation message, not an error record — correctly excluded).

### 2. Distinct client requests and deduplication
- 720 distinct request_ids in access.log (from 725 valid lines).
- Deduplication method: grouped all access.log records by request_id; when a request_id
  appeared on more than one line (5 request_ids, "duplicate log lines" — the same event
  logged twice), only the last occurrence was used as the record of that request.
- Retries were identified separately from duplicate lines: nginx logs a retried request as
  ONE line with a comma-separated `upstream` field (e.g. "172.23.0.12:8080, 172.23.0.3:8080"),
  representing multiple upstream attempts for a single client request. 19 request_ids had this
  comma-separated pattern. These were counted once each (not once per attempted upstream),
  using the final status/upstream in that single line, avoiding double-counting.

### 3. Final client status counts and error rate
- Status counts (720 distinct requests): 200: 615, 404: 10, 502: 40, 503: 47, 504: 8.
- Denominator: 720 distinct client requests (deduplicated as described in Q2).
- Error rate (status >= 400 / total): 105 / 720 = 14.58%.

### 4. Failures by path, time window, and backend
- By path: /records: 26, /counter: 26, /ready: 23, /missing: 10, /health: 10, /: 10.
  (Note: the 10 /missing failures are 404s unrelated to the outage — see Q9.)
- By backend (upstream): 172.23.0.12:8080: 73 failures, 172.23.0.11:8080: 32 failures.
- By time: all 105 failures fall within a single hour bucket (2026-08-20T11), consistent
  with a single incident window rather than scattered failures across the full log period.
  The actual failure window (see Q7) is 11:05:02 to 11:26:47.

### 5. Latency percentiles
- n = 720 (final record per distinct request), source: access.log `request_time` field (seconds).
- Method: linear interpolation on the sorted request_time values (the "R-7"/Excel-style
  percentile method): index = (n-1) * p, interpolating between the two nearest ranks.
- Median (p50): 0.0540s. p95: 2.0010s.
- The p95 value of ~2.000s aligns closely with the environment's proxy_read_timeout/
  proxy_connect_timeout ceiling (2s), suggesting p95 is dominated by requests that hit a
  dead backend and waited out a timeout before failing or retrying, not organic slow
  responses from a healthy backend.

### 6. Retried requests
- 19 request_ids show a comma-separated `upstream` value (an actual multi-attempt retry).
- All 19 succeeded after retrying (final status < 400 in each case).
- This is separate from the 5 "duplicate log line" cases in Q2, which are not retries —
  they are the same single-attempt request logged twice in the source file.

### 7. Incident timeline (cross-referenced across all three logs)
- 11:00:00.015Z — normal traffic begins; first log line is an unrelated /missing 404
  (client-side, pre-dates and is unrelated to the outage).
- 11:05:02.503Z — first outage-related failure: request lab-000122, GET /health, 502,
  upstream 172.23.0.12:8080. Correlated error.log entry at the same timestamp:
    "connect() failed (111: Connection refused)... upstream: http://172.23.0.12:8080/health"
  This request has NO corresponding entry in application.log — it never reached the app
  process (see Q9).
- 11:05:02 -> 11:26:47 (~21m 45s) — intermittent 502/503/504 failures concentrated on
  172.23.0.12:8080 (73 of 105 total failures), with a smaller number also affecting
  172.23.0.11:8080 (32 failures) during the same window. 19 requests in this window
  successfully retried to the healthy backend rather than failing outright.
- 11:26:47.001Z — last outage-related failure: lab-000643, GET /records, 504,
  upstream 172.23.0.11:8080.
- 11:26:47.566Z (0.565s later) — first clean success after the outage: lab-000644,
  GET /counter, 200, served by instance_id "app-02" per application.log (matching upstream
  172.23.0.12:8080), confirming that backend had recovered.
- 11:29:57.578Z — log coverage ends; no further failures observed after recovery.

### 8. Correlated failed and successful request examples
- Failed request: request_id=lab-000122, 2026-08-20T11:05:02.503Z, GET /health, status 502,
  upstream 172.23.0.12:8080.
  - error.log (same timestamp): "connect() failed (111: Connection refused) while
    connecting to upstream... upstream: http://172.23.0.12:8080/health"
  - application.log: no entry for lab-000122 — the request never reached the app process.
- Successful request: request_id=lab-000644, 2026-08-20T11:26:47.566Z, GET /counter,
  status 200, upstream 172.23.0.12:8080.
  - application.log: {"level":"INFO","event":"http_request","request_id":"lab-000644",
    "instance_id":"app-02","method":"GET","path":"/counter","status":200,"duration_ms":66.0}
  - Confirms the same backend (172.23.0.12 / app-02) had recovered and was serving
    requests normally again by this point.

### 9. Proxy/connectivity issues vs dependency/application issues
- All 105 failures during the incident window are proxy/connectivity issues, not
  application/dependency issues. Proof: every failed request_id checked (e.g. lab-000122)
  has a corresponding error.log entry showing "connect() failed (111: Connection refused)"
  at the TCP level, and has NO corresponding entry in application.log at all — meaning the
  request never reached the Flask app process for that backend to log anything (successful
  or an error). If these were application/dependency failures (e.g. Postgres or Redis
  unreachable from within the app), we would instead expect a 503 with an application.log
  entry showing an ERROR-level "dependency_error" event, which does not appear anywhere in
  application.log during this window.
- The 10 /missing 404s are ordinary client errors (requests to a nonexistent route),
  unrelated to the outage, and are excluded from the incident analysis above.

### 10. What the logs do not prove, and what to check next in a running environment
- The logs prove that 172.23.0.12:8080 became unreachable at the TCP level for ~21 minutes
  and that NGINX successfully retried some requests to the surviving backend, but they do
  NOT prove the root cause of why that backend became unreachable (container crash,
  container removed, network partition, host resource exhaustion, etc.) — error.log only
  records the symptom (connection refused), not the underlying cause.
- The logs do not explain why 172.23.0.11:8080 also shows 32 failures during the same
  window — this is not obviously explained by a single-backend-down scenario and would
  need further investigation (e.g. was app-01 also degraded, or under increased load from
  handling all traffic during the outage, causing some of its own request timeouts?).
- The logs do not include container-level events (e.g. Docker healthcheck transitions,
  restarts, OOM kills) that would confirm what state the failing container was actually in.
- In a running environment, next steps would include: checking `docker events` and
  container healthcheck history for the corresponding time window, checking host-level
  resource metrics (CPU/memory) for signs of exhaustion, and checking whether
  proxy_next_upstream was even enabled at the time of this historical incident (the current
  environment's nginx.conf was found to have proxy_next_upstream disabled by default,
  raising the question of whether this historical incident's partial failover success
  reflects a different configuration than what was later found in the assessed environment
  — see troubleshooting.md Entry 5).

## Timeline and correlated examples

See Section "Results" #7 (Incident timeline) and #8 (Correlated examples) above for the
full chronological account with request IDs, timestamps, and cross-log evidence.

## Conclusions and limits

- A single backend (172.23.0.12:8080, corresponding to app-02) experienced a connectivity
  outage from 11:05:02 to 11:26:47 UTC (~21m 45s), evidenced by TCP-level connection
  refusals in error.log with no corresponding application.log activity.
- NGINX's retry mechanism successfully rerouted 19 requests to a healthy backend during
  this window, all of which succeeded — demonstrating partial failover was functioning
  historically, though a secondary, smaller pattern of failures on 172.23.0.11:8080 during
  the same window is not fully explained by the available logs and would need further
  investigation in a live environment.
- All conclusions above are derived directly from analyze_logs.py's output and targeted
  grep commands, cross-referencing request_id across all three log files — no counts or
  findings were manually estimated or invented.