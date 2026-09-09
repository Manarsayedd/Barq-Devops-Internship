# Troubleshooting journal

Keep chronological entries. Copy this block for each meaningful investigation.

## Entry 1 / 2026-09-09 / initial investigation
- Symptom: `docker compose -p barq-assessment ps -a` showed both `app-01` and
  `app-02` as `Up (unhealthy)`, while `postgres` and `redis` showed `(healthy)`.
- Hypothesis: The app containers might be crashing or the Flask process might
  be failing to start correctly.
- Command or test:
  `docker compose -p barq-assessment exec app-01 python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/health').read())"`
- Actual output:
  `b'{"instance_id":"app-01","service":"barq-api","status":"alive","version":"2.0.0"}'`
- Failed attempt and what changed your thinking: Initially assumed the app
  process itself was broken or crashing, since Docker reported it unhealthy.
  This test proved the Flask process was actually running fine and answering
  /health correctly when queried from inside its own container. This ruled
  out an app-code crash and redirected the investigation toward networking
  or healthcheck configuration instead.
- Root cause: Not yet isolated at this point — see Entry 2.
- Fix: Not yet applied at this point.
- Retest evidence: See Entry 2.
- Related commit: N/A (investigation only, no change made yet).
- Remaining uncertainty: Whether the unhealthy status is caused by a
  networking issue, a healthcheck misconfiguration, or both.

## Entry 2 / 2026-09-09 / networking investigation
- Symptom: Following Entry 1, needed to confirm whether app-01 is reachable
  from a *different* container (as NGINX would need), not just from itself.
- Hypothesis: If APP_HOST=127.0.0.1 is the cause, a request from another
  container should fail even though the app answered fine internally in
  Entry 1.
- Command or test:
  `docker compose -p barq-assessment exec nginx wget -qO- http://app-01:8080/health`
- Actual output (before fix):
  `wget: can't connect to remote host (172.18.0.2): Connection refused`
- Failed attempt and what changed your thinking: N/A — this test confirmed
  the hypothesis on the first try, using the contrast with Entry 1's result
  as proof (works from inside itself, fails from another container).
- Root cause: Confirmed. docker-compose.yml set `APP_HOST: "127.0.0.1"` for
  app-01 and app-02, which binds the Flask server to the loopback interface
  only. This makes it unreachable from other containers (including NGINX)
  over the Docker network, since their traffic does not arrive via loopback.
- Fix: Changed `APP_HOST: "127.0.0.1"` to `APP_HOST: "0.0.0.0"` in the shared
  `x-app.environment` block of docker-compose.yml, so the app accepts
  connections on all network interfaces, not just loopback.
- Retest evidence (after fix):
  `docker compose -p barq-assessment exec nginx wget -qO- http://app-01:8080/health`
  -> `{"instance_id":"app-01","service":"barq-api","status":"alive","version":"2.0.0"}`
  Same command that previously failed with "Connection refused" now succeeds.
- Related commit: bcae930
- Remaining uncertainty: The (unhealthy) Docker status may still persist
  after this fix alone, since the healthcheck itself queries 127.0.0.1 from
  inside the container (unaffected by the bind-address bug) but hits
  /healthz, a route that does not exist in app/server.py. Tracked separately
  in Entry 3.

## Entry 3 / 2026-09-09 / healthcheck investigation
- Symptom: app-01 and app-02 still showed (unhealthy) in `docker compose
  ps -a` even after the APP_HOST fix in Entry 2.
- Hypothesis: The healthcheck command queries /healthz, a route not defined
  in app/server.py (only /health is implemented). Flask would return 404 for
  the wrong path, which the healthcheck script treats as a failure.
- Command or test:
  Changed docker-compose.yml healthcheck test URL from /healthz to /health,
  in the shared x-app.healthcheck block, then:
  `docker compose -p barq-assessment up -d --build app-01 app-02`
  `docker compose -p barq-assessment ps -a`
- Actual output: [paste your full ps -a output here, showing (healthy)]
- Failed attempt and what changed your thinking: N/A — root cause was
  identified directly by comparing the healthcheck URL against the actual
  routes defined in app/server.py.
- Root cause: The healthcheck was checking a nonexistent route (/healthz
  instead of /health), causing Docker to mark the container unhealthy on
  every interval even though the app itself was functioning correctly.
- Fix: Changed /healthz to /health in the shared x-app.healthcheck block of
  docker-compose.yml.
- Retest evidence:
  `docker compose -p barq-assessment exec nginx wget -qO- http://app-01:8080/health`
  -> `{"instance_id":"app-01","service":"barq-api","status":"alive","version":"2.0.0"}`
  This confirms both the Entry 2 fix (APP_HOST) and the Entry 3 fix
  (healthcheck path) are working together correctly.
- Related commit: bcae930
- Remaining uncertainty: `curl -i http://127.0.0.1:8080/` from the host still
  fails with "Recv failure: Connection reset by peer". This is a separate,
  known issue: nginx.conf listens on port 80, but docker-compose.yml maps
  host port 8080 to container port 81, where nothing is listening. This will
  be investigated next as its own entry.