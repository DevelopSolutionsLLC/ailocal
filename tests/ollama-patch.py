#!/usr/bin/env python3
"""Patched Ollama: the pinned build refuses what it cannot verify.

Fixtures only: build, downloads and signature checks are mocked.
"""
import shutil, tempfile, unittest  # noqa: E401
from pathlib import Path
from unittest.mock import patch

from harness import unittest_main  # first: puts this checkout's src/ ahead of any installed ailocal
from ailocal import ollama_patch as candidate

QWEN_TWO = ("--- PASS: TestQwen35HistoryOmitsEmptyLoopThinkBlocks (0.00s)\n"
            "--- PASS: TestQwen35HistoryKeepsReplayedReasoning (0.00s)\n")
QWEN = QWEN_TWO + "--- PASS: TestQwen35HistoryKeepsNonThinkingAndPrefill (0.00s)\nPASS\n"
REGRESSIONS = {"gemma4.test": "--- PASS: TestLoadStackedExpertsSwitchGLU (0.01s)\n"
                              "--- PASS: TestMoERouterMixedPrecisionConfig (0.01s)\nPASS\n",
               "stop.test": "--- PASS: TestStopBuffer (0.01s)\nPASS\n", "qwen35-history.test": QWEN}
PATCHES = {"patch": "Bundled Ollama patch", "security_patch": "security patch", "router_patch": "router patch",
           "stop_patch": "stop patch", "qwen35_patch": "Qwen3.5 history patch"}
MACHO = b"\xcf\xfa\xed\xfe"  # the signature walk selects native files by this header


class PatchInstall(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.runtimes = self.root / "runtimes"
        self.stock = self.root / "Caskroom/ollama-binary/0.40.14/ollama"
        (self.stock.parent / "mlx_metal_v4").mkdir(parents=True)
        for name, text in (("ollama", "stock binary"), ("llama-server", "native llama"),
                           ("mlx_metal_v4/libmlxc.dylib", "native mlx")):
            (self.stock.parent / name).write_bytes(MACHO + text.encode())
        (self.stock.parent / "GO_LICENSE").write_text("license text")
        (fix := self.root / "fix.patch").write_text("reviewed patch")
        self.spec = dict(version="0.40.14-ailocal.18631.3", base_version="0.40.14",
                         source_url="https://example.invalid/source", source_sha256="pinned",
                         native_archive_url="https://example.invalid/ollama-darwin.tgz",
                         native_archive_sha256="pinned-native", native_cli_sha256=candidate._sha(self.stock))
        for name in PATCHES:
            self.spec[f"{name}_file"], self.spec[f"{name}_sha256"] = fix, candidate._sha(fix)
        mocks = [patch.object(candidate.shutil, "which", side_effect=lambda n: "/usr/bin/" + n),
                 patch.object(candidate.platform, "system", return_value="Darwin"),
                 patch.object(candidate.platform, "machine", return_value="arm64")]
        for name, value in (("ollama_patch_spec", self.spec), ("ollama_runtime_root", self.runtimes),
                            ("stock_ollama_binary", str(self.stock))):
            mocks.append(patch.object(candidate.policy, name, return_value=value, create=True))
        self.addCleanup(patch.stopall)
        [mock.start() for mock in mocks]

    def run_step(self, args, deadline, **kwargs):
        if args[-1] == "--version":
            self.assertEqual(kwargs["env"]["OLLAMA_HOST"], "127.0.0.1:0")
            return "Warning: could not connect\nWarning: client version is 0.40.8\n"
        if args[:2] == ["go", "version"]:
            return "go version go1.27.3 darwin/arm64"
        if args[0] == "go":
            output = Path(args[args.index("-o") + 1])
            output.write_text("fixture executable")
            output.chmod(0o755)
        return next((out for name, out in REGRESSIONS.items() if args[0].endswith(name)), "")

    def replacing(self, match, output):
        def runner(args, deadline, **kw):
            if not args[0].endswith(match):
                return self.run_step(args, deadline, **kw)
            if isinstance(output, Exception):
                raise output
            return output
        return runner

    GO_LISTING = [{"version": "go1.27.3", "stable": True}, {"version": "go1.26.10", "stable": True}]

    def build(self, runner=None, go_listing=None):
        listing = self.GO_LISTING if go_listing is None else go_listing
        with (patch.object(candidate, "_download"), patch.object(candidate, "_extract", return_value=self.root),
              patch.object(candidate, "_run", side_effect=runner or self.run_step),
              patch("ailocal.checks.services.http_json", return_value=listing)):
            return candidate.prepare()

    def test_an_unsupported_go_toolchain_is_refused(self):
        """Outdated point release links its stdlib advisories in (#106)."""
        with self.assertRaisesRegex(RuntimeError, "not a current supported release"):
            self.build(go_listing=[{"version": "go1.27.4", "stable": True}, {"version": "go1.26.11", "stable": True}])

    def test_a_tampered_runtime_is_refused_never_reused_or_overwritten(self):
        binary = self.build()
        self.assertTrue(candidate.verify(binary))
        native = binary.parent / "mlx_metal_v4/libmlxc.dylib"
        native.write_text("tampered")
        self.assertFalse(candidate.verify(binary))
        with self.assertRaisesRegex(RuntimeError, "integrity"):
            candidate.prepare()
        self.assertEqual(native.read_text(), "tampered")

    def test_a_required_regression_that_did_not_pass_blocks_promotion(self):
        cases = {("gemma4.test", "did not execute"): ("--- SKIP: TestLoadStackedExpertsSwitchGLU\nPASS\n", "PASS\n"),
                 ("stop.test", "MLX stop regression"): ("PASS\n", "--- SKIP: TestStopBuffer\nPASS\n"),
                 ("qwen35-history.test", "Qwen3.5 history regression"): (
                     "PASS\n", QWEN_TWO + "PASS\n", "--- SKIP: TestQwen35HistoryOmitsEmptyLoopThinkBlocks\nPASS\n")}
        for (test, error), outputs in cases.items():
            for output in outputs:
                with self.subTest(test=test, output=output), self.assertRaisesRegex(RuntimeError, error):
                    self.build(self.replacing(test, output))
                self.assertEqual(list(self.runtimes.iterdir()), [])

    def test_a_patch_checksum_mismatch_refuses_before_any_download(self):
        for name, label in PATCHES.items():
            with (self.subTest(pin=name), patch.dict(self.spec, {f"{name}_sha256": "wrong"}),
                  patch.object(candidate, "_run", side_effect=self.run_step),
                  patch.object(candidate, "_download") as download,
                  self.assertRaisesRegex(RuntimeError, f"{label} checksum")):
                candidate.prepare()
            download.assert_not_called()

    def test_a_native_file_without_ollamas_signature_is_refused(self):
        calls = []

        def runner(args, deadline, **kw):
            if args[0] == "codesign":
                calls.append(args)
            return self.run_step(args, deadline, **kw)
        self.build(runner)
        verified = sorted(Path(a[-1]).name for a in calls)
        self.assertEqual(verified, ["libmlxc.dylib", "llama-server"])  # the license is not Mach-O
        self.assertTrue(all(a[3] == f"-R={candidate.OLLAMA_SIGNATURE}" for a in calls))
        shutil.rmtree(self.runtimes)
        with self.assertRaisesRegex(RuntimeError, "signature rejected"):
            self.build(self.replacing("codesign", RuntimeError("signature rejected")))
        self.assertEqual(list(self.runtimes.iterdir()), [])


if __name__ == "__main__":
    unittest_main()
