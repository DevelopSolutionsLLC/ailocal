#!/usr/bin/env python3
"""Patched Ollama: pinned build, ownership and failure handling.

Fixtures only: build steps, downloads and signature checks are mocked; nothing compiles or infers.
"""
import io, json, os, re, shutil  # noqa: E401
import tarfile, tempfile, tomllib, unittest  # noqa: E401
from pathlib import Path
from unittest.mock import patch

from ailocal import ollama_patch as candidate
from ailocal import policy

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
        self.runtimes, self.stores = self.root / "runtimes", []
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
            return "fixture go version"
        if args[0] == "go":
            output = Path(args[args.index("-o") + 1])
            output.write_text("fixture executable")
            output.chmod(0o755)
        if args[0].endswith(".test"):  # a vendor regression: record and exercise its model store
            store = Path(kwargs["env"]["OLLAMA_MODELS"])
            self.stores.append((store, kwargs["env"]["OLLAMA_HOST"]))
            store.mkdir(parents=True, exist_ok=True)
            (store / "regression-model").write_text("vendor fixture")
        return next((out for name, out in REGRESSIONS.items() if args[0].endswith(name)), "")

    def replacing(self, match, output):
        def runner(args, deadline, **kw):
            if not args[0].endswith(match):
                return self.run_step(args, deadline, **kw)
            if isinstance(output, Exception):
                raise output
            return output
        return runner

    def build(self, runner=None):
        with (patch.object(candidate, "_download"), patch.object(candidate, "_extract", return_value=self.root),
              patch.object(candidate, "_run", side_effect=runner or self.run_step)):
            return candidate.prepare()

    @staticmethod
    def receipt(directory, **changes):
        data = {**json.loads((directory / "receipt.json").read_text()), **changes}
        if changes:
            (directory / "receipt.json").write_text(json.dumps(data))
        return data

    def test_build_ignores_checksum_exemptions(self):
        seen = []

        def runner(args, deadline, **kw):
            if args[:2] == ["go", "build"]:
                seen.append(kw["env"])
            return self.run_step(args, deadline, **kw)
        hostile = {"GONOSUMDB": "*", "GOPRIVATE": "*", "GOINSECURE": "*", "GOSUMDB": "off",
                   "GONOSUMCHECK": "1", "GOENV": "/tmp/hostile-go.env"}
        with patch.dict(os.environ, hostile):
            self.build(runner)
        env = seen[0]
        self.assertFalse({"GONOSUMDB", "GOPRIVATE", "GOINSECURE", "GONOSUMCHECK"} & env.keys())
        self.assertEqual((env["GOSUMDB"], env["GOENV"]), ("sum.golang.org", "off"))

    def test_promote_reuse_and_reject_tampering(self):
        operator = self.root / "operator-models"
        operator.mkdir()
        (operator / "existing").write_text("operator data")
        with patch.dict(os.environ, OLLAMA_MODELS=str(operator), OLLAMA_HOST="127.0.0.1:11434"):
            binary = self.build()
        self.assertTrue(candidate.verify(binary))
        # Vendor regressions run against a scratch model store and an unbound host, never the operator's.
        self.assertEqual(sorted(operator.iterdir()), [operator / "existing"])
        self.assertEqual(len(self.stores), 3)
        self.assertTrue(all(s != operator and not s.exists() and h == "127.0.0.1:0" for s, h in self.stores))
        with patch.object(candidate, "_run", side_effect=AssertionError("must reuse")):
            self.assertEqual(candidate.prepare(), binary)
        original = (receipt := binary.parent / "receipt.json").read_text()
        foreign_origin = json.loads(original)
        foreign_origin["native_origin"]["vendor_binary_sha256"] = "different vendor payload"
        for invalid in ("[]", "null", '{"files": []}', json.dumps(foreign_origin)):
            receipt.write_text(invalid)
            self.assertFalse(candidate.verify(binary), invalid[:40])
        receipt.write_text(original)
        native = binary.parent / "mlx_metal_v4/libmlxc.dylib"
        native.unlink()
        native.symlink_to(self.stock.parent / "mlx_metal_v4/libmlxc.dylib")
        self.assertFalse(candidate.verify(binary))
        native.unlink()
        native.write_text("tampered")
        self.assertFalse(candidate.verify(binary))
        with self.assertRaisesRegex(RuntimeError, "integrity"):  # refuses; never overwrites
            candidate.prepare()
        self.assertEqual(native.read_text(), "tampered")

    def test_required_regressions_and_build_must_succeed_before_promotion(self):
        cases = {("gemma4.test", "did not execute"): ("--- SKIP: TestLoadStackedExpertsSwitchGLU\nPASS\n", "PASS\n"),
                 ("stop.test", "MLX stop regression"): ("PASS\n", "--- SKIP: TestStopBuffer\nPASS\n"),
                 ("qwen35-history.test", "Qwen3.5 history regression"): (
                     "PASS\n", QWEN_TWO + "PASS\n", "--- SKIP: TestQwen35HistoryOmitsEmptyLoopThinkBlocks\nPASS\n"),
                 ("go", "compiler failed"): (RuntimeError("compiler failed"),)}
        for (test, error), outputs in cases.items():
            for output in outputs:
                with self.subTest(test=test, output=output), self.assertRaisesRegex(RuntimeError, error):
                    self.build(self.replacing(test, output))
                self.assertEqual(list(self.runtimes.iterdir()), [])
        # An optional vendor parity skip is not a skipped required regression.
        optional = QWEN + "--- SKIP: TestQwen38TokenizerMatchesReference (0.00s)\n"
        self.assertTrue(candidate.verify(self.build(self.replacing("qwen35-history.test", optional))))

    def test_bad_pin_never_downloads(self):
        for name, label in PATCHES.items():
            with (self.subTest(pin=name), patch.dict(self.spec, {f"{name}_sha256": "wrong"}),
                  patch.object(candidate, "_run", side_effect=self.run_step),
                  patch.object(candidate, "_download") as download,
                  self.assertRaisesRegex(RuntimeError, f"{label} checksum")):
                candidate.prepare()
            download.assert_not_called()

    def test_cask_upgrades_reuse_an_intact_base_or_fetch_the_pinned_one(self):
        binary = self.build()
        self.assertEqual(self.receipt(binary.parent)["native_origin"]["kind"], "signed-vendor-cask")
        shutil.copytree(binary.parent, older := binary.parent.with_name("0.40.14-ailocal.18631.2"))
        stock_sha = self.receipt(older, version=older.name)["stock_sha256"]
        shutil.rmtree(binary.parent)
        new = self.build(self.replacing((str(self.stock), "codesign"), AssertionError("must use the cache")))
        origin = self.receipt(new.parent)["native_origin"]
        self.assertTrue(candidate.verify(new))
        self.assertEqual((origin["kind"], origin["vendor_binary_sha256"]), ("validated-owned-runtime", stock_sha))
        shutil.rmtree(new.parent)
        (older / "mlx_metal_v4/libmlxc.dylib").write_text("modified")
        release = Path(shutil.copytree(self.stock.parent, self.root / "release"))
        self.stock.write_bytes(MACHO + b"a newer cask's CLI")
        before = self.stock.read_bytes()
        acquire = lambda spec, work, deadline: Path(shutil.copytree(release, work / "r")) / "ollama"  # noqa: E731
        # A newer cask with a modified cache fetches the pinned signed tarball instead.
        with patch.object(candidate, "_release_native_payload", side_effect=acquire) as fetched:
            binary = self.build()
        fetched.assert_called_once()
        origin = self.receipt(binary.parent)["native_origin"]
        self.assertEqual((origin["kind"], origin["archive_sha256"]),
                         ("signed-release-archive", self.spec["native_archive_sha256"]))
        self.assertTrue(self.stock.read_bytes() == before and older.exists() and candidate.verify(binary))

    def native_tgz(self, name, entries):
        work = self.root / name
        work.mkdir()
        with tarfile.open(work / "native-base.tgz", "w:gz") as bundle:
            for entry, data, link in entries:
                member = tarfile.TarInfo(entry)
                if link:
                    member.type, member.linkname = tarfile.SYMTYPE, link
                    bundle.addfile(member)
                else:
                    member.size = len(data)
                    bundle.addfile(member, io.BytesIO(data))
        return work

    def fetch(self, work, spec=None):
        return candidate._release_native_payload(spec or self.spec, work, candidate.time.monotonic() + 10)

    def test_archives_refuse_unsafe_members_and_unverified_native_bases(self):
        with tarfile.open(self.root / "evil.tar", "w") as bundle:
            member = tarfile.TarInfo("../escaped")
            member.size = 1
            bundle.addfile(member, io.BytesIO(b"x"))
        (self.root / "unpack").mkdir()
        with self.assertRaisesRegex(RuntimeError, "Unsafe"):
            candidate._extract(self.root / "evil.tar", self.root / "unpack")
        self.assertFalse((self.root / "escaped").exists())
        for index, entry in enumerate((("../outside", b"x", None), ("/tmp/outside", b"x", None),
                                       ("link", b"", "../../../../../outside"))):
            work = self.native_tgz(f"tgz-{index}", [entry])
            with (self.subTest(entry=index), patch.object(candidate, "_download"),
                  self.assertRaisesRegex(RuntimeError, "Unsafe")):
                self.fetch(work)
        stock = MACHO + b"stock binary"
        work = self.native_tgz("archive", [("ollama", stock, None), ("libggml.0.dylib", b"", "libggml.0.28.0.dylib"),
                                          ("libggml.0.28.0.dylib", MACHO + b"lib", None)])
        with patch.object(candidate, "_download"):
            fetched = self.fetch(work)
            self.assertEqual(candidate._sha(fetched), self.spec["native_cli_sha256"])
            self.assertTrue((fetched.parent / "libggml.0.dylib").is_symlink())  # internal links survive
        work = self.native_tgz("wrong", [("ollama", MACHO + b"other", None)])
        with patch.object(candidate, "_download"), self.assertRaisesRegex(RuntimeError, "checksum"):
            self.fetch(work)

    def test_every_native_file_must_carry_ollamas_signature(self):
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

    def test_remove_deletes_only_intact_owned_historical_versions(self):
        binary = self.build()
        old = self.runtimes / "historical-version"
        shutil.copytree(binary.parent, old)
        self.receipt(old, version="old-reviewed-version")
        (binary.parent / "ollama").write_text("modified by operator")
        (self.runtimes / "foreign").mkdir()
        (self.runtimes / "foreign/keep").write_text("operator data")
        removed, preserved = candidate.remove()
        self.assertEqual(removed, [str(old)])
        self.assertEqual(set(preserved), {str(binary.parent), str(self.runtimes / "foreign")})
        self.assertTrue(binary.exists() and (self.runtimes / "foreign/keep").exists())


class ShippedManifest(unittest.TestCase):
    def test_security_dependencies_match_the_shipped_go_mod_patch(self):
        """The audit reads [security_dependencies]; the build applies security.patch: they must agree."""
        root = policy.data_root() / "deploy" / "ollama"
        spec = tomllib.loads((root / "patch.toml").read_text(encoding="utf-8"))
        text = (root / spec["security_patch_file"]).read_text(encoding="utf-8")
        added = dict(re.findall(r"(?m)^\+\t(\S+) (v\S+)", text.split("diff --git a/go.sum")[0]))
        # Every raised module is declared at its raised version; the table also
        # lists the stock x/ modules so the audit covers the whole graph.
        self.assertTrue(added and added.items() <= spec["security_dependencies"].items())
        self.assertEqual(candidate._sha(root / spec["security_patch_file"]), spec["security_patch_sha256"])


if __name__ == "__main__":
    unittest.main()
