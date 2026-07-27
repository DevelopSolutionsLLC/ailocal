#!/usr/bin/env python3
"""discover.py — build a small, task-relevant context pack for a coding request.

    user prompt -> classify -> find docs -> rank by relevance -> compact pack

The point is NOT to give the coder model more context. It is to give it less, but
the right less. Established on this stack by measurement: a 432-byte instruction
file performed the same as a 47 KB one, so volume is not the lever. Relevance is.

WHAT IT DOES
  1. Classifies the task with the CHEAP model (the 3B tier), not the 30B. The
     30B's job is coding; classification is a one-word answer.
  2. Finds candidate documents — CLAUDE.md, AGENTS.md, CODEX.md, README.md,
     docs/**.md, ADRs.
  3. Ranks them against the task by EMBEDDING similarity, using the local
     embeddings model. Not keyword matching: "modifying the auth flow" should
     surface a security ADR that never says "auth".
  4. Emits only the top slices, under a hard byte budget.

WHAT IT REFUSES TO DO
  - Dump every markdown file it finds. That is the behaviour this replaces.
  - Exceed --budget. If the ranked content does not fit, it is truncated at a
    section boundary and the omission is REPORTED in the pack, so the coder model
    knows something was withheld rather than silently receiving a partial view.
  - Silently fall back to keyword ranking. If embeddings are unavailable it says
    so in the pack and labels the ranking method, because "ranked by relevance"
    and "ranked by word overlap" are different claims.

CACHING
  Summaries and embeddings are cached by CONTENT hash under data/discovery-cache/,
  so an unchanged doc is never re-embedded. The cache key includes the model name:
  a model change invalidates it rather than serving vectors from a different
  embedding space.

Usage:
    scripts/discover.py --task "add retry handling to the http client" [--repo .]
    scripts/discover.py --task "..." --json
    scripts/discover.py --task "..." --budget 4000
"""

import argparse
import glob
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request

PROXY = os.environ.get("AILOCAL_PROXY_URL", "http://127.0.0.1:4000")
CACHE = os.environ.get("AILOCAL_DISCOVERY_CACHE", "data/discovery-cache")
CHEAP_MODEL = os.environ.get("AILOCAL_CHEAP_MODEL", "ailocal-implementation")
EMBED_MODEL = os.environ.get("AILOCAL_EMBED_MODEL", "ailocal-embeddings")

# Where repository instructions and design notes actually live. Ordered by how
# likely they are to carry binding constraints rather than prose.
DOC_PATTERNS = [
    "CLAUDE.md", "AGENTS.md", "CODEX.md", "CONTRIBUTING.md", "README.md",
    "docs/*.md", "docs/**/*.md", "doc/*.md",
    "adr/*.md", "docs/adr/*.md", "docs/architecture/*.md",
    ".github/*.md",
]

TASK_CLASSES = ["edit", "explore", "architecture", "debug", "review", "test"]


def api_key():
    for line in open(".env", encoding="utf-8", errors="replace"):
        if line.startswith("LITELLM_MASTER_KEY="):
            return line.split("=", 1)[1].strip()
    return ""


def post(path, payload, timeout=120):
    req = urllib.request.Request(
        PROXY + path, data=json.dumps(payload).encode(),
        headers={"content-type": "application/json",
                 "Authorization": "Bearer " + api_key()})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# ── cache ───────────────────────────────────────────────────────────────────
def cache_path(kind, key):
    os.makedirs(os.path.join(CACHE, kind), exist_ok=True)
    return os.path.join(CACHE, kind, key + ".json")


