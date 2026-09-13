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
  ## Entry 4 / 2026-09-13 / NGINX port mismatch investigation
- Symptom: `curl -i http://127.0.0.1:8080/` failed with
  "Recv failure: Connection reset by peer" from the host machine.
- Hypothesis: nginx.conf listens on port 80 inside its container, but
  docker-compose.yml maps host port 8080 to container port 81, where
  nothing is listening.
- Command or test:
  Changed docker-compose.yml: ports mapping from
  "127.0.0.1:${PUBLIC_PORT:-8080}:81" to
  "127.0.0.1:${PUBLIC_PORT:-8080}:80"
  Also changed nginx/nginx.conf upstream block: "server app-01:8081"
  to "server app-01:8080" (app-01 was never actually listening on 8081;
  APP_PORT is 8080 for both app services per docker-compose.yml).
  docker compose -p barq-assessment down
  docker compose -p barq-assessment up -d
  curl -i http://127.0.0.1:8080/
- Actual output:
  HTTP/1.1 200 OK
  {"instance_id":"app-01","message":"Welcome to BARQ Systems", ...}
- Failed attempt and what changed your thinking: First retest used
  `docker compose up -d --force-recreate nginx` only (not a full down/up).
  A burst of 10 rapid requests all hit the same upstream IP
  (172.18.0.3, app-01), suggesting load balancing wasn't working. A full
  `down` + `up` (recreating all containers together) followed by requests
  spaced 1 second apart showed clear alternation between app-01 and app-02.
  This showed the earlier result was a startup-timing artifact from a
  partial recreate, not a real load-balancing bug.
- Root cause: Two separate config mismatches in NGINX: (1) container listened
  on port 80 but Compose mapped host 8080 to container port 81; (2) the
  upstream block pointed app-01 at port 8081, a port it never listens on.
- Fix: Corrected the Compose port mapping to 8080:80, and corrected the
  upstream block to use app-01:8080.
- Retest evidence:
  curl -i http://127.0.0.1:8080/  -> HTTP/1.1 200 OK
  10 requests spaced 1s apart via /instance alternated between
  app-01 and app-02 roughly evenly, confirming both backends are reachable
  and being load-balanced correctly through nginx.
- Related commit: [fill in after committing]
- Remaining uncertainty: None regarding this specific issue. Separately
  noted: proxy_next_upstream off is still set in nginx.conf, which disables
  automatic failover to a healthy backend if one goes down — this will be
  addressed and tested as part of Part 3's failure_test requirement.
  ## Note / 2026-09-13 / correcting an earlier assumption
During initial static review, the docker-compose.yml shared for analysis
appeared to have INSTANCE_ID: "app-01" duplicated on both app-01 and app-02
services. On inspection of the actual working file, app-02 correctly sets
INSTANCE_ID: "app-02". This was a false positive from the initial review,
not an actual bug in the environment. No fix was needed for this item.
## Entry 5 / 2026-09-13 / proxy_next_upstream failover investigation
- Symptom: Stopping app-01 while it is in nginx's upstream pool causes ~50%
  of client requests to fail with raw 502/504 errors instead of nginx
  automatically serving them from the remaining healthy backend (app-02).
- Hypothesis: nginx.conf sets `proxy_next_upstream off;`, which disables
  nginx's default behavior of retrying a failed upstream request against
  another backend in the pool.
- Command or test (before fix):
  docker compose -p barq-assessment stop app-01
  for i in $(seq 1 10); do curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/instance; done
- Actual output (before fix):
  504, 502, 504, 502, 504, 502, 504, 502, 504, 502
  nginx logs confirm each failed request only attempted a single upstream
  (172.18.0.2, app-01) with no retry against app-02, e.g.:
  "connect() failed (113: Host is unreachable) while connecting to upstream"
- Failed attempt and what changed your thinking: An earlier attempt at this
  same test showed nginx, postgres, redis, and app-02 all "Exited (255)"
  simultaneously, unrelated to this test — traced to a WSL/Docker Desktop
  suspension (laptop sleep) during testing, not an application fault.
  Environment was restarted with `down` then `up -d` before repeating the
  test cleanly.
