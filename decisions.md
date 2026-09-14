# Technical decisions

Record at least 5 decisions. Include assumptions and limits.

## Decision 1: App bind address (0.0.0.0 vs 127.0.0.1)
- Choice: Bind Flask to APP_HOST=0.0.0.0 instead of 127.0.0.1.
- Why: Containers communicate over Docker's internal network, not loopback.
  Binding to 127.0.0.1 made the app unreachable from any other container
  (including NGINX), confirmed via direct testing (see troubleshooting.md
  Entry 2).
- Alternative: Bind to a specific container IP instead of 0.0.0.0. Rejected
  because container IPs are dynamic and assigned by Docker at start time,
  making a hardcoded IP fragile and unnecessary.
- Trade-off: 0.0.0.0 accepts connections on every network interface inside
  the container, which is standard practice for containerized services but
  would be a real security concern on a bare-metal host with multiple
  network interfaces exposed directly to untrusted networks.
- Evidence / commit: bcae930
- Production improvement: In a production setup with a service mesh or
  sidecar proxy, mTLS or network policies would restrict which peers can
  actually reach 0.0.0.0, rather than relying on binding alone.

## Decision 2: NGINX failover policy (proxy_next_upstream)
- Choice: proxy_next_upstream error timeout http_502 http_503 http_504;
  with proxy_next_upstream_tries 2; and proxy_next_upstream_timeout 4s;
  instead of the original proxy_next_upstream off;.
- Why: The brief requires proving that traffic continues when one backend
  fails. With failover disabled, ~50% of requests failed outright when one
  backend was stopped (see troubleshooting.md Entry 5).
- Alternative: Leave proxy_next_upstream at its off setting and instead
  handle retries at the client/caller level. Rejected because it pushes
  complexity onto every client and contradicts the brief's requirement that
  NGINX itself prove failover.
- Trade-off: Automatic retries mean a single client request can silently hit
  two backends, which slightly increases worst-case latency (up to
  ~1s connect timeout + retry) but is preferable to an outright failure.
  proxy_next_upstream_tries 2 caps this so a fully-down cluster still fails
  fast rather than retrying indefinitely.
- Evidence / commit: e10a80c
- Production improvement: In production, this would be paired with active
  health checks (e.g. NGINX Plus or an external load balancer with
  health-check-based upstream removal) so failed backends are removed from
  rotation proactively, not just retried reactively per request.

## Decision 3: NGINX network isolation from PostgreSQL/Redis
- Choice: NGINX is only attached to the frontend network; it is not a
  member of the backend network where postgres and redis live.
- Why: The brief requires blocking direct NGINX access to PostgreSQL/Redis.
  Network membership alone determines reachability in Docker, so removing
  NGINX from backend is the simplest way to enforce this (see
  troubleshooting.md Entry 9).
- Alternative: Keep NGINX on both networks but rely on nginx.conf to simply
  never proxy to postgres/redis. Rejected because this only prevents
  accidental misconfiguration in nginx.conf — it does not prevent someone
  with shell access to the nginx container from directly reaching postgres
  or redis over the network.
- Trade-off: None significant; NGINX never needed backend access for its
  actual job (proxying to app-01/app-02, which are on both networks).
- Evidence / commit: 4041fbb
- Production improvement: In production, this would be reinforced further
  with actual network policies (e.g. Kubernetes NetworkPolicies or a
  service mesh) rather than relying solely on Docker Compose network
  membership, since Compose networks are relatively coarse-grained.

