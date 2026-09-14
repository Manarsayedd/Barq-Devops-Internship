#!/usr/bin/env python3
import json
import re
from collections import defaultdict
from pathlib import Path

LOGS = Path("logs")

def parse_access(path):
    valid, malformed, seen_lines = [], [], defaultdict(int)
    with open(path) as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            seen_lines[line] += 1
            try:
                rec = json.loads(line)
                required = ["timestamp", "request_id", "method", "path", "status",
                            "upstream", "upstream_status", "request_time", "client"]
                if not all(k in rec for k in required):
                    malformed.append((lineno, line, "missing_field"))
                    continue
                rec["_lineno"] = lineno
                valid.append(rec)
            except json.JSONDecodeError as e:
                malformed.append((lineno, line, f"json_error:{e}"))
    duplicate_lines = {k: v for k, v in seen_lines.items() if v > 1}
    return valid, malformed, duplicate_lines

def parse_application(path):
    valid, malformed, seen_lines = [], [], defaultdict(int)
    with open(path) as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            seen_lines[line] += 1
            try:
                rec = json.loads(line)
                required = ["timestamp", "level", "event", "request_id"]
                if not all(k in rec for k in required):
                    malformed.append((lineno, line, "missing_field"))
                    continue
                rec["_lineno"] = lineno
                valid.append(rec)
            except json.JSONDecodeError as e:
                malformed.append((lineno, line, f"json_error:{e}"))
    duplicate_lines = {k: v for k, v in seen_lines.items() if v > 1}
    return valid, malformed, duplicate_lines

ERROR_RE = re.compile(
    r'^(?P<ts>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}) \[(?P<level>\w+)\] '
    r'(?P<pid>\d+)#(?P<tid>\d+): \*(?P<conn>\d+) (?P<msg>.*?), '
    r'request_id=(?P<request_id>[\w-]+), request: "(?P<request>[^"]*)", '
    r'upstream: "(?P<upstream>[^"]*)"'
)

def parse_error(path):
    valid, malformed, seen_lines = [], [], defaultdict(int)
    with open(path) as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            seen_lines[line] += 1
            m = ERROR_RE.match(line)
            if not m:
                malformed.append((lineno, line, "no_regex_match"))
                continue
            rec = m.groupdict()
            rec["_lineno"] = lineno
            valid.append(rec)
    duplicate_lines = {k: v for k, v in seen_lines.items() if v > 1}
    return valid, malformed, duplicate_lines

