#!/usr/bin/env python3
"""Test the two measurement contracts whose failure records a wrong number.

Which runner produced it, and how much prompt the cache served. Fixtures
only; drivers are tests/measure_*.py, so only these earn a test.
"""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import measure_cache as cache
import measure_geometry as geometry


class MeasurementContracts(unittest.TestCase):
    def test_a_result_is_attributed_to_the_runner_ollama_reports_for_the_exact_tag(self):
        entries = [{"name": "model:mlx", "size": 10, "runner": "llamacpp"},
                   {"name": "model:latest", "size": 20, "runner": "mlx"}]
        with patch.object(geometry, "api", return_value={"models": entries}):
            self.assertEqual(geometry.engine_of("model:mlx"), "llama_cpp")  # the tag name does not decide
            self.assertEqual(geometry.engine_of("model"), "mlx")
            self.assertRaises(RuntimeError, geometry.engine_of, "model:other")

    def test_cache_reuse_counts_only_uncached_tokens(self):  # count incl. cache
        cold = {"prompt_eval_count": 1000, "prompt_eval_duration": 30e9, "response": "x"}
        warm = {"prompt_eval_count": 1010, "prompt_eval_duration": 3e9}
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "ollama.err.log"
            log.write_text("prompt eval time = 100.0 ms / 500 tokens\n")
            # API field first; else the newest request logged after cold turn.
            for response, appended, expect in (({**warm, "prompt_eval_cached_count": 1000}, "", 1 - 10 / 1010),
                                               (warm, "prompt eval time = 20.0 ms / 20 tokens\n", 1 - 20 / 1010)):
                turns = iter([cold, response])

                def generate(*_, **__):
                    turn = next(turns)
                    if turn is response:  # warm request logged after cold offset
                        log.write_text(log.read_text() + appended)
                    return turn
                with (patch.object(cache, "generate", side_effect=generate),
                      patch.object(cache.subprocess, "run"), patch.object(cache.time, "sleep"),
                      contextlib.redirect_stdout(io.StringIO())):
                    self.assertAlmostEqual(cache.probe("fixture", 4096, 1000, log=log), expect, places=4)


if __name__ == "__main__":
    from harness import unittest_main  # the script directory is on sys.path
    unittest_main()