## Decision 4: PostgreSQL data volume mount path
- Choice: Mount the named volume postgres-data at
  /var/lib/postgresql/data (Postgres's actual data directory), removing the
  incorrect tmpfs mount that had been placed there.
- Why: The original configuration mounted the named (persistent) volume at
  /var/lib/postgresql/backup, an unused path, while the real data directory
  was on tmpfs (memory-backed, wiped on container removal). This meant data
  never actually persisted despite a volume being declared. Confirmed via
  direct before/after testing (see troubleshooting.md Entry 7).
- Alternative: Keep the backup-path volume as an actual backup staging
  area and add a separate volume for the real data directory. Considered
  for later (see Part 3 backup.sh/restore.sh work) but out of scope for
  this specific fix, which only needed to correct basic persistence.
- Trade-off: None — this only corrects a misconfiguration, it does not add
  meaningful complexity.
- Evidence / commit: fe9235b
- Production improvement: In production, this volume would be backed by
  managed, replicated block storage (e.g. cloud provider persistent disks)
  rather than a local Docker volume on a single host, and would be paired
  with automated periodic backups (addressed separately in backup.sh).

## Decision 5: Resource limits and restart policy
- Choice: Added deploy.resources.limits (CPU/memory ceilings) to every
  service, and changed restart: "no" to restart: unless-stopped across
  app-01, app-02, postgres, redis, and nginx.
- Why: The brief requires "correct... restart policies and resource
  limits." With restart: "no", a crashed container stayed dead
  indefinitely with no self-healing. With no resource limits, a runaway or
  buggy container could exhaust host resources and starve other services.
- Alternative: Use restart: always instead of unless-stopped. Rejected
  because always would restart a container even after a deliberate manual
  stop (e.g. during the Part 3 failure test), which would interfere with
  intentionally testing failure/recovery behavior.
- Trade-off: unless-stopped means a container manually stopped for testing
  will not auto-restart, which is desired for controlled failure testing,
  but means a genuinely crashed container also will not "surprise restart"
  if a human happened to have stopped something else nearby — this is an
  acceptable trade-off for a lab/assessment environment.
- Evidence / commit:a72b07f
- Production improvement: Resource limit values (256M/0.5 CPU for apps,
  512M/1.0 CPU for postgres, etc.) were chosen as reasonable defaults for a
  lab environment, not derived from load testing. In production these would
  be tuned using real traffic/query profiling, and paired with
  autoscaling/orchestration (e.g. Kubernetes resource requests/limits + HPA)
  rather than static per-container caps.

## Decision 6: Redis persistence (AOF enabled)
- Choice: Enabled Redis AOF persistence (--appendonly yes --appendfsync
  everysec) with a named volume (redis-data) mounted at /data, replacing
  the original --save "" --appendonly no configuration.
- Why: The brief asks to "configure Redis persistence where appropriate."
  The /counter value is the only data Redis holds; without persistence, it
  reset to zero on every Redis restart/recreation, which did not
  demonstrate real cache durability behavior. Confirmed via before/after
  testing (see troubleshooting.md Entry 11) — counter value 3 survived a
  full stop/remove/recreate of the redis container and continued to 4.
- Alternative: Leave persistence disabled, reasoning that a request counter
  is non-critical data with no business significance. This was the initial
  direction considered, but AOF was chosen instead to actually demonstrate
  configuring Redis persistence, since it is directly requested in Part 2.
- Trade-off: --appendfsync everysec means up to ~1 second of writes could
  be lost in a hard crash (versus always, which fsyncs every write but adds
  latency). everysec is the standard production-reasonable middle ground.
  AOF also adds a small amount of disk I/O and a named volume to manage.
- Evidence / commit: ce381cb
- Production improvement: In production, this would be paired with
  periodic AOF rewrites/compaction monitoring, and potentially RDB
  snapshots alongside AOF for faster full-restore scenarios, plus
  replication (Redis Sentinel/Cluster) for high availability rather than a
  single Redis instance.

## Decision 7: DATABASE_URL/REDIS_URL correction (config/app.env)
- Choice: Corrected the Postgres password, Postgres port, and Redis port in
  config/app.env to match the actual values configured for postgres and
  redis in docker-compose.yml.
- Why: The app returned postgres_unavailable on /records despite postgres
  being healthy with the correct schema and data. Investigation traced this
  to a wrong password character and wrong ports baked into the env file
  (see troubleshooting.md Entry 6), unrelated to any of the networking or
  bind-address issues fixed earlier.
- Alternative: None meaningful — this was a straightforward typo
  correction, not a design trade-off.
- Trade-off: None.
- Evidence / commit: 1264650
- Production improvement: In production, these values would come from a
  secrets manager (e.g. AWS Secrets Manager, HashiCorp Vault) rather than a
  plaintext .env file, with automated validation (e.g. a startup check that
  fails fast with a clear error if credentials are wrong) instead of
  surfacing only as a generic postgres_unavailable error at request time.