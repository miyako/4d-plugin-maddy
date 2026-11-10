"""Actual native sources plus the complete unchanged SDK, synthetic engine only."""

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(os.environ.get("OWNERSHIP_SOURCE_ROOT", ROOT)).resolve()
FIXTURES = ("ASCII", "\u03a9", "\U0001f600", "")
CASES = (
    "get-string", "get-bool", "get-number", "get-object", "get-collection",
    "mismatch-string", "mismatch-bool", "mismatch-number", "mismatch-object", "mismatch-collection",
    "failed-string", "failed-bool", "failed-number", "failed-object", "failed-collection",
    "set-narrow", "set-wide", "set-utf16", "set-wchar", "set-picture",
    "stringify", "stringify-command-failure", "stringify-handle-failure",
    "stringify-duplicate-failure", "stringify-nonstring",
    "arrays", "scalar", "plugin", "plugin-options",
)


@unittest.skipUnless(sys.platform == "darwin", "macOS synthetic framework host")
class OwnershipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="maddy-ownership-")
        cls.binary = Path(cls.temp.name) / "ownership"
        api = SOURCE / "4D Plugin API"
        sdk = (api / "4DPluginAPI.c").read_bytes()
        blob = hashlib.sha1(f"blob {len(sdk)}\0".encode() + sdk).hexdigest()
        if blob != "62f745e91d81ef2c3c901c656ea8e175d3b173e8":
            cls.temp.cleanup()
            raise AssertionError("Re-audit the bundled SDK before changing its pinned blob")
        command = [
            "clang++", "-std=c++14", "-x", "objective-c++",
            "-include", "Carbon/Carbon.h", "-I", str(api),
            "-I", str(api / "Classes"), "-I", str(SOURCE / "support"),
            "-I", str(SOURCE / "include"), "-fsanitize=address,undefined",
            "-fno-sanitize=alignment", "-fno-omit-frame-pointer", "-g",
            "-Wno-deprecated-declarations", "-Wno-return-type-c-linkage",
            "-Wno-ignored-attributes", "-Wno-writable-strings",
            str(api / "4DPluginAPI.c"),
            *map(str, sorted((api / "Classes").glob("*.cpp"))),
            str(SOURCE / "support/4DPlugin-JSON.cpp"),
            str(SOURCE / "4DPlugin-maddy.cpp"),
            str(ROOT / ".github/tests/ownership_host.mm"),
            "-framework", "Cocoa", "-framework", "Carbon", "-o", str(cls.binary),
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode:
            cls.temp.cleanup()
            raise AssertionError(result.stdout + result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_native_ownership(self):
        selected = os.environ.get("OWNERSHIP_CASES", "").split(",")
        cases = CASES if selected == [""] else selected
        for case in cases:
            if selected != [""] and case not in selected:
                continue
            for text in FIXTURES:
                for empty in ("allocated", "null"):
                    with self.subTest(case=case, text=text, empty=empty):
                        result = subprocess.run(
                            [str(self.binary), case, text, empty],
                            env={**os.environ, "ASAN_OPTIONS": "detect_leaks=0",
                                 "UBSAN_OPTIONS": "halt_on_error=1"},
                            capture_output=True, text=True,
                        )
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertIn("strings=0 handles=0 refs=0 pictures=0", result.stdout)


if __name__ == "__main__":
    unittest.main()
