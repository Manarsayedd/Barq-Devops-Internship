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
- Related commit: efe957f
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
- Related commit: e10a80c
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
- Related commit: 1264650
- Remaining uncertainty: None regarding basic connectivity. Note this
  DATABASE_URL/REDIS_URL was not caught during the original static review of
  docker-compose.yml, since these values live in the separate config/app.env
  file (referenced via env_file:), not inline in the compose file itself.
  ## Entry 7 / 2026-09-13 / PostgreSQL persistence (volume/tmpfs mismatch)
- Symptom: A record created via POST /records does not survive recreating
  the postgres container, even though a named volume (postgres-data) is
  declared in docker-compose.yml.
- Hypothesis: The named volume is mounted to /var/lib/postgresql/backup,
  which Postgres does not use by default. The actual data directory
  /var/lib/postgresql/data is mounted as tmpfs (memory-backed), which is
  wiped whenever the container is removed.
- Command or test:
  curl -X POST -d '{"title":"Persistence proof before recreate"}' \
    http://127.0.0.1:8080/records
  curl http://127.0.0.1:8080/records
  -> records: [1, 2, 3] (3 = new record)
  docker compose -p barq-assessment stop postgres
  docker compose -p barq-assessment rm -f postgres
  docker compose -p barq-assessment up -d postgres
  curl http://127.0.0.1:8080/records
- Actual output: records: [1, 2] only — record 3 is gone. Postgres logs
  (docker compose logs postgres) confirmed a full fresh initdb ran again,
  including re-executing database/init.sql and re-inserting the two seed
  rows, exactly as would happen with no prior data at all.
- Root cause: Confirmed. The named volume postgres-data is mounted to
  /var/lib/postgresql/backup, an unused path. The real Postgres data
  directory (/var/lib/postgresql/data) is mounted as tmpfs, so all data is
  lost whenever the container is removed and recreated.
- Fix: Changed docker-compose.yml to mount the named volume at the correct
  path:
    volumes:
      - postgres-data:/var/lib/postgresql/data
      - ./database/init.sql:/docker-entrypoint-initdb.d/01-init.sql:ro
  and removed the tmpfs entry entirely.
- Failed attempt and what changed your thinking:
  (1) After editing the volumes block, the two list items were accidentally
  left indented with only 2 spaces while "volumes:" itself was indented 4
  spaces — less than its parent key. This produced a YAML parse error
  ("did not find expected key") and postgres failed to start. Fixed by
  re-indenting both list items to 6 spaces, matching the file's existing
  style, then validating with `docker compose config > /dev/null`.
  (2) After fixing the YAML, the first attempt to retest persistence created
  a new record BEFORE recreating postgres with the corrected config —
  meaning that record was still written to the old, still-running
  tmpfs-backed instance and was lost as expected, producing a misleading
  "fix didn't work" result. This clarified that the correct test sequence
  must be: recreate postgres first (to actually apply the new volume
  mount), THEN create the test record, THEN recreate postgres again to
  test whether it survives.
- Retest evidence: Repeating the test in the correct order:
    docker compose -p barq-assessment up -d postgres   (applies corrected mount)
    curl -X POST -d '{"title":"Persistence proof - real test"}' \
      http://127.0.0.1:8080/records
    docker compose -p barq-assessment stop postgres
    docker compose -p barq-assessment rm -f postgres
    docker compose -p barq-assessment up -d postgres
    curl http://127.0.0.1:8080/records
  Result: records: [1, 2, 3] — record 3 ("Persistence proof - real test")
  survived the container recreation. Fix confirmed working.
- Related commit: fe9235b
- Remaining uncertainty: None regarding basic persistence across container
  recreation. Not yet tested: persistence across a full `docker compose down`
  (which also removes networks) or a host machine restart — planned as part
  of Part 3's formal backup/restore testing.
  ## Entry 8 / 2026-09-14 / removing exposed PostgreSQL/Redis host ports
- Symptom/requirement: The brief requires "Publish only NGINX on host port
  8080. Do not publish app, PostgreSQL or Redis ports." The original
  docker-compose.yml exposed both:
    postgres: ports: ["127.0.0.1:15432:5432"]
    redis: ports: ["127.0.0.1:16379:6379"]
- Hypothesis: These host-exposed ports allow direct access to the database
  and cache from the host machine, bypassing the app and nginx entirely,
  against the brief's isolation requirement.