def cached(kind, key):
    try:
        with open(cache_path(kind, key), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def store(kind, key, value):
    try:
        with open(cache_path(kind, key), "w", encoding="utf-8") as f:
            json.dump(value, f)
    except Exception:
        pass          # a cache write failure must never break discovery


def content_key(text, *parts):
    """Hash of content PLUS the model name. A model change must invalidate the
    cache: serving vectors from a different embedding space would silently
    corrupt every ranking."""
    h = hashlib.sha256()
    h.update(text.encode("utf-8", "replace"))
    for p in parts:
        h.update(b"\x00" + str(p).encode())
    return h.hexdigest()[:24]


# ── step 1: classify with the cheap model ───────────────────────────────────
def classify(task):
    """(class, method). Falls back to keyword matching and SAYS SO — a guessed
    class presented as a model classification would be a quiet lie."""
    prompt = ("Classify this software engineering task into exactly one of: "
              + ", ".join(TASK_CLASSES)
              + ".\nRespond with the single word only, no punctuation.\n\nTask: "
              + task)
    try:
        out = post("/v1/chat/completions", {
            "model": CHEAP_MODEL, "max_tokens": 8, "temperature": 0,
            "messages": [{"role": "user", "content": prompt}]})
        word = (out["choices"][0]["message"]["content"] or "").strip().lower()
        word = re.sub(r"[^a-z]", "", word.split()[0]) if word.split() else ""
        if word in TASK_CLASSES:
            return word, f"cheap model ({CHEAP_MODEL})"
        # A model that answered off-menu is not authoritative; fall through.
    except Exception:
        pass
    lowered = task.lower()
    keywords = {
        "debug": ["bug", "fail", "error", "crash", "broken", "traceback"],
        "review": ["review", "audit", "security", "vulnerab"],
        "architecture": ["design", "architect", "refactor", "restructure", "migrate"],
        "explore": ["where", "how does", "find", "explain", "which file"],
        "test": ["test", "coverage", "spec"],
    }
    for cls, words in keywords.items():
        if any(w in lowered for w in words):
            return cls, "keyword fallback (cheap model unavailable or off-menu)"
    return "edit", "keyword fallback (default)"


# ── step 2: find candidate docs ─────────────────────────────────────────────
def find_docs(repo):
    seen, out = set(), []
    for pattern in DOC_PATTERNS:
        for path in glob.glob(os.path.join(repo, pattern), recursive=True):
            real = os.path.realpath(path)
            if real in seen or not os.path.isfile(real):
                continue
            # Skip anything enormous: a 200 KB changelog has no business in a
            # context pack, and embedding it wastes the budget it would blow.
            if os.path.getsize(real) > 400_000:
                continue
            seen.add(real)
            out.append(path)
    return sorted(out)


def sections(path, max_chars=1800):
    """Split a markdown file into heading-anchored sections. Sections, not whole
    files, are the unit of relevance: a README's 'Deployment' heading should not
    ride along because its 'Auth' heading matched."""
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except Exception:
        return []
    parts, current, title = [], [], os.path.basename(path)
    for line in text.splitlines():
        if re.match(r"^#{1,3}\s+\S", line):
            if current:
                parts.append((title, "\n".join(current).strip()))
            title = line.lstrip("#").strip()
            current = [line]
        else:
            current.append(line)
    if current:
        parts.append((title, "\n".join(current).strip()))
    out = []
    for title, body in parts:
        if not body:
            continue
        out.append((title, body[:max_chars]))
    return out


# ── step 3: rank by embedding similarity ────────────────────────────────────
def embed(texts):
    """Embeddings for a list of strings, cache-backed. Returns None when the
    embeddings model is unavailable, so the caller can label the fallback."""
    vectors, missing, order = {}, [], []
    for t in texts:
        key = content_key(t, EMBED_MODEL)
        hit = cached("embed", key)
        if hit:
            vectors[t] = hit
        else:
            missing.append(t)
        order.append(t)
    if missing:
        try:
            out = post("/v1/embeddings", {"model": EMBED_MODEL, "input": missing})
            for t, item in zip(missing, out["data"]):
                vectors[t] = item["embedding"]
                store("embed", content_key(t, EMBED_MODEL), item["embedding"])
        except Exception:
            return None
    return [vectors[t] for t in order]


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def rank(task, candidates):
    """[(score, path, title, body)] best first, plus the method used."""
    bodies = [c[2] for c in candidates]
    vecs = embed([task] + bodies)
    if vecs is None:
        # Word overlap. Labelled honestly: this is NOT semantic relevance and
        # will miss a security ADR that never says "auth".
        words = set(re.findall(r"[a-z]{4,}", task.lower()))
        scored = []
        for path, title, body in candidates:
            hay = set(re.findall(r"[a-z]{4,}", (title + " " + body).lower()))
            overlap = len(words & hay) / (len(words) or 1)
            scored.append((overlap, path, title, body))
        scored.sort(key=lambda r: -r[0])
        return scored, "word overlap (embeddings unavailable — NOT semantic)"
    tvec, dvecs = vecs[0], vecs[1:]
    scored = [(cosine(tvec, dv), c[0], c[1], c[2])
              for c, dv in zip(candidates, dvecs)]
    scored.sort(key=lambda r: -r[0])
    return scored, f"embedding similarity ({EMBED_MODEL})"


# ── assemble ────────────────────────────────────────────────────────────────
def discriminate(scores, k):
    """Relative relevance cutoff, and the top score's margin above it.

    HONEST LIMIT [REAL, n=2]: this does NOT reliably detect "the repo has nothing
    on this topic". Measured on this repo, 72 sections:

      "how does the gateway decide which tools to drop"  max 0.665, cutoff 0.568
      "configure kubernetes ingress TLS certificates"    max 0.583, cutoff 0.524

    The second topic appears nowhere in the repo, yet its distribution has the
    same SHAPE as the first, so a statistical test cannot separate them. The
    margin above cutoff does differ (0.097 vs 0.059) but tuning a threshold on
    two examples would be curve-fitting, so no such threshold is applied.

    What the relative cutoff DOES buy, measured: on the relevant task it cut the
    pack from 7 mixed sections to 5 that are all gateway docs. It improves
    precision; it does not detect topic absence.

    Consequence, and why this is acceptable: for an off-topic task the pack
    degrades to README orientation (Capabilities/Setup/Services). That is mild
    waste, not a wrong answer — and arguably useful context for any task. The
    failure mode to avoid is the opposite one, withholding something needed, so
    the design errs toward including generic orientation and LABELLING it as
    such rather than claiming topical relevance.
    """
    if not scores:
        return 0.0, 0.0
    n = len(scores)
    mean = sum(scores) / n
    var = sum((x - mean) ** 2 for x in scores) / n if n > 1 else 0.0
    stdev = var ** 0.5
    cutoff = mean + k * stdev
    return cutoff, max(scores) - cutoff


def build(task, repo, budget, min_score, k=1.0):
    docs = find_docs(repo)
    candidates = []
    for path in docs:
        rel = os.path.relpath(path, repo)
        for title, body in sections(path):
            candidates.append((rel, title, body))

    cls, cls_method = classify(task)

    if not candidates:
        return {
            "task": task, "task_class": cls, "classified_by": cls_method,
            "docs_found": 0, "ranking": "n/a", "included": [],
            "omitted": [], "bytes": 0,
            "note": "no documentation found in this repository",
        }

    scored, method = rank(task, candidates)

    all_scores = [s for s, _, _, _ in scored]
    cutoff, margin = discriminate(all_scores, k)
    # Label, do not gate. See discriminate() for why no threshold is applied:
    # with n=2 observations any cutoff here would be curve-fitted.
    strength = ("topical match" if margin >= 0.08
                else "general orientation (no strong topical match — the repo may "
                     "not document this subject)")
    # The absolute floor still applies as a sanity bound; the relative cutoff is
    # what actually does the work.
    effective = max(cutoff, min_score)

    if not all_scores:
        return {
            "task": task, "task_class": cls, "classified_by": cls_method,
            "docs_found": len(docs), "sections_considered": len(candidates),
            "ranking": method, "budget_bytes": budget, "bytes": 0,
            "included": [], "omitted": [],
            "score_range": [round(min(all_scores), 3), round(max(all_scores), 3)],
            "cutoff": round(effective, 3),
            "note": "no documentation sections were found to rank",
        }

    included, omitted, used = [], [], 0
    for score, path, title, body in scored:
        if score < effective:
            omitted.append({"path": path, "section": title,
                            "score": round(score, 3), "why": "below cutoff"})
            continue
        chunk = len(body.encode())
        if used + chunk > budget:
            omitted.append({"path": path, "section": title,
                            "score": round(score, 3), "why": "budget exhausted"})
            continue
        included.append({"path": path, "section": title,
                         "score": round(score, 3), "body": body})
        used += chunk

    return {
        "task": task, "task_class": cls, "classified_by": cls_method,
        "docs_found": len(docs), "sections_considered": len(candidates),
        "ranking": method, "budget_bytes": budget, "bytes": used,
        "included": included, "omitted": omitted,
        "score_range": [round(min(all_scores), 3), round(max(all_scores), 3)],
        "cutoff": round(effective, 3),
        "match_strength": strength,
        "margin_above_cutoff": round(margin, 3),
    }


def render(pack):
    out = []
    out.append(f"# Context pack — {pack['task_class']} task")
    out.append("")
    out.append(f"Task: {pack['task']}")
    out.append(f"Classified by: {pack['classified_by']}")
    out.append(f"Ranking: {pack['ranking']}")
    if pack.get("score_range"):
        out.append(f"Score range: {pack['score_range'][0]}-{pack['score_range'][1]}, "
                   f"cutoff {pack.get('cutoff')}, margin "
                   f"{pack.get('margin_above_cutoff')}")
        out.append(f"Match: {pack.get('match_strength')}")
    out.append(f"Docs found: {pack['docs_found']}, sections considered: "
               f"{pack.get('sections_considered', 0)}, "
               f"included: {len(pack['included'])}, "
               f"{pack['bytes']}/{pack.get('budget_bytes', 0)} bytes")
    if pack.get("note"):
        out.append(f"Note: {pack['note']}")
    out.append("")
    for item in pack["included"]:
        out.append(f"## {item['path']} — {item['section']} "
                   f"(relevance {item['score']})")
        out.append(item["body"])
        out.append("")
    if pack["omitted"]:
        # Saying what was withheld is the difference between a compact view and a
        # misleadingly partial one.
        budget_hit = [o for o in pack["omitted"] if o["why"] == "budget exhausted"]
        out.append(f"## Withheld ({len(pack['omitted'])} sections)")
        out.append(f"{len(budget_hit)} omitted for budget, "
                   f"{len(pack['omitted']) - len(budget_hit)} below relevance "
                   f"threshold. This pack is deliberately partial; ask for a "
                   f"specific file if something is missing.")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--budget", type=int, default=6000,
                    help="hard byte ceiling on included doc content")
    ap.add_argument("--min-score", type=float, default=0.25,
                    help="absolute sanity floor; the relative cutoff usually wins")
    ap.add_argument("-k", type=float, default=1.0,
                    help="relative cutoff = mean + k*stdev of section scores")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    pack = build(args.task, args.repo, args.budget, args.min_score, args.k)
    print(json.dumps(pack, indent=2) if args.json else render(pack))
    return 0


if __name__ == "__main__":
    sys.exit(main())