- Root cause: `proxy_next_upstream off;` in nginx/nginx.conf explicitly
  disabled nginx's automatic failover to a healthy backend when the chosen
  upstream fails or times out.
- Fix: Changed nginx/nginx.conf to:
  proxy_next_upstream error timeout http_502 http_503 http_504;
  proxy_next_upstream_tries 2;
  proxy_next_upstream_timeout 4s;
  Recreated nginx: docker compose -p barq-assessment up -d --force-recreate nginx
- Retest evidence (after fix):
  docker compose -p barq-assessment stop app-01
  for i in $(seq 1 10); do curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/instance; done
  -> 200, 200, 200, 200, 200, 200, 200, 200, 200, 200 (all succeeded)
  nginx logs confirm the retry chain directly, e.g.:
  upstream:"172.18.0.2:8080, 172.18.0.3:8080"
  upstream_status:"502, 200"
  This shows nginx tried app-01, received a 502, automatically retried
  app-02, and returned that 200 to the client — zero visible failures.
- Related commit: [fill in after committing]
- Remaining uncertainty: None regarding basic single-backend failover.
  Behavior under both backends being down simultaneously, or a slow (not
  fully dead) backend, has not yet been tested.
  ## Entry 6 / 2026-09-13 / DATABASE_URL/REDIS_URL credential mismatch
- Symptom: GET /records returned {"error":"postgres_unavailable"} even though
  postgres itself was healthy, had the correct schema (records table via
  database/init.sql), and contained seed data confirmed via direct psql query.
- Hypothesis: The app's DATABASE_URL might differ from postgres's actual
  configured credentials/port.
- Command or test:
  docker compose -p barq-assessment logs app-01 --no-color | grep dependency_error
  -> repeated "OperationalError" entries
  docker compose -p barq-assessment exec app-01 python -c "import psycopg;
  psycopg.connect('postgresql://barq_app:BarqLabOnly_7qN2vK8c@postgres:5432/barq_tasks', ...)"
  -> succeeded (using the CORRECT connection string typed manually)
  docker compose -p barq-assessment exec app-01 env | grep -E "DATABASE_URL|REDIS_URL"
  -> DATABASE_URL=postgresql://barq_app:BarqLabOnly_7qN2vK8d@postgres:5433/barq_tasks
  -> REDIS_URL=redis://redis:6380/0
- Actual output: Comparing the app's actual env var against postgres's real
  config in docker-compose.yml revealed three mismatches: password ends in
  "8d" instead of the real "8c", Postgres port is "5433" instead of the real
  "5432", and Redis port is "6380" instead of the real "6379".
- Failed attempt and what changed your thinking: Initially tested the raw
  connection with a manually-typed, correct connection string, which
  succeeded — this created a false impression that connectivity was fine.
  Only checking the ACTUAL env vars set inside the container (rather than
  assuming they matched what the app "should" have) revealed the real bug.
  Lesson: verify what the app is actually configured with, not just whether
  the dependency is reachable with known-good credentials.
- Root cause: config/app.env contained a wrong password character, wrong
  Postgres port, and wrong Redis port for DATABASE_URL and REDIS_URL.
- Fix: Corrected config/app.env:
  DATABASE_URL=postgresql://barq_app:BarqLabOnly_7qN2vK8c@postgres:5432/barq_tasks
  REDIS_URL=redis://redis:6379/0
  docker compose -p barq-assessment up -d --force-recreate app-01 app-02
- Retest evidence:
  curl http://127.0.0.1:8080/records
  -> {"records":[{"id":1,"title":"Review service readiness"},
      {"id":2,"title":"Document the operating procedure"}], ...}
  curl http://127.0.0.1:8080/counter -> {"counter":1, ...}
  curl http://127.0.0.1:8080/ready
  -> {"dependencies":{"postgres":"ready","redis":"ready"},"status":"ready",...}
- Related commit: [fill in after committing]
- Remaining uncertainty: None regarding basic connectivity. Note this
  DATABASE_URL/REDIS_URL was not caught during the original static review of
  docker-compose.yml, since these values live in the separate config/app.env
  file (referenced via env_file:), not inline in the compose file itself.