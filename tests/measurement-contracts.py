#!/usr/bin/env python3
"""Deterministic checks of the tests/measure_*.py drivers; fixtures only, no model loads."""
import contextlib
import io
import json
import signal
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import measure_agentic as agentic
import measure_cache as cache
import measure_geometry as geometry
import measure_refusals as refusals

QUIET = contextlib.redirect_stdout(io.StringIO())


class MeasurementContracts(unittest.TestCase):
    def test_runner_identity_is_the_exact_tag_and_reported_runner(self):
        entries = [{"name": "model:mlx", "size": 10, "runner": "llamacpp"},
                   {"name": "model:latest", "size": 20, "runner": "mlx"}]
        with patch.object(geometry, "api", return_value={"models": entries}):
            self.assertEqual(geometry.resident("model"), 20)
            self.assertEqual(geometry.engine_of("model:mlx"), "llama_cpp")  # the tag name does not decide
            self.assertEqual(geometry.engine_of("model"), "mlx")
            self.assertRaises(RuntimeError, geometry.engine_of, "model:other")

    def test_agentic_loads_at_policy_context_and_releases(self):
        with (patch.object(agentic.P, "effective_role", return_value={"num_ctx": 12345}),
              patch.object(agentic, "load_cold", return_value=100) as load,
              patch.object(agentic, "engine_of", return_value="mlx"),
              patch.object(agentic, "decode", return_value=(None, "stop", None, True)),
              patch.object(agentic, "release_model") as release, QUIET):
            agentic.run("fixture", {"label": "fixture", "options": {}, "think": False}, [], 1, True, 1)
        load.assert_called_once_with("fixture", 12345)
        release.assert_called_once_with("fixture")

    def test_refusal_records_are_benign_ungraded_and_released(self):
        requests = []
        def fake(path, payload=None, **_):
            requests.append((path, payload))
            return {"/api/tags": {"models": [{"name": "fixture:latest", "digest": "fixture-digest"}]},
                    "/api/chat": {"message": {"content": ""}, "done_reason": "length"}}.get(path, {})
        with (patch.object(refusals, "api", side_effect=fake) as calls, patch.object(geometry, "api", calls),
              patch.object(refusals, "loaded_model", return_value={"runner": "mlx"}),
              contextlib.redirect_stdout(io.StringIO()) as out):
            self.assertEqual(refusals.main(["--models", "fixture:latest"]), 0)
        chats = [p for path, p in requests if path == "/api/chat"]
        self.assertTrue(len(chats) == 16 and all("tools" not in p for p in chats))
        self.assertEqual(requests[-1], ("/api/generate", {"model": "fixture:latest", "keep_alive": 0}))
        rows = [json.loads(l) for l in out.getvalue().splitlines()][1:]
        self.assertTrue(all(r["truncated"] and r["review"].startswith("unlabeled") and r["runner"] == "mlx"
                            and r["digest"] == "fixture-digest" for r in rows))

    def test_probes_salt_once_and_cache_continuation_is_raw(self):  # salting every identifier multiplied tokens
        with (patch.object(geometry, "api", return_value={}) as api, patch.object(geometry, "resident"),
              patch.object(geometry.subprocess, "run"), patch.object(geometry.time, "sleep"),
              patch.object(geometry.time, "time_ns", return_value=123456789)):
            geometry.fill("fixture", 180, 4096)
        prompt = api.call_args.args[1]["prompt"]
        self.assertEqual((prompt.count("123456789"), prompt.count(geometry.CHUNK)), (1, 10))
        with patch.object(cache, "api", return_value={}) as api:
            cache.generate("fixture", "prefix and continuation", 4096)
        self.assertEqual((api.call_args.args[1]["raw"], api.call_args.args[1]["think"]), (True, False))

    def test_cache_reuse_counts_only_uncached_tokens(self):  # prompt_eval_count includes cached tokens
        cold = {"prompt_eval_count": 1000, "prompt_eval_duration": 30e9, "response": "x"}
        warm = {"prompt_eval_count": 1010, "prompt_eval_duration": 3e9}
        # --tail: only the first `cached n_tokens` per request is inherited; later ones are snapshots.
        text = ("cached n_tokens = 0\ncached n_tokens = 500\nprompt eval time = 100.0 ms / 500 tokens\n"
                "cached n_tokens = 480\ncached n_tokens = 500\nprompt eval time = 20.0 ms / 20 tokens\n")
        with tempfile.TemporaryDirectory() as tmp:
            (log := Path(tmp) / "ollama.err.log").write_text(text)
            for response, expect in (({**warm, "prompt_eval_cached_count": 1000}, 1 - 10 / 1010),
                                     (warm, 1 - 20 / 1010)):  # the API field first, else the server log
                with (patch.object(cache, "generate", side_effect=[cold, response]),
                      patch.object(cache, "evaluated_from_log", return_value=20),
                      patch.object(cache.subprocess, "run"), patch.object(cache.time, "sleep"), QUIET):
                    self.assertAlmostEqual(cache.probe("fixture", 4096, 1000, log=log), expect, places=4)
            self.assertEqual(cache.evaluated_from_log(log, 0), 20)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                cache.tail(log, 2)
        self.assertEqual([l.split()[-1] for l in out.getvalue().splitlines()[-2:]], ["0.0%", "96.0%"])

    def test_budget_expiry_is_never_swallowed_and_restores_the_alarm(self):
        before = signal.getsignal(signal.SIGALRM)
        with self.assertRaises(geometry.BudgetExpired), geometry.measurement_budget(0.02):
            time.sleep(1)
        self.assertEqual((signal.getsignal(signal.SIGALRM), signal.getitimer(signal.ITIMER_REAL)),
                         (before, (0.0, 0.0)))
        with (patch.object(geometry, "api", side_effect=geometry.BudgetExpired("expired")),
              self.assertRaises(geometry.BudgetExpired)):
            geometry.versions()


if __name__ == "__main__":
    unittest.main()
