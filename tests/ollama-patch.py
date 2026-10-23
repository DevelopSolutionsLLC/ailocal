#!/usr/bin/env python3
"""[APPROX] Installer failure/ownership fixtures; no compilation or inference."""
import io
import json
import os
import plistlib
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from ailocal import ollama_patch as candidate


class PatchInstall(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stock = self.root / "Ollama.app/Contents/Resources/ollama"
        self.stock.parent.mkdir(parents=True)
        (self.stock.parent.parent / "Info.plist").write_bytes(
            plistlib.dumps({"CFBundleShortVersionString": "0.40.8"}))
        self.stock.write_text("stock binary")
        (self.stock.parent / "llama-server").write_text("native llama")
        native = self.stock.parent / "mlx_metal_v4"
        native.mkdir()
        (native / "libmlxc.dylib").write_text("native mlx")
        self.patchfile = self.root / "fix.patch"
        self.patchfile.write_text("reviewed patch")
        self.spec = dict(version="0.40.8-ailocal.18631.1", base_version="0.40.8",
                         source_url="https://example.invalid/source", source_sha256="pinned",
                         patch_file=self.patchfile, patch_sha256=candidate._sha(self.patchfile),
                         security_patch_file=self.patchfile,
                         security_patch_sha256=candidate._sha(self.patchfile))
        tools = patch.object(candidate.shutil, "which", side_effect=lambda name: "/usr/bin/" + name)
        tools.start()
        self.addCleanup(tools.stop)
        for name, value in (("ollama_patch_spec", self.spec),
                            ("ollama_runtime_root", self.root / "runtimes"),
                            ("stock_ollama_binary", str(self.stock))):
            mock = patch.object(candidate.policy, name, return_value=value, create=True)
            mock.start()
            self.addCleanup(mock.stop)
        for name, value in (("system", "Darwin"), ("machine", "arm64")):
            mock = patch.object(candidate.platform, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)

    def run_step(self, args, deadline, **kwargs):
        if args[-1] == "--version":
            self.assertEqual(kwargs["env"]["OLLAMA_HOST"], "127.0.0.1:0")
            return "Warning: could not connect\nWarning: client version is 0.40.8\n"
        if args[:2] == ["go", "version"]:
            return "fixture go version"
        if args[0] == "go":
            output = Path(args[args.index("-o") + 1])
            output.write_text("fixture executable")
            output.chmod(0o755)
        if args[0].endswith("gemma4.test"):
            return "--- PASS: TestLoadStackedExpertsSwitchGLU (0.01s)\nPASS\n"
        return ""

    def build(self, runner=None):
        with (patch.object(candidate, "_download"),
              patch.object(candidate, "_extract", return_value=self.root),
              patch.object(candidate, "_run", side_effect=runner or self.run_step)):
            return candidate.prepare()

    def test_promote_reuse_and_detect_native_tampering(self):
        binary = self.build()
        self.assertTrue(candidate.verify(binary))
        with patch.object(candidate, "_run", side_effect=AssertionError("must reuse")):
            self.assertEqual(candidate.prepare(), binary)
        (binary.parent / "mlx_metal_v4/libmlxc.dylib").write_text("tampered")
        self.assertFalse(candidate.verify(binary))
        with self.assertRaisesRegex(RuntimeError, "integrity"):
            candidate.prepare()
        self.assertEqual(self.stock.read_text(), "stock binary")

    def test_skipped_regression_never_promotes(self):
        def skipped(args, deadline, **kwargs):
            if args[0].endswith("gemma4.test"):
                return "--- SKIP: TestLoadStackedExpertsSwitchGLU\nPASS\n"
            return self.run_step(args, deadline, **kwargs)
        with self.assertRaisesRegex(RuntimeError, "did not execute"):
            self.build(skipped)
        self.assertEqual(list((self.root / "runtimes").iterdir()), [])

    def test_symlink_or_invalid_receipt_is_rejected(self):
        binary = self.build()
        receipt = binary.parent / "receipt.json"
        original = receipt.read_text()
        for invalid in ("[]", "null", '{"files": []}'):
            receipt.write_text(invalid)
            self.assertFalse(candidate.verify(binary))
        receipt.write_text(original)
        native = binary.parent / "mlx_metal_v4/libmlxc.dylib"
        native.unlink()
        native.symlink_to(self.stock.parent / "mlx_metal_v4/libmlxc.dylib")
        self.assertFalse(candidate.verify(binary))

    def test_failed_build_and_missing_regression_leave_no_runtime(self):
        for failure in ("build", "missing regression"):
            with self.subTest(failure=failure):
                def failed(args, deadline, **kwargs):
                    if failure == "build" and args[:2] == ["go", "build"]:
                        raise RuntimeError("compiler failed")
                    if args[0].endswith("gemma4.test"):
                        return "PASS\n"
                    return self.run_step(args, deadline, **kwargs)
                with self.assertRaises(RuntimeError):
                    self.build(failed)
                self.assertEqual(list((self.root / "runtimes").iterdir()), [])

    def test_wrong_version_never_stages_or_downloads(self):
        with (patch.object(candidate, "_run", return_value="client version is 0.40.9"),
              self.assertRaisesRegex(RuntimeError, "exactly")):
            candidate.prepare()
        self.assertFalse((self.root / "runtimes").exists())

    def test_remove_only_intact_owned_historical_versions(self):
        binary = self.build()
        root = binary.parent.parent
        old = root / "historical-version"
        candidate.shutil.copytree(binary.parent, old)
        receipt = old / "receipt.json"
        data = json.loads(receipt.read_text())
        data["version"] = "old-reviewed-version"
        receipt.write_text(json.dumps(data))
        (binary.parent / "ollama").write_text("modified by operator")
        foreign = root / "foreign"
        foreign.mkdir()
        (foreign / "keep").write_text("operator data")
        removed, preserved = candidate.remove()
        self.assertEqual(removed, [str(old)])
        self.assertEqual(set(preserved), {str(binary.parent), str(foreign)})
        self.assertTrue(binary.exists())
        self.assertEqual((foreign / "keep").read_text(), "operator data")

    def test_unknown_destination_is_preserved(self):
        target = self.root / "runtimes" / self.spec["version"]
        target.mkdir(parents=True)
        marker = target / "unrelated"
        marker.write_text("preserve")
        with self.assertRaisesRegex(RuntimeError, "integrity"):
            candidate.prepare()
        self.assertEqual(marker.read_text(), "preserve")

    def test_bad_patch_never_downloads(self):
        self.patchfile.write_text("changed")
        with (patch.object(candidate, "_run", side_effect=self.run_step),
              patch.object(candidate, "_download") as download,
              self.assertRaisesRegex(RuntimeError, "patch checksum")):
            candidate.prepare()
        download.assert_not_called()

    def test_archive_escape_is_refused(self):
        archive = self.root / "evil.tar"
        with tarfile.open(archive, "w") as bundle:
            member = tarfile.TarInfo("../escaped")
            member.size = 1
            bundle.addfile(member, io.BytesIO(b"x"))
        target = self.root / "unpack"
        target.mkdir()
        with self.assertRaisesRegex(RuntimeError, "Unsafe"):
            candidate._extract(archive, target)
        self.assertFalse((self.root / "escaped").exists())


if __name__ == "__main__":
    unittest.main()
