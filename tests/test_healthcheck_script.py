"""Tests for scripts/healthcheck.py.

The script lives outside the app package (it runs from cron, not inside the
API), so it is loaded by path. Network calls are replaced by patching the
module's `http` function with a scripted fake.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import date
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "healthcheck.py"
_spec = importlib.util.spec_from_file_location("healthcheck", _PATH)
hc = importlib.util.module_from_spec(_spec)
# @dataclass resolves string annotations through sys.modules, which a module
# loaded by path is not in until registered.
sys.modules["healthcheck"] = hc
_spec.loader.exec_module(hc)


# ---- Version handling ------------------------------------------------------

class TestParseVersionDate:

    def test_running_version_format(self):
        assert hc.parse_version_date("2026.9.25+12f8b6515") == date(2026, 9, 25)

    def test_registry_tag_format(self):
        assert hc.parse_version_date("2026.11.2-cccbc9e15") == date(2026, 11, 2)

    @pytest.mark.parametrize("value", [None, "", "latest", "main", "v1.2.3"])
    def test_non_dated_values(self, value):
        assert hc.parse_version_date(value) is None

    def test_impossible_date_is_rejected_not_crashing(self):
        assert hc.parse_version_date("2026.13.45-abc") is None


class TestLatestDatedTag:

    def test_compares_as_dates_not_strings(self):
        """As text, '2026.9.25' sorts after '2026.11.2' because '9' > '1'.
        Getting this wrong would report a stale deployment as current."""
        name, d = hc.latest_dated_tag(["2026.9.25-aaa", "2026.11.2-bbb", "2026.10.30-ccc"])
        assert name == "2026.11.2-bbb"
        assert d == date(2026, 11, 2)

    def test_floating_tags_are_ignored(self):
        name, _ = hc.latest_dated_tag(["latest", "2026.11.1-x", "edge"])
        assert name == "2026.11.1-x"

    def test_no_dated_tags(self):
        assert hc.latest_dated_tag(["latest"]) is None
        assert hc.latest_dated_tag([]) is None


# ---- Classification and aggregation ----------------------------------------

class TestTransient:

    @pytest.mark.parametrize("reason", [
        "too many requests", "Suspended: too many requests", "timeout", "Suspended: timeout",
    ])
    def test_rate_limits_and_timeouts_heal_on_their_own(self, reason):
        assert hc.is_transient(reason)

    @pytest.mark.parametrize("reason", [
        "CAPTCHA", "Suspended: CAPTCHA", "access denied", "HTTP error", "parsing error", None,
    ])
    def test_everything_else_needs_attention(self, reason):
        assert not hc.is_transient(reason)


class TestOverallLevel:

    def test_worst_check_wins(self):
        checks = [hc.Check("a", "OK", ""), hc.Check("b", "WARN", ""), hc.Check("c", "FAIL", "")]
        assert hc.overall_level(checks) == "FAIL"

    def test_skip_does_not_escalate(self):
        assert hc.overall_level([hc.Check("a", "OK", ""), hc.Check("b", "SKIP", "")]) == "OK"

    def test_empty_is_ok(self):
        assert hc.overall_level([]) == "OK"


# ---- Env file --------------------------------------------------------------

def test_env_file_parsing(tmp_path, monkeypatch):
    monkeypatch.delenv("ORIO_URL", raising=False)
    monkeypatch.delenv("ORIO_API_KEY", raising=False)
    monkeypatch.setenv("ORIO_PRESET", "from-environment")
    f = tmp_path / "orio.env"
    f.write_text(
        "# comment\n"
        "\n"
        "export ORIO_URL=https://example.com\n"
        'ORIO_API_KEY="quoted-key"\n'
        "ORIO_PRESET=from-file\n",
        encoding="utf-8",
    )
    hc.load_env_file(str(f))
    assert os.environ["ORIO_URL"] == "https://example.com"
    assert os.environ["ORIO_API_KEY"] == "quoted-key"
    # The real environment wins, so a cron override is not silently ignored.
    assert os.environ["ORIO_PRESET"] == "from-environment"


# ---- Checks against a scripted backend -------------------------------------

class _Headers(dict):
    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


def _fake_http(routes):
    """routes: list of (predicate(method, url), (status, headers, body)).
    First match wins; body may be a dict (JSON-encoded) or bytes."""
    calls = []

    def http(method, url, *, headers=None, body=None, timeout=60):
        calls.append((method, url, body))
        for match, (status, hdrs, payload) in routes:
            if match(method, url):
                raw = json.dumps(payload).encode() if isinstance(payload, dict) else payload
                return status, _Headers(hdrs or {}), raw
        raise AssertionError(f"unexpected request {method} {url}")

    http.calls = calls
    return http


def _engines_payload(delivering, failing, version="2026.11.2+abc"):
    return {
        "backend_version": version,
        "delivering": [{"name": n, "results": r, "ok": True} for n, r in delivering],
        "failing": [{"name": n, "results": 0, "ok": False, "reason": why} for n, why in failing],
        "silent": [],
    }


class TestCheckEngines:

    def test_healthy_set_is_ok(self, monkeypatch):
        monkeypatch.setattr(hc, "http", _fake_http([
            (lambda m, u: "/engines" in u,
             (200, {}, _engines_payload([("a", 10), ("b", 9), ("c", 8), ("d", 7)], []))),
        ]))
        check, version = hc.check_engines("https://x", {}, min_engines=4)
        assert check.level == "OK"
        assert version == "2026.11.2+abc"

    def test_rate_limited_engines_do_not_warn(self, monkeypatch):
        """brave flips between 429 and fine; warning on it weekly is noise."""
        fake = _fake_http([
            (lambda m, u: "/engines" in u,
             (200, {}, _engines_payload(
                 [("a", 1), ("b", 1), ("c", 1), ("d", 1)],
                 [("brave", "Suspended: too many requests")]))),
        ])
        monkeypatch.setattr(hc, "http", fake)
        check, _ = hc.check_engines("https://x", {}, min_engines=4)
        assert check.level == "OK"
        assert "brave" in check.message            # still reported, just not escalated
        assert not any("probe=" in u for _, u, _ in fake.calls)   # no recheck needed

    def test_persistent_failure_survives_recheck_and_warns(self, monkeypatch):
        fake = _fake_http([
            (lambda m, u: "probe=" in u,
             (200, {}, _engines_payload([], [("yep", "access denied")]))),
            (lambda m, u: "/engines" in u,
             (200, {}, _engines_payload(
                 [("a", 1), ("b", 1), ("c", 1), ("d", 1)], [("yep", "access denied")]))),
        ])
        monkeypatch.setattr(hc, "http", fake)
        check, _ = hc.check_engines("https://x", {}, min_engines=4)
        assert check.level == "WARN"
        assert "yep (access denied)" in check.message

    def test_one_off_failure_that_recovers_on_recheck_is_ok(self, monkeypatch):
        """Upstream engines throw occasional parse errors that clear at once.
        The re-probe keeps those from turning into weekly false alarms."""
        fake = _fake_http([
            (lambda m, u: "probe=" in u,
             (200, {}, _engines_payload([("duckduckgo web", 10)], []))),
            (lambda m, u: "/engines" in u,
             (200, {}, _engines_payload(
                 [("a", 1), ("b", 1), ("c", 1), ("d", 1)],
                 [("duckduckgo web", "parsing error")]))),
        ])
        monkeypatch.setattr(hc, "http", fake)
        check, _ = hc.check_engines("https://x", {}, min_engines=4)
        assert check.level == "OK"

    def test_too_few_engines_warns(self, monkeypatch):
        monkeypatch.setattr(hc, "http", _fake_http([
            (lambda m, u: "/engines" in u, (200, {}, _engines_payload([("a", 5), ("b", 5)], []))),
        ]))
        check, _ = hc.check_engines("https://x", {}, min_engines=4)
        assert check.level == "WARN"

    def test_no_engines_fails(self, monkeypatch):
        monkeypatch.setattr(hc, "http", _fake_http([
            (lambda m, u: "probe=" in u, (200, {}, _engines_payload([], [("x", "CAPTCHA")]))),
            (lambda m, u: "/engines" in u, (200, {}, _engines_payload([], [("x", "CAPTCHA")]))),
        ]))
        check, _ = hc.check_engines("https://x", {}, min_engines=4)
        assert check.level == "FAIL"


class TestCheckVersion:

    def _hub(self, monkeypatch, tags):
        monkeypatch.setattr(hc, "http", _fake_http([
            (lambda m, u: "hub.docker.com" in u, (200, {}, {"results": [{"name": t} for t in tags]})),
        ]))

    def test_current_is_ok(self, monkeypatch):
        self._hub(monkeypatch, ["latest", "2026.11.2-aaa"])
        assert hc.check_version("2026.11.1+bbb", max_lag_days=14).level == "OK"

    def test_stale_warns_with_action(self, monkeypatch):
        self._hub(monkeypatch, ["latest", "2026.11.2-aaa"])
        check = hc.check_version("2026.9.25+bbb", max_lag_days=14)
        assert check.level == "WARN"
        assert "38 days behind" in check.message
        assert "redeploy" in check.message

    def test_unreachable_registry_warns_instead_of_failing(self, monkeypatch):
        """Docker Hub is an outside dependency; its outage is not ours."""
        def broken(*a, **k):
            raise OSError("network down")
        monkeypatch.setattr(hc, "http", broken)
        check = hc.check_version("2026.11.1+bbb", max_lag_days=14)
        assert check.level == "WARN"
        assert "unreachable" in check.message


class TestCheckExtract:

    def _extract(self, monkeypatch, payload, status=200):
        monkeypatch.setattr(hc, "http", _fake_http([
            (lambda m, u: "/extract" in u, (status, {}, payload)),
        ]))

    def test_readable_content_is_ok(self, monkeypatch):
        self._extract(monkeypatch, {"results": [{"raw_content": "Beautiful is better. " * 40}]})
        assert hc.check_extract("https://x", {}).level == "OK"

    def test_garbled_content_fails(self, monkeypatch):
        """The missing-brotli bug produced replacement characters instead of
        text, while still returning 200 and a non-empty body."""
        self._extract(monkeypatch, {"results": [{"raw_content": "x\ufffd\ufffd" * 200}]})
        check = hc.check_extract("https://x", {})
        assert check.level == "FAIL"
        assert "garbled" in check.message

    def test_failed_result_fails(self, monkeypatch):
        self._extract(monkeypatch, {"results": [], "failed_results": [{"url": "u", "error": "Timeout"}]})
        check = hc.check_extract("https://x", {})
        assert check.level == "FAIL"
        assert "Timeout" in check.message


class TestCheckCache:

    def test_redis_hit_is_ok(self, monkeypatch):
        monkeypatch.setattr(hc, "http", _fake_http([
            (lambda m, u: "/search" in u, (200, {"X-Orio-Source": "redis"}, {"results": []})),
        ]))
        assert hc.check_cache("https://x", {}, prior_results=3).level == "OK"

    def test_miss_warns(self, monkeypatch):
        monkeypatch.setattr(hc, "http", _fake_http([
            (lambda m, u: "/search" in u, (200, {"X-Orio-Source": "web"}, {"results": []})),
        ]))
        assert hc.check_cache("https://x", {}, prior_results=3).level == "WARN"

    def test_skipped_when_search_failed(self, monkeypatch):
        monkeypatch.setattr(hc, "http", _fake_http([]))   # must not be called
        assert hc.check_cache("https://x", {}, prior_results=0).level == "SKIP"


# ---- Output modes ----------------------------------------------------------

class TestMain:

    def _run(self, monkeypatch, capsys, checks, argv):
        monkeypatch.setenv("ORIO_URL", "https://x")
        monkeypatch.setenv("ORIO_API_KEY", "k")
        monkeypatch.setattr(hc, "run", lambda *a, **k: checks)
        monkeypatch.setattr("sys.argv", ["healthcheck.py", *argv])
        code = hc.main()
        return code, capsys.readouterr().out

    def test_quiet_is_silent_when_ok(self, monkeypatch, capsys):
        """Cron mails whatever is printed; OK must produce no mail."""
        code, out = self._run(monkeypatch, capsys, [hc.Check("a", "OK", "")], ["--quiet"])
        assert code == 0
        assert out == ""

    def test_quiet_still_reports_problems(self, monkeypatch, capsys):
        code, out = self._run(monkeypatch, capsys, [hc.Check("a", "WARN", "stale")], ["--quiet"])
        assert code == 1
        assert "stale" in out

    def test_exit_code_follows_worst_check(self, monkeypatch, capsys):
        code, _ = self._run(monkeypatch, capsys,
                            [hc.Check("a", "OK", ""), hc.Check("b", "FAIL", "")], [])
        assert code == 2

    def test_json_output(self, monkeypatch, capsys):
        code, out = self._run(monkeypatch, capsys, [hc.Check("a", "WARN", "m")], ["--json"])
        data = json.loads(out)
        assert data["result"] == "WARN"
        assert data["checks"] == [{"name": "a", "level": "WARN", "message": "m"}]

    def test_missing_config_is_a_failure(self, monkeypatch, capsys):
        monkeypatch.delenv("ORIO_URL", raising=False)
        monkeypatch.delenv("ORIO_API_KEY", raising=False)
        monkeypatch.setattr("sys.argv", ["healthcheck.py"])
        assert hc.main() == 2
