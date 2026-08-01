"""probe.py — capability discovery. Decides which matrix cells are LEGAL.

Nothing here is inferred from a model name. Every field is read from
/api/show, or established by sending a request and observing what came back.

The two facts this exists to establish, because both have already been wrong
in this repository:

  * ACTUAL accepted context, not the advertised ceiling. A model that reports
    262144 may still refuse or silently clip a 64K prompt.
  * EFFECTIVE reasoning support, not requested. `reasoning_effort` reached the
    proxy and was dropped unmapped for months; the config said thinking was
    controllable and it was not. A mode counts as supported only when changing
    it changes observable reasoning output.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402


def show(ollama, tag):
    try:
        return C.post(f"{ollama}/api/show", {"model": tag}, timeout=120)
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}


def advertised_context(info):
    for k, v in (info.get("model_info") or {}).items():
        if k.endswith(".context_length"):
            return v
    return None


def probe_context(ollama, tag, targets):
    """Send progressively larger prompts and record what the backend ACTUALLY
    accepted, using its own prompt_eval_count. A model that clips instead of
    refusing is caught here rather than producing a fake '64K' result."""
    out = {}
    for t in targets:
        # ~4.1 chars/token for this code-like filler; corrected by measurement.
        n = max(1, int(t / 8))
        body = "\n".join(f"def f{i}(a,b): return a+b+{i}" for i in range(n))
        try:
            r = C.post(f"{ollama}/api/chat", {
                "model": tag, "stream": False,
                "messages": [{"role": "user", "content": body + "\nReply OK."}],
                "options": {"num_ctx": t, "num_predict": 4, "temperature": 0},
            }, timeout=1800)
            got = r.get("prompt_eval_count")
            out[str(t)] = {"accepted": True, "actual_prompt_tokens": got,
                           "prompt_tok_s": C.timings_from_ollama(r)["prompt_tok_s"]}
        except Exception as e:
            out[str(t)] = {"accepted": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
            break  # larger targets cannot succeed if this one failed
    return out


def probe_reasoning(ollama, tag, modes):
    """Supported means OBSERVABLY DIFFERENT, not accepted-without-error.

    Distinguishes: unsupported (request rejected) / ignored (accepted but
    reasoning identical across modes) / supported (reasoning output varies).
    """
    caps = {}
    lengths = {}
    for name, ctl in modes.items():
        try:
            r = C.post(f"{ollama}/api/chat", {
                "model": tag, "stream": False, "think": ctl["think"],
                "messages": [{"role": "user",
                              "content": "What is 17*23? Answer with the number only."}],
                "options": {"num_ctx": 8192, "num_predict": 512, "temperature": 0},
            }, timeout=900)
            th = (r.get("message") or {}).get("thinking") or ""
            lengths[name] = len(th)
            caps[name] = {"accepted": True, "reasoning_chars": len(th)}
        except Exception as e:
            caps[name] = {"accepted": False,
                          "error": f"{type(e).__name__}: {str(e)[:100]}"}
    # A mode is DISTINCT only if no other mode produced the same reasoning
    # length. Checking merely "are all modes identical" is too weak and was:
    # qwen3.5 emits 765/765 for standard and deep, i.e. `deep` is silently
    # ignored, while `off` differs -- so the all-identical test passed it as
    # three working modes. Duplicates are reported pairwise instead.
    seen = {}
    for k, v in caps.items():
        if not v.get("accepted"):
            continue
        n = v["reasoning_chars"]
        if n in seen:
            twin = seen[n]
            v["effective"] = False
            v["note"] = f"reasoning length identical to '{twin}' — control ignored"
            caps[twin].setdefault("note", f"reasoning length identical to '{k}'")
        else:
            seen[n] = k
            # `off` is effective when it actually SILENCES reasoning. gpt-oss:20b
            # emits 111 chars with think=false, so off is NOT effective there.
            v["effective"] = (n == 0) if k == "off" else (n > 0)
    return caps


def main():
    m = C.manifest()
    ollama = m["endpoint"]["ollama"]
    installed = {x["name"] for x in C.get(f"{ollama}/api/tags").get("models", [])}
    report = {"schema_version": C.SCHEMA_VERSION,
              "software": C.software_versions(ollama),
              "machine": C.machine_snapshot(), "models": {}}

    quick = "--quick" in sys.argv
    for entry in m["models"]:
        tag = entry["tag"]
        if tag not in installed:
            report["models"][tag] = {"available": False,
                                     "reason": "not installed (ollama pull required)"}
            print(f"  {tag:<30} MISSING")
            continue
        C.unload_all_except(ollama)
        info = show(ollama, tag)
        det = info.get("details") or {}
        rec = {
            "available": True,
            "digest": next((x["digest"] for x in C.get(f"{ollama}/api/tags")["models"]
                            if x["name"] == tag), None),
            "family": det.get("family"),
            "parameter_size": det.get("parameter_size"),
            "quantization": det.get("quantization_level"),
            "format": det.get("format"),
            "size_bytes": next((x["size"] for x in C.get(f"{ollama}/api/tags")["models"]
                                if x["name"] == tag), None),
            "capabilities": info.get("capabilities"),
            "advertised_context": advertised_context(info),
            "supports_tools": "tools" in (info.get("capabilities") or []),
            "supports_thinking": "thinking" in (info.get("capabilities") or []),
            "currently_loaded": tag in C.loaded_models(ollama),
        }
        if not quick:
            rec["reasoning_modes"] = probe_reasoning(ollama, tag, m["reasoning_modes"])
            rec["context"] = probe_context(ollama, tag, m["context_targets"])
        print(f"  {tag:<30} {det.get('parameter_size','?'):>7} "
              f"{str(det.get('quantization_level')):>8} ctx={rec['advertised_context']} "
              f"tools={rec['supports_tools']} think={rec['supports_thinking']}")
        report["models"][tag] = rec
        C.unload_all_except(ollama)

    os.makedirs(C.RESULTS, exist_ok=True)
    p = os.path.join(C.RESULTS, "capabilities.json")
    with open(p, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n  wrote {p}")


if __name__ == "__main__":
    main()
