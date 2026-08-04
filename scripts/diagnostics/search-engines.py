#!/usr/bin/env python3
"""diag-search-engines — measure SearXNG engine health without changing it.

Engine availability DRIFTS. Comments in deploy/searxng/settings.yml are dated
observations, not standing truth: google cse was recorded at 0% ("Suspended:
too many requests") on 2026-07-28 and measured healthy on 2026-08-03. This
exists so enable/disable decisions are made against a fresh measurement rather
than a stale comment.

DIAGNOSTIC ONLY. It never edits settings.yml, never enables or disables an
engine, and is not part of runtime behaviour. It reports; a human decides.

THE MEASUREMENT MUST NOT CHANGE THE MEASUREMENT. The first version of this tool
drove crossref from healthy to "Suspended: too many requests" during a single
run -- it caused the failure it then reported. Crossref's public pool allows
ONE request per second (measured from x-rate-limit-limit; the widely quoted
50/s is out of date). Defaults here are therefore deliberately gentle:

  * one query class per engine (coding) unless --extended
  * a delay between every probe, >= the tightest known upstream limit
  * strictly sequential -- never concurrent
  * an engine already reporting suspension is REPORTED, not re-probed

Engine list comes from SearXNG's /config endpoint, not from parsing
settings.yml, so this cannot drift from what is actually loaded.

  scripts/diagnostics/search-engines.py                 # safe default sweep
  scripts/diagnostics/search-engines.py --extended      # adds the research query class
  scripts/diagnostics/search-engines.py --engine arxiv  # one engine
  scripts/diagnostics/search-engines.py --json          # machine-readable

Exit 0 whenever the probe ran; a dead engine is a finding, not a script
failure -- which is also why this is NOT in the regression gate: a green gate
must not depend on somebody else's CAPTCHA. Exit 2 if SearXNG is unreachable.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

DEFAULT_BASE = "http://127.0.0.1:8080"

# >= the tightest upstream limit we know of (crossref: 1 req/s). Applied
# between probes so a sweep cannot burst.
DEFAULT_DELAY = 1.5

# An engine is not "healthy" in the abstract -- it is healthy FOR something.
# docker hub answers a CRISPR query with 10 results, all noise; scoring that as
# health makes a noise generator look reliable. `coding` is the default because
# coding is this deployment's default workload.
PROBES = {
    "coding": "python asyncio Semaphore limit concurrency",
    "research": "CRISPR Cas9 off-target effects review",
}

# Failure taxonomy. Order matters: first match wins, most specific first.
CLASSIFIERS = (
    ("CAPTCHA", ("captcha",)),
    ("RATE_LIMITED", ("too many request", "suspended", "429", "rate limit")),
    ("ACCESS_DENIED", ("403", "access denied", "forbidden")),
    ("PARSE_ERROR", ("expecting value", "jsondecode", "parsing error", "parse")),
    ("HTTP_ERROR", ("400", "500", "502", "503", "bad request", "http error")),
    ("TIMEOUT", ("timeout", "timed out")),
    ("NETWORK", ("connection", "transport", "dns", "unreachable")),
)


# Engines reached through a keyed upstream API. Probing these spends real
# quota, and their error strings are the only place a credential could
# plausibly surface, so both are handled explicitly below.
KEYED_ENGINES = {"braveapi"}

# Defence in depth. This tool talks only to SearXNG and never reads .env, so it
# has no key to leak -- but SearXNG error text can echo an upstream URL or
# header, so anything token-shaped is scrubbed before it is printed.
_SECRET_RE = re.compile(r"(?i)\b(?:BS[A-Za-z0-9_-]{10,}|[A-Za-z0-9_-]{28,})\b")


def redact(text: str) -> str:
    return _SECRET_RE.sub("<REDACTED>", text)


def classify(messages: list[str]) -> str | None:
    blob = " ".join(messages).lower()
    for label, needles in CLASSIFIERS:
        if any(n in blob for n in needles):
            return label
    return "ERROR" if messages else None


def fetch(base: str, query: str, timeout: float) -> tuple[dict, float]:
    url = base.rstrip("/") + "/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json"})
    start = time.monotonic()
    with urllib.request.urlopen(url, timeout=timeout) as fh:
        payload = json.load(fh)
    return payload, (time.monotonic() - start) * 1000.0


def loaded_engines(base: str, timeout: float) -> list[dict]:
    with urllib.request.urlopen(base.rstrip("/") + "/config", timeout=timeout) as fh:
        return json.load(fh).get("engines", [])


def probe_engine(base: str, engine: dict, classes: list[str], repeat: int,
                 delay: float, timeout: float, state: dict) -> dict:
    """Probe one engine in isolation via its bang.

    Isolation matters: in a blended search an engine returning nothing is
    invisible, because the other ten fill the page.
    """
    name = engine.get("name", "?")
    shortcut = engine.get("shortcut")
    record: dict = {
        "engine": name,
        "shortcut": shortcut,
        "enabled_by_default": engine.get("enabled") is not False,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "probes": {},
        "suspended": False,
    }

    if not shortcut:
        # Without a bang the engine cannot be addressed alone, so any number
        # would really describe the blended result set.
        record["status"] = "UNPROBEABLE"
        record["detail"] = "no shortcut/bang configured"
        return record

    statuses: list[str] = []
    for kind in classes:
        query = PROBES[kind]
        counts, latencies, errors = [], [], []
        for _ in range(repeat):
            # Never hammer something already known to be suspended: that is
            # what turned a diagnostic into an outage last time.
            if state.get("suspended", {}).get(name):
                record["suspended"] = True
                break
            if state["probed"]:
                time.sleep(delay)
            state["probed"] = True
            try:
                payload, ms = fetch(base, f"!{shortcut} {query}", timeout)
                latencies.append(ms)
                counts.append(len(payload.get("results", [])))
                # SearXNG reports engine failure here, not by HTTP code: a
                # CAPTCHA is a 200 with an empty result list.
                for entry in payload.get("unresponsive_engines") or []:
                    if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                        if entry[0] == name:
                            errors.append(redact(str(entry[1])))
                    elif entry:
                        errors.append(redact(str(entry)))
                # Wikipedia-class engines answer in infoboxes[], which LiteLLM
                # discards -- recorded, never counted as a result.
                # See docs/troubleshooting.md.
                if not counts[-1] and payload.get("infoboxes"):
                    errors.append("infoboxes-only (not model-visible)")
            except urllib.error.URLError as exc:
                errors.append(redact(f"transport: {exc.reason}"))
            except Exception as exc:  # noqa: BLE001 - report, never abort the sweep
                errors.append(redact(f"{type(exc).__name__}: {exc}"))

        kind_class = classify(errors)
        if kind_class in ("RATE_LIMITED", "CAPTCHA"):
            # Remember, so later query classes skip this engine entirely.
            state.setdefault("suspended", {})[name] = kind_class
            record["suspended"] = True

        served = sum(1 for c in counts if c > 0)
        record["probes"][kind] = {
            "served": served,
            "attempts": len(counts) or repeat,
            "results_median": int(statistics.median(counts)) if counts else 0,
            "latency_ms_median": round(statistics.median(latencies), 1) if latencies else None,
            "classification": kind_class,
            "errors": sorted(set(errors)),
        }

        if served == 0 and errors:
            statuses.append(kind_class or "FAILING")
        elif served == 0:
            statuses.append("EMPTY")
        elif served < (len(counts) or repeat):
            statuses.append("INTERMITTENT")
        else:
            statuses.append("OK")

    # Aggregate on WHETHER THE ENGINE WORKS, not on whether it matched every
    # topic. A specialist legitimately returns nothing outside its domain:
    # stackoverflow has no CRISPR content. Scoring "worst class wins" labelled
    # stackoverflow EMPTY while it served coding 2/2 -- which reads as broken
    # and invites someone to disable a working engine.
    failure = next((s for s in statuses
                    if s not in ("OK", "INTERMITTENT", "EMPTY")), None)
    if failure:
        record["status"] = failure
    elif "OK" in statuses:
        record["status"] = "OK"
    elif "INTERMITTENT" in statuses:
        record["status"] = "INTERMITTENT"
    else:
        record["status"] = "EMPTY"
    record["serves"] = [k for k, p in record["probes"].items() if p["served"]]
    return record


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="diag-search-engines", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--engine", help="probe only this engine name")
    ap.add_argument("--include-disabled", action="store_true",
                    help="also probe engines that are disabled by default "
                         "(bang-only). These are disabled BECAUSE they CAPTCHA "
                         "or rate-limit; probing them produces real CAPTCHA "
                         "tracebacks in the SearXNG log. Off by default so a "
                         "routine health check cannot manufacture log noise.")
    ap.add_argument("--extended", action="store_true",
                    help="probe every query class, not just coding "
                         "(MORE UPSTREAM REQUESTS -- may consume rate limit)")
    ap.add_argument("--repeat", type=int, default=1,
                    help="probes per query class (default 1; raising this "
                         "samples CAPTCHA rate but spends rate limit)")
    ap.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                    help=f"seconds between probes (default {DEFAULT_DELAY}; "
                         "crossref allows 1 req/s)")
    ap.add_argument("--timeout", type=float, default=45.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    classes = list(PROBES) if args.extended else ["coding"]

    try:
        engines = loaded_engines(args.base, args.timeout)
    except Exception as exc:  # noqa: BLE001
        print(f"SEARXNG_UNREACHABLE: {args.base}: {exc}", file=sys.stderr)
        return 2

    if args.engine:
        engines = [e for e in engines if e.get("name") == args.engine]
        if not engines:
            print(f"ENGINE_NOT_LOADED: {args.engine!r} is not in {args.base}/config",
                  file=sys.stderr)
            return 2
    elif not args.include_disabled:
        # Bang-only engines are disabled BECAUSE they CAPTCHA or rate-limit.
        # Probing them was the sole source of the DuckDuckGo CAPTCHA tracebacks
        # in the SearXNG log -- a health check must not manufacture the noise it
        # then reports. Naming one with --engine still probes it deliberately.
        skipped = [e.get("name") for e in engines if e.get("enabled") is False]
        engines = [e for e in engines if e.get("enabled") is not False]
        if skipped and not args.json:
            print(f"Skipping {len(skipped)} bang-only engine(s): "
                  f"{', '.join(sorted(filter(None, skipped)))}\n"
                  f"  (--include-disabled to probe them; expect CAPTCHA lines)\n")

    if args.extended and not args.json:
        print("NOTE: --extended issues more upstream requests. Crossref allows "
              "1 req/s;\n      a burst can suspend it for ~180 s.\n")

    state: dict = {"probed": False, "suspended": {}}
    records = [probe_engine(args.base, e, classes, args.repeat, args.delay,
                            args.timeout, state)
               for e in sorted(engines, key=lambda e: e.get("name", ""))]

    if args.json:
        print(json.dumps({
            "base": args.base, "checked_at": datetime.now(timezone.utc)
            .isoformat(timespec="seconds"),
            "classes": classes, "repeat": args.repeat, "delay": args.delay,
            "probes": {k: PROBES[k] for k in classes},
            "engines": records}, indent=2))
        return 0

    print(f"SearXNG engine health — {args.base}")
    print(f"  checked : {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"  classes : {', '.join(classes)}"
          f"   repeat={args.repeat}  delay={args.delay}s (sequential)")
    for k in classes:
        print(f"    {k:9}: {PROBES[k]}")
    print()

    cols = [f"{k[:8]:>15}" for k in classes]
    head = (f"{'engine':18} {'status':14} {'dflt':5} {'susp':5} {'serves':16}"
            + "".join(cols) + "  notes")
    print(head)
    print("-" * len(head))
    for r in records:
        def cell(kind: str) -> str:
            p = r["probes"].get(kind)
            if not p:
                return "skipped"
            lat = p.get("latency_ms_median")
            return (f"{p['served']}/{p['attempts']} n={p['results_median']}"
                    + (f" {lat:.0f}ms" if lat else ""))

        notes = sorted({e for p in r["probes"].values() for e in p.get("errors", [])})
        if r["status"] == "UNPROBEABLE":
            notes = [r.get("detail", "")]
        serves = ",".join(r.get("serves") or []) or "-"
        print(f"{r['engine']:18} {r['status']:14} "
              f"{'yes' if r['enabled_by_default'] else 'no':5} "
              f"{'YES' if r.get('suspended') else '-':5} {serves:16}"
              + "".join(f"{cell(k):>15}" for k in classes)
              + f"  {'; '.join(notes)[:52]}")

    counts: dict[str, int] = {}
    for r in records:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("\n" + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    keyed = sorted({r["engine"] for r in records if r["engine"] in KEYED_ENGINES})
    if keyed:
        print(f"\nQuota note: {', '.join(keyed)} use a keyed upstream API; each probe\n            spends real quota. Brave measured 50 req/s, so a sweep is cheap,\n            but --extended doubles it. Keys are never read by this tool.")
    print("\nEMPTY on a specialist engine usually means 'no content for THIS "
          "query', not broken.\nDiagnostic only — nothing was changed. Record "
          "this output and the date next to any\nenable/disable decision in "
          "deploy/searxng/settings.yml.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
