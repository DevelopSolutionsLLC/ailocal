#!/usr/bin/env python3
"""Deterministic measurement-driver checks; never load a real model."""
from __future__ import annotations

import contextlib
import io
import json
import signal
import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import patch

import measure_agentic as agentic
import measure_cache as cache
import measure_geometry as geometry
import measure_refusals as refusals
import measure_sampling as sampling


class MeasurementContracts(unittest.TestCase):
    def test_sampling_forwards_explicit_budget_and_prompt(self):
        response = {"message": {"content": "fixture", "thinking": ""}, "done_reason": "stop",
                    "eval_count": 2, "eval_duration": 1000000000, "prompt_eval_count": 4}
        with (patch.object(sampling, "api", return_value=response) as api,
              patch.object(sampling, "log_offset"), patch.object(sampling, "speculation_since")):
            result = sampling.generate("fixture", "task", {}, "medium", 32768, 2048, "short prompt")
        payload = api.call_args.args[1]
        self.assertEqual(payload["options"], {"num_ctx": 32768, "num_predict": 2048})
        self.assertEqual(payload["think"], "medium")
        self.assertEqual(api.call_args.args[0], "/api/chat")
        self.assertEqual(payload["messages"], [{"role": "system", "content": "short prompt"},
                                                {"role": "user", "content": "task"}])
        self.assertEqual(result[4:], ("stop", 4, 2))

    def test_exact_variant_and_actual_runner(self):
        entries = [{"name": "model:mlx", "size": 10, "runner": "llamacpp"},
                   {"name": "model:latest", "size": 20, "runner": "mlx"}]
        with patch.object(geometry, "api", return_value={"models": entries}):
            self.assertEqual(geometry.resident("model"), 20)
            self.assertEqual(geometry.engine_of("model:mlx"), "llama_cpp")
            self.assertEqual(geometry.engine_of("model"), "mlx")
            self.assertIsNone(geometry.resident("model:other"))
            with self.assertRaises(RuntimeError):
                geometry.engine_of("model:other")

    def test_agentic_uses_policy_context_before_load(self):
        spec = {"label": "fixture", "options": {}, "think": False}
        with (patch.object(agentic.P, "effective_role", return_value={"num_ctx": 12345}),
              patch.object(agentic, "load_cold", return_value=100) as load,
              patch.object(agentic, "engine_of", return_value="mlx"),
              patch.object(agentic, "decode", return_value=(None, "stop", None, True)),
              patch.object(agentic, "release_model") as release,
              patch.object(agentic.subprocess, "run"),
              contextlib.redirect_stdout(io.StringIO())):
            agentic.run("fixture", spec, [], 1, True, 1)
        load.assert_called_once_with("fixture", 12345)
        release.assert_called_once_with('fixture')

    def test_unknown_candidate_fails_before_discovery(self):
        with (patch.object(agentic, "versions") as versions,
              contextlib.redirect_stderr(io.StringIO()),
              self.assertRaises(SystemExit) as exit_status):
            agentic.main(["--models", "misspelled:model"])
        self.assertEqual(exit_status.exception.code, 2)
        versions.assert_not_called()

    def test_prompt_listing_is_offline(self):
        output = io.StringIO()
        with patch.object(refusals, "api") as api, contextlib.redirect_stdout(output):
            self.assertEqual(refusals.main(["--list-prompts"]), 0)
        api.assert_not_called()
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(rows), 16)
        self.assertEqual({row["variant"] for row in rows}, {"control", "probe"})

    def test_refusal_pilot_selects_complete_pairs(self):
        output = io.StringIO()
        with patch.object(refusals, "api") as api, contextlib.redirect_stdout(output):
            refusals.main(["--list-prompts", "--cases", "fiction", "language"])
        api.assert_not_called()
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(rows), 4)
        self.assertEqual({row["case"] for row in rows}, {"fiction", "language"})

    def test_bounded_pilot_overrides_without_mutating_candidates(self):
        model = "gemma4:26b-mlx"
        original = dict(agentic.CANDIDATES[model])
        with (patch.object(agentic, "versions", return_value=("fixture", "fixture")),
              patch.object(agentic.P, "effective_role", return_value={"num_ctx": 196608}),
              patch.object(agentic.sys, "stdout"), patch.object(agentic, "run") as run):
            self.assertEqual(agentic.main(["--models", model, "--context", "32768", "--prefill"]), 0)
        args = run.call_args.args
        self.assertEqual(args[1]["num_ctx"], 32768)
        self.assertEqual(args[2], (8192,))
        self.assertEqual(agentic.CANDIDATES[model], original)

    def test_default_agentic_run_is_one_small_pilot(self):
        with (patch.object(agentic, "versions", return_value=("fixture", "fixture")),
              patch.object(agentic.P, "effective_role", return_value={"model": "gemma4:26b-mlx", "num_ctx": 196608}),
              patch.object(agentic.sys, "stdout"), patch.object(agentic, "run") as run):
            self.assertEqual(agentic.main([]), 0)
        self.assertEqual(run.call_count, 1)
        args = run.call_args.args
        self.assertEqual(args[1]["num_ctx"], 32768)
        self.assertEqual(args[2], ())
        self.assertEqual((args[3], args[5]), (3, 3))

    def test_refusal_records_are_ungraded_and_model_released(self):
        requests = []

        def api(path, payload=None, **kwargs):
            requests.append((path, payload))
            if path == "/api/tags":
                return {"models": [{"name": "fixture:latest", "digest": "fixture-digest"}]}
            if path == "/api/version":
                return {"version": "fixture-version"}
            if path == "/api/chat":
                return {"message": {"content": ""}, "done_reason": "length"}
            return {}

        output = io.StringIO()
        with (patch.object(refusals, "api", side_effect=api) as calls,
              patch.object(geometry, "api", calls),
              patch.object(refusals, "loaded_model", return_value={"runner": "mlx"}),
              contextlib.redirect_stdout(output)):
            self.assertEqual(refusals.main(["--models", "fixture:latest", "--repeats", "1"]), 0)
        chats = [payload for path, payload in requests if path == "/api/chat"]
        self.assertEqual(len(chats), 16)
        self.assertTrue(all("tools" not in payload for payload in chats))
        self.assertTrue(all(payload["keep_alive"] == "120s" for payload in chats))
        self.assertEqual(requests[-1], ("/api/generate", {"model": "fixture:latest", "keep_alive": 0}))
        rows = [json.loads(line) for line in output.getvalue().splitlines()][1:]
        self.assertTrue(all(row["truncated"] and row["review"].startswith("unlabeled") for row in rows))
        self.assertTrue(all(row["runner"] == "mlx" and row["digest"] == "fixture-digest" for row in rows))

    def test_failed_inference_still_releases_candidate(self):
        def api(path, payload=None, **kwargs):
            if path == "/api/tags":
                return {"models": [{"name": "fixture:latest", "digest": "fixture"}]}
            if path == "/api/chat":
                raise OSError("fixture transport failure")
            return {}

        with (patch.object(refusals, "api", side_effect=api) as calls,
              patch.object(geometry, "api", calls),
              contextlib.redirect_stdout(io.StringIO()), self.assertRaises(OSError)):
            refusals.main(["--models", "fixture:latest", "--repeats", "1"])
        self.assertEqual(calls.call_args.args, ("/api/generate", {"model": "fixture:latest", "keep_alive": 0}))

    def test_expired_budget_sends_no_more_inference(self):
        def api(path, payload=None, **kwargs):
            if path == '/api/chat':
                time.sleep(1)
            return {"models": [{"name": "fixture:latest", "digest": "fixture"}]} if path == '/api/tags' else {}

        with (patch.object(refusals, "api", side_effect=api) as calls,
              patch.object(geometry, "api", calls),
              patch.object(refusals, "measurement_budget", side_effect=lambda seconds: geometry.measurement_budget(0.02)),
              contextlib.redirect_stdout(io.StringIO()), self.assertRaises(geometry.BudgetExpired)):
            refusals.main(["--models", "fixture:latest", "--time-budget", "1"])
        self.assertEqual(sum(call.args[0] == '/api/chat' for call in calls.call_args_list), 1)
        self.assertEqual(calls.call_args.args, ("/api/generate", {"model": "fixture:latest", "keep_alive": 0}))

    def test_budget_restores_alarm_and_preserves_primary_error(self):
        before = signal.getsignal(signal.SIGALRM)
        with self.assertRaises(geometry.BudgetExpired):
            with geometry.measurement_budget(0.02):
                time.sleep(1)
        self.assertEqual(signal.getsignal(signal.SIGALRM), before)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
        with (patch.object(agentic, '_run', side_effect=OSError('primary failure')),
              patch.object(geometry, 'api', side_effect=OSError('cleanup failure')),
              contextlib.redirect_stderr(io.StringIO()), self.assertRaisesRegex(OSError, 'primary failure')):
            agentic.run('fixture', {}, [], 1, True, 1)

    def test_tool_loop_does_not_swallow_whole_run_deadline(self):
        with (tempfile.TemporaryDirectory() as tmp,
              patch.object(agentic, 'FIXTURE_PATH', str(Path(tmp) / 'fixture.txt')),
              patch.object(agentic, 'api', side_effect=geometry.BudgetExpired('expired')) as api,
              self.assertRaises(geometry.BudgetExpired)):
            agentic.tool_loop('fixture', {}, False, 32768, 3)
        self.assertEqual(api.call_count, 1)

    def test_version_discovery_does_not_swallow_whole_run_deadline(self):
        with (patch.object(geometry, 'api', side_effect=geometry.BudgetExpired('expired')),
              self.assertRaises(geometry.BudgetExpired)):
            geometry.versions()

    def test_sampling_releases_model_on_interrupt(self):
        with (patch.object(sampling, 'measure', side_effect=KeyboardInterrupt),
              patch.object(sampling, 'release_model') as release,
              self.assertRaises(KeyboardInterrupt)):
            sampling.main(['--model', 'gemma4:26b-mlx'])
        release.assert_called_once_with('gemma4:26b-mlx')

    def test_non_thinking_mode_is_explicit(self):
        with (patch.object(sampling, 'measure', return_value=0) as measure,
              patch.object(sampling, 'release_model')):
            self.assertEqual(sampling.main(['--model', 'gemma4:26b-mlx', '--no-think']), 0)
        self.assertIs(measure.call_args.args[2], False)

    # measure_cache: the reuse SIGNAL is the whole correctness story. Ollama's
    # prompt_eval_count is the full prompt (cache included), so trusting it reads a
    # false 0% on a warm turn; the probe must prefer prompt_eval_cached_count
    # (Ollama >= 0.33.3), then the server log's evaluated-token count. These lock
    # that selection and the --tail first-cached parsing without loading a model.
    def test_cache_probe_prefers_cached_count_field(self):
        cold = {"prompt_eval_count": 1000, "prompt_eval_duration": 30e9, "response": "x"}
        warm = {"prompt_eval_count": 1010, "prompt_eval_cached_count": 1000,
                "prompt_eval_duration": 3e9}
        with (patch.object(cache, "generate", side_effect=[cold, warm]),
              patch.object(cache, "evaluated_from_log") as from_log,
              patch.object(cache.subprocess, "run"), patch.object(cache.time, "sleep"),
              contextlib.redirect_stdout(io.StringIO())):
            reuse = cache.probe("fixture", 4096, 1000, log=None)
        from_log.assert_not_called()           # the field wins; the log is not read
        self.assertAlmostEqual(reuse, 1 - 10 / 1010, places=4)

    def test_cache_probe_falls_back_to_server_log(self):
        cold = {"prompt_eval_count": 1000, "prompt_eval_duration": 30e9, "response": "x"}
        warm = {"prompt_eval_count": 1010, "prompt_eval_duration": 3e9}  # no cached field
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "ollama.err.log"     # must EXIST for an offset to be taken
            log.write_text("")
            with (patch.object(cache, "generate", side_effect=[cold, warm]),
                  patch.object(cache, "evaluated_from_log", return_value=200),
                  patch.object(cache.subprocess, "run"), patch.object(cache.time, "sleep"),
                  contextlib.redirect_stdout(io.StringIO())):
                reuse = cache.probe("fixture", 4096, 1000, log=log)
        self.assertAlmostEqual(reuse, 1 - 200 / 1010, places=4)

    def test_tail_reads_first_cached_per_request(self):
        # A cold request ramps `cached n_tokens` 0->500 as it prefills; a warm one
        # inherits 480. First-wins must read the cold request as 0% reused, not the
        # ~100% its last ramp line would imply.
        log = ("cached n_tokens = 0, memory_seq_rm [0, end)\n"
               "cached n_tokens = 500, memory_seq_rm [500, end)\n"
               "prompt eval time = 100.0 ms / 500 tokens (...)\n"
               "cached n_tokens = 480, memory_seq_rm [480, end)\n"
               "cached n_tokens = 500, memory_seq_rm [500, end)\n"
               "prompt eval time = 20.0 ms / 20 tokens (...)\n")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ollama.err.log"
            path.write_text(log)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cache.tail(path, 2)
            self.assertEqual(cache.evaluated_from_log(path, 0), 20)  # newest request
        lines = out.getvalue().splitlines()
        self.assertIn("0.0%", lines[-2])    # cold request: nothing inherited
        self.assertIn("96.0%", lines[-1])   # warm request: 480 of 500 inherited


if __name__ == "__main__":
    unittest.main()
