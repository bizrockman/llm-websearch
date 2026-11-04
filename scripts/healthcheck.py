#!/usr/bin/env python3
"""Deterministic health check for an OrioSearch deployment.

Built for cron: standard library only, a meaningful exit code, and silence on
success with --quiet, so cron only mails when something needs attention.

Exit codes:
  0  OK    everything works
  1  WARN  degraded, action recommended (stale SearXNG, failing engines,
           cache not hitting)
  2  FAIL  broken (API down, auth off, no search results, extraction broken)

Configuration, from the environment or an --env-file with KEY=VALUE lines:
  ORIO_URL      base URL, e.g. https://llm-websearch.example.com
  ORIO_API_KEY  bearer key

Crontab example (Mondays 07:00, mail only when something is off):
  MAILTO=you@example.com
  0 7 * * 1  python3 /opt/orio/healthcheck.py --env-file /etc/orio-healthcheck.env --quiet

The thresholds encode what this deployment has taught us:
  - Engines fail in two ways. "too many requests" is rate limiting and heals
    on its own; anything else (CAPTCHA, access denied, HTTP error) does not.
    Only the second kind is worth a warning, and only if it survives an
    individual re-probe, because upstream engines produce one-off hiccups.
  - A stale SearXNG image is the most common root cause of failing engines:
    the scrapers are fixed upstream continuously, and Coolify does not watch
    the registry, so nothing updates without a redeploy.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Optional

# A query every healthy general-purpose engine has something to say about,
# and a page that will not change. Both are deliberately boring.
STABLE_QUERY = "python asyncio taskgroup"
STABLE_URL = "https://peps.python.org/pep-0020/"

DOCKER_HUB_TAGS = (
    "https://hub.docker.com/v2/repositories/searxng/searxng/tags"
    "?page_size=25&ordering=last_updated"
)

# Engine failure reasons that clear by themselves. Matched case-insensitively
# as substrings, since SearXNG prefixes suspended engines ("Suspended: ...").
TRANSIENT_MARKERS = ("too many requests", "timeout")

LEVELS = {"OK": 0, "SKIP": 0, "WARN": 1, "FAIL": 2}

_DATE_PREFIX = re.compile(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})")


@dataclass
class Check:
    name: str
    level: str
    message: str


# ---- Pure helpers (unit tested) ---------------------------------------------

def parse_version_date(version: Optional[str]) -> Optional[date]:
    """SearXNG versions and image tags both lead with the release date:
    '2026.9.25+12f8b6515' (running) and '2026.11.2-cccbc9e15' (registry)."""
    if not version:
        return None
    m = _DATE_PREFIX.match(version.strip())
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def latest_dated_tag(tag_names: list[str]) -> Optional[tuple[str, date]]:
    """Newest release tag, ignoring floating ones like 'latest'."""
    dated = [(n, parse_version_date(n)) for n in tag_names]
    dated = [(n, d) for n, d in dated if d is not None]
    if not dated:
        return None
    return max(dated, key=lambda nd: nd[1])


def is_transient(reason: Optional[str]) -> bool:
    r = (reason or "").lower()
    return any(m in r for m in TRANSIENT_MARKERS)


def overall_level(checks: list[Check]) -> str:
    worst = max((LEVELS[c.level] for c in checks), default=0)
    return {0: "OK", 1: "WARN", 2: "FAIL"}[worst]


def load_env_file(path: str) -> None:
    """KEY=VALUE lines, '#' comments, optional 'export ' prefix and quotes.
    Values already set in the real environment win."""
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            key, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key.strip(), value)


# ---- HTTP ---------------------------------------------------------------------

def http(method, url, *, headers=None, body=None, timeout=60):
    """Returns (status, headers, body_bytes). HTTP errors are returned, not
    raised; only transport failures (DNS, refused, timeout) raise."""
    data = json.dumps(body).encode() if body is not None else None
    h = {"User-Agent": "orio-healthcheck/1"}
    if body is not None:
        h["Content-Type"] = "application/json"
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, (e.read() if e.fp else b"")


# ---- Checks -------------------------------------------------------------------

def check_health(base: str) -> Check:
    try:
        status, _, raw = http("GET", base + "/health", timeout=20)
    except Exception as e:
        return Check("health", "FAIL", f"unreachable: {e}")
    if status == 200 and json.loads(raw or b"{}").get("status") == "ok":
        return Check("health", "OK", "api up")
    return Check("health", "FAIL", f"/health returned HTTP {status}")


def check_auth(base: str) -> Check:
    # Rejected by the auth dependency before any upstream query runs.
    status, _, _ = http("GET", base + "/engines", timeout=30)
    if status in (401, 403):
        return Check("auth", "OK", f"rejects requests without key ({status})")
    if status == 200:
        return Check("auth", "FAIL", "accepts requests without a key - auth is off")
    return Check("auth", "WARN", f"unexpected HTTP {status} without key")


def check_engines(base: str, auth: dict, min_engines: int) -> tuple[Check, Optional[str]]:
    status, _, raw = http("GET", base + "/engines", headers=auth, timeout=90)
    if status != 200:
        return Check("engines", "FAIL", f"/engines returned HTTP {status}"), None
    d = json.loads(raw)
    version = d.get("backend_version")
    delivering = d.get("delivering") or []
    failing = d.get("failing") or []

    transient = [e for e in failing if is_transient(e.get("reason"))]
    suspect = [e for e in failing if not is_transient(e.get("reason"))]

    # Re-probe the non-transient failures individually before warning: a single
    # parse error from an upstream engine is common and clears on its own.
    persistent: dict[str, str] = {}
    if suspect:
        names = ",".join(e["name"] for e in suspect)
        s2, _, raw2 = http(
            "GET", base + "/engines?probe=" + urllib.parse.quote(names),
            headers=auth, timeout=90,
        )
        if s2 == 200:
            for e in json.loads(raw2).get("failing") or []:
                if not is_transient(e.get("reason")):
                    persistent[e["name"]] = e.get("reason") or "?"
        else:
            persistent = {e["name"]: e.get("reason") or "?" for e in suspect}

    n = len(delivering)
    parts = [f"{n} delivering (min {min_engines})"]
    if persistent:
        parts.append("failing: " + ", ".join(f"{k} ({v})" for k, v in persistent.items()))
    if transient:
        parts.append("rate-limited, ignored: " + ", ".join(e["name"] for e in transient))
    message = "; ".join(parts)

    if n == 0:
        return Check("engines", "FAIL", message), version
    if n < min_engines or persistent:
        return Check("engines", "WARN", message), version
    return Check("engines", "OK", message), version


def check_version(running: Optional[str], max_lag_days: int) -> Check:
    running_date = parse_version_date(running)
    if running_date is None:
        return Check("version", "WARN", f"SearXNG version unknown ({running!r})")
    try:
        status, _, raw = http("GET", DOCKER_HUB_TAGS, timeout=30)
        if status != 200:
            raise RuntimeError(f"HTTP {status}")
        names = [t["name"] for t in json.loads(raw).get("results", [])]
    except Exception as e:
        return Check("version", "WARN", f"{running} running; Docker Hub unreachable ({e})")
    latest = latest_dated_tag(names)
    if latest is None:
        return Check("version", "WARN", f"{running} running; no dated tag found on Docker Hub")
    latest_name, latest_date = latest
    lag = (latest_date - running_date).days
    if lag <= max_lag_days:
        return Check("version", "OK", f"{running} ({max(lag, 0)} days behind {latest_name})")
    return Check(
        "version", "WARN",
        f"{running} running, {latest_name} available ({lag} days behind) - redeploy to update",
    )


def check_search(base: str, auth: dict) -> tuple[Check, int]:
    t0 = time.perf_counter()
    status, _, raw = http(
        "POST", base + "/search", headers=auth,
        body={"query": STABLE_QUERY, "max_results": 3}, timeout=90,
    )
    dt = time.perf_counter() - t0
    if status != 200:
        return Check("search", "FAIL", f"/search returned HTTP {status}"), 0
    n = len(json.loads(raw).get("results") or [])
    if n == 0:
        return Check("search", "FAIL", f"0 results for {STABLE_QUERY!r}"), 0
    return Check("search", "OK", f"{n} results in {dt:.1f}s"), n


def check_cache(base: str, auth: dict, prior_results: int) -> Check:
    if prior_results == 0:
        # Failed or empty searches leave nothing to cache (empty responses are
        # deliberately never cached), so there is nothing to measure here.
        return Check("cache", "SKIP", "search did not succeed")
    status, headers, _ = http(
        "POST", base + "/search", headers=auth,
        body={"query": STABLE_QUERY, "max_results": 3}, timeout=90,
    )
    source = headers.get("X-Orio-Source") if headers else None
    if status == 200 and source == "redis":
        return Check("cache", "OK", "repeat query served from redis")
    return Check("cache", "WARN", f"repeat query served from {source!r} (HTTP {status}), expected redis")


def check_extract(base: str, auth: dict) -> Check:
    # force_refresh skips Redis and Meilisearch, so this exercises the real
    # path: network fetch, decompression, Trafilatura.
    status, _, raw = http(
        "POST", base + "/extract", headers=auth,
        body={"urls": [STABLE_URL], "force_refresh": True}, timeout=90,
    )
    if status != 200:
        return Check("extract", "FAIL", f"/extract returned HTTP {status}")
    d = json.loads(raw)
    if d.get("failed_results"):
        err = d["failed_results"][0].get("error", "?")
        return Check("extract", "FAIL", f"extraction failed: {err}")
    content = ((d.get("results") or [{}])[0].get("raw_content") or "")
    if len(content) < 200:
        return Check("extract", "FAIL", f"only {len(content)} chars extracted")
    # Undecoded compressed bodies (the missing-brotli bug) surface as U+FFFD.
    if "\ufffd" in content[:2000]:
        return Check("extract", "FAIL", "content looks garbled - decompression broken?")
    return Check("extract", "OK", f"{len(content)} chars fetched from origin")


# ---- Main ---------------------------------------------------------------------

def run(base: str, key: str, min_engines: int, max_lag_days: int) -> list[Check]:
    checks = [check_health(base)]
    if checks[0].level == "FAIL":
        for name in ("auth", "engines", "version", "search", "cache", "extract"):
            checks.append(Check(name, "SKIP", "api unreachable"))
        return checks

    auth = {"Authorization": f"Bearer {key}"}
    try:
        checks.append(check_auth(base))
        engines, version = check_engines(base, auth, min_engines)
        checks.append(engines)
        if engines.level == "FAIL" and version is None:
            # The version comes from /engines; if that failed there is nothing
            # to compare, and "unknown" would read like a separate problem.
            checks.append(Check("version", "SKIP", "/engines did not answer"))
        else:
            checks.append(check_version(version, max_lag_days))
        search, n = check_search(base, auth)
        checks.append(search)
        checks.append(check_cache(base, auth, n))
        checks.append(check_extract(base, auth))
    except Exception as e:
        # A transport failure mid-run: report it rather than crash, so cron
        # still gets a readable message and a FAIL exit code.
        checks.append(Check("run", "FAIL", f"aborted: {e}"))
    return checks


def main() -> int:
    p = argparse.ArgumentParser(description="Health check for an OrioSearch deployment.")
    p.add_argument("--env-file", help="file with ORIO_URL / ORIO_API_KEY lines")
    p.add_argument("--min-engines", type=int, default=4,
                   help="warn below this many delivering engines (default 4)")
    p.add_argument("--max-lag-days", type=int, default=14,
                   help="warn when SearXNG trails the newest release by more (default 14)")
    p.add_argument("--quiet", action="store_true",
                   help="print nothing when everything is OK (for cron mail)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    args = p.parse_args()

    if args.env_file:
        load_env_file(args.env_file)
    base = (os.environ.get("ORIO_URL") or "").rstrip("/")
    key = os.environ.get("ORIO_API_KEY") or ""
    if not base or not key:
        print("ORIO_URL and ORIO_API_KEY must be set (environment or --env-file)", file=sys.stderr)
        return 2

    checks = run(base, key, args.min_engines, args.max_lag_days)
    result = overall_level(checks)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if args.json:
        print(json.dumps({
            "url": base, "time": stamp, "result": result,
            "checks": [asdict(c) for c in checks],
        }, indent=2))
    elif not (args.quiet and result == "OK"):
        print(f"orio healthcheck  {base}  {stamp}")
        for c in checks:
            print(f"  {c.level:<5} {c.name:<8} {c.message}")
        print(f"RESULT: {result}")

    return LEVELS[result]


if __name__ == "__main__":
    sys.exit(main())