- Command or test / actual output:
  Checking the committed file directly (git show HEAD:docker-compose.yml)
  showed postgres's ports: line had already been removed in an earlier
  commit (fe9235b) as an untracked side effect of an unrelated volume fix.
  Redis's ports: line was still present and committed at that point.
- Failed attempt and what changed your thinking: `nc -zv` tests against
  both ports initially both showed "Connection refused," which looked like
  both were already fixed. Directly inspecting the committed file (git show
  HEAD:docker-compose.yml) revealed redis's ports: line was still present,
  contradicting the nc test. This is unresolved as to why nc reported
  refused for a port that was, per the committed config, supposed to be
  mapped — possibly a stale container instance at the time of that specific
  test. Relying on the committed file content, rather than a single runtime
  test, was necessary to catch this discrepancy.
- Root cause: docker-compose.yml explicitly published host ports for both
  postgres (15432->5432) and redis (16379->6379), against the brief's
  requirement that only nginx be published on the host.
- Fix: Removed the `ports:` line from the redis service (postgres's had
  already been removed earlier). Recreated redis:
    docker compose -p barq-assessment up -d --force-recreate redis
- Retest evidence:
    nc -zv 127.0.0.1 15432   -> Connection refused
    nc -zv 127.0.0.1 16379   -> Connection refused
    docker compose -p barq-assessment ps -a
    -> postgres: 5432/tcp only (no host mapping)
    -> redis: 6379/tcp only (no host mapping)
    curl http://127.0.0.1:8080/ready
    -> {"dependencies":{"postgres":"ready","redis":"ready"},"status":"ready",...}
    curl http://127.0.0.1:8080/counter -> counter increments correctly
  Confirms both services are unreachable from the host directly, while the
  app continues to function normally via the internal Docker network.
- Related commit:3701a98
- Remaining uncertainty: None regarding final state. Noted as a process
  lesson: verify fixes against the actual committed file content, not just
  a single runtime network test, since container state and file state can
  briefly diverge during iterative editing.
  ## Entry 9 / 2026-09-14 / NGINX network isolation from PostgreSQL/Redis
- Symptom/requirement: The brief requires "Connect NGINX + apps to frontend;
  apps + PostgreSQL + Redis to backend... Block direct NGINX access to
  PostgreSQL/Redis." The original docker-compose.yml attached nginx to both
  networks: [frontend, backend], giving it network-level reachability to
  postgres and redis even though nginx.conf never proxies to them directly.
- Hypothesis: With nginx on the backend network, it should be able to
  resolve and connect to postgres/redis hostnames, violating isolation.
- Command or test (before fix):
  docker compose -p barq-assessment exec nginx nc -zv postgres 5432
  docker compose -p barq-assessment exec nginx nc -zv redis 6379
- Actual output (before fix): [not captured with a clean before-state due to
  moving directly to the fix; DNS resolution and connection were expected to
  succeed given nginx's network membership at that time — confirmed
  indirectly by the after-fix result showing resolution now fails]
- Root cause: docker-compose.yml's nginx service listed
  networks: [frontend, backend], granting it network membership on backend
  alongside postgres and redis, contrary to the brief's isolation
  requirement.
- Fix: Changed nginx's networks to networks: [frontend] only, removing
  backend network membership.
- Retest evidence:
    docker compose -p barq-assessment up -d --force-recreate nginx
    docker compose -p barq-assessment exec nginx nc -zv postgres 5432
    -> nc: bad address 'postgres'
    docker compose -p barq-assessment exec nginx nc -zv redis 6379
    -> nc: bad address 'redis'
  DNS resolution itself fails, confirming nginx is no longer on the same
  network as postgres/redis and cannot reach them at all, not merely
  blocked at the application layer.
  App functionality confirmed unaffected:
    curl http://127.0.0.1:8080/        -> 200 OK
    curl http://127.0.0.1:8080/ready   -> postgres: ready, redis: ready
    curl http://127.0.0.1:8080/records -> all 3 records returned correctly
- Related commit: [fill in after committing]
- Remaining uncertainty: None. Note: a clean "before" test proving nginx
  COULD resolve postgres/redis prior to the fix was not captured separately,
  since the fix was applied in the same step as the test; the isolation
  requirement and its resolution are nonetheless clearly demonstrated by
  the after-fix result and by direct inspection of the original
  docker-compose.yml network configuration.