def main():
    access, access_bad, access_dup = parse_access(LOGS / "access.log")
    app, app_bad, app_dup = parse_application(LOGS / "application.log")
    err, err_bad, err_dup = parse_error(LOGS / "error.log")

    print("=== Q1: coverage & counts ===")
    if access:
        times = sorted(r["timestamp"] for r in access)
        print(f"access.log UTC range: {times[0]} .. {times[-1]}")
    print(f"access.log: valid={len(access)} malformed={len(access_bad)} duplicate_lines={sum(access_dup.values())}")
    print(f"application.log: valid={len(app)} malformed={len(app_bad)} duplicate_lines={sum(app_dup.values())}")
    print(f"error.log: valid={len(err)} malformed={len(err_bad)} duplicate_lines={sum(err_dup.values())}")
    if access_bad:
        print("Sample malformed access.log lines:", access_bad[:3])
    if app_bad:
        print("Sample malformed application.log lines:", app_bad[:3])
    if err_bad:
        print("Sample malformed error.log lines:", err_bad[:3])

    print("\n=== Q2: distinct client requests ===")
    by_rid = defaultdict(list)
    for r in access:
        by_rid[r["request_id"]].append(r)
    distinct = len(by_rid)
    retried = {rid: recs for rid, recs in by_rid.items()
               if len(recs) > 1 or "," in recs[0]["upstream"]}
    print(f"distinct request_ids in access.log: {distinct}")
    print(f"request_ids with >1 line OR comma-separated upstream (retry indicator): {len(retried)}")

    print("\n=== Q2 (refined): duplicate lines vs real retries ===")
    literal_dupe_rids = [rid for rid, recs in by_rid.items() if len(recs) > 1]
    real_retry_rids = [rid for rid, recs in by_rid.items() if "," in recs[-1]["upstream"]]
    print(f"request_ids with duplicate log LINES (same request logged twice): {len(literal_dupe_rids)}")
    print(f"request_ids with an actual retry (comma-separated upstream): {len(real_retry_rids)}")

    print("\n=== Q3: final client status counts & error rate ===")
    status_counts = defaultdict(int)
    for rid, recs in by_rid.items():
        final = recs[-1]
        status_counts[final["status"]] += 1
    total = sum(status_counts.values())
    errors = sum(v for k, v in status_counts.items() if int(k) >= 400)
    print("status counts:", dict(sorted(status_counts.items())))
    print(f"denominator (distinct requests): {total}")
    print(f"error rate (status>=400 / total): {errors}/{total} = {errors/total:.4f}")

    print("\n=== Q4: failures by path/time/backend ===")
    fail_by_path = defaultdict(int)
    fail_by_backend = defaultdict(int)
    fail_by_hour = defaultdict(int)
    for rid, recs in by_rid.items():
        final = recs[-1]
        if int(final["status"]) >= 400:
            fail_by_path[final["path"]] += 1
            fail_by_backend[final["upstream"]] += 1
            hour = final["timestamp"][:13]
            fail_by_hour[hour] += 1
    print("failures by path:", dict(fail_by_path))
    print("failures by upstream:", dict(fail_by_backend))
    print("failures by hour:", dict(sorted(fail_by_hour.items())))

    print("\n=== Q5: latency percentiles ===")
    times_list = sorted(float(recs[-1]["request_time"]) for recs in by_rid.values())
    n = len(times_list)
    def pct(p):
        if n == 0:
            return None
        k = (n - 1) * p
        f, c = int(k), min(int(k) + 1, n - 1)
        if f == c:
            return times_list[f]
        return times_list[f] + (times_list[c] - times_list[f]) * (k - f)
    print(f"n={n} median(p50)={pct(0.5):.4f}s p95={pct(0.95):.4f}s "
          f"(linear-interpolation, seconds, source=access.log request_time)")

    print("\n=== Q6: retried requests ===")
    retry_success = 0
    retry_total = 0
    for rid, recs in by_rid.items():
        final = recs[-1]
        if "," in final["upstream"]:
            retry_total += 1
            if int(final["status"]) < 400:
                retry_success += 1
    print(f"requests with comma-separated (retried) upstream: {retry_total}, succeeded after retry: {retry_success}")

    print("\n=== Q7/Q8 helper: incident timeline sample ===")
    failed_5xx = [recs[-1] for rid, recs in by_rid.items() if int(recs[-1]["status"]) >= 500]
    failed_404 = [recs[-1] for rid, recs in by_rid.items() if int(recs[-1]["status"]) == 404]
    failed_5xx.sort(key=lambda r: r["timestamp"])
    failed_404.sort(key=lambda r: r["timestamp"])
    success_examples = [recs[-1] for rid, recs in by_rid.items() if int(recs[-1]["status"]) < 400]
    success_examples.sort(key=lambda r: r["timestamp"])

    if failed_5xx:
        f = failed_5xx[0]
        print("First 5xx (outage-related) failure:", f["request_id"], f["timestamp"], f["path"], f["status"], f["upstream"])
        last = failed_5xx[-1]
        print("Last 5xx (outage-related) failure:", last["request_id"], last["timestamp"], last["path"], last["status"], last["upstream"])
        after = [r for r in success_examples if r["timestamp"] > last["timestamp"]]
        if after:
            s = after[0]
            print("First success after last 5xx failure:", s["request_id"], s["timestamp"], s["path"], s["status"], s["upstream"])
    print(f"Total 404s (client errors, not outage-related): {len(failed_404)}")
    if failed_404:
        print("Sample 404:", failed_404[0]["request_id"], failed_404[0]["timestamp"], failed_404[0]["path"])

    print("\n=== Q9 helper: error.log entries by upstream ===")
    err_upstreams = defaultdict(int)
    for e in err:
        err_upstreams[e["upstream"]] += 1
    print("error.log entries by upstream:", dict(err_upstreams))
    if err:
        print(f"error.log time range: {err[0]['ts']} .. {err[-1]['ts']}")

if __name__ == "__main__":
    main()