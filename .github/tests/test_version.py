import codecs
from contextlib import contextmanager
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".github/scripts"))
import version


@contextmanager
def cwd(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for path in version.SOURCE_PATHS:
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / path, target)
        self.context = cwd(self.root)
        self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        self.current = version.inspect()[0]

    def snapshot(self):
        return {p: (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
                for p in version.SOURCE_PATHS}

    def invalid(self):
        before = self.snapshot()
        with self.assertRaises((ValueError, UnicodeError)):
            version.bump("patch")
        self.assertEqual(self.snapshot(), before)

    def edit(self, path, transform):
        document = version.Text.read(path)
        path.write_bytes(document.bom + transform(document.text).encode(document.encoding))

    def test_patch_minor_major(self):
        major, minor, patch = map(int, self.current.split("."))
        for mode, expected in (("patch", f"{major}.{minor}.{patch+1}"),
                               ("minor", f"{major}.{minor+1}.0"),
                               ("major", f"{major+1}.0.0")):
            with self.subTest(mode=mode):
                original = self.snapshot()
                self.assertEqual(version.bump(mode), expected)
                self.assertEqual(version.inspect()[0], expected)
                for path, (raw, _mode) in original.items():
                    path.write_bytes(raw)

    def test_preserves_every_unrelated_byte_and_mode(self):
        for path in version.SOURCE_PATHS:
            doc = version.Text.read(path)
            lines = doc.text.splitlines()
            endings = ("\r\n", "\n", "\r")
            text = "".join(line + endings[i % 3] for i, line in enumerate(lines))
            path.write_bytes(codecs.BOM_UTF16_LE + text.encode("utf-16-le"))
            path.chmod(0o640)
        current, edits = version.inspect()
        expected = {}
        parts = list(map(int, current.split(".")))
        parts[2] += 1
        new = ".".join(map(str, parts))
        for doc, spans in edits:
            expected[doc.path] = doc.replaced([(a, b, new) for a, b in spans])
        version.bump("patch")
        for path in version.SOURCE_PATHS:
            self.assertEqual(path.read_bytes(), expected[path])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_utf8_bom_and_utf16_be(self):
        for bom, encoding in ((codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_BE, "utf-16-be")):
            with self.subTest(encoding=encoding):
                for path in version.SOURCE_PATHS:
                    doc = version.Text.read(path)
                    path.write_bytes(bom + doc.text.encode(encoding))
                version.bump("patch")
                for path in version.SOURCE_PATHS:
                    self.assertTrue(path.read_bytes().startswith(bom))

    def test_required_short_missing_alone(self):
        self.edit(version.PLIST, lambda s: s.replace(
            "</dict>", f"<key>CFBundleVersion</key><string>{self.current}</string></dict>"))
        self.edit(version.PLIST, lambda s: re.sub(
            r"\s*<key>CFBundleShortVersionString</key>\s*<string>[^<]*</string>", "", s))
        self.invalid()

    def test_both_plist_version_keys_missing(self):
        self.edit(version.PLIST, lambda s: re.sub(
            r"\s*<key>CFBundle(?:ShortVersionString|Version)</key>\s*<string>[^<]*</string>", "", s))
        self.invalid()

    def test_only_build_optional(self):
        self.assertNotIn(version.BUILD, version.xml_fields(version.Text.read(version.PLIST)))
        version.bump("patch")
        self.assertNotIn(version.BUILD, version.xml_fields(version.Text.read(version.PLIST)))

    def test_present_build_co_bumps(self):
        self.edit(version.PLIST, lambda s: s.replace("</dict>", f"<key>CFBundleVersion</key><string>{self.current}</string></dict>"))
        new = version.bump("patch")
        self.assertEqual(version.xml_fields(version.Text.read(version.PLIST))[version.BUILD].value, new)

    def test_present_optional_build_drift_is_not_ignored(self):
        for value in ("99.98.97", "01.2.3", "$(UNKNOWN_VERSION)"):
            with self.subTest(value=value):
                original = version.PLIST.read_bytes()
                self.edit(version.PLIST, lambda s: s.replace(
                    "</dict>", f"<key>CFBundleVersion</key><string>{value}</string></dict>"))
                self.invalid()
                version.PLIST.write_bytes(original)

    def test_plist_references_preserved(self):
        self.edit(version.PLIST, lambda s: s.replace(
            f"<string>{self.current}</string>", "<string>$(MARKETING_VERSION)</string>").replace(
                "</dict>", "<key>CFBundleVersion</key><string>$(CURRENT_PROJECT_VERSION)</string></dict>"))
        before = version.PLIST.read_bytes()
        version.bump("patch")
        self.assertEqual(version.PLIST.read_bytes(), before)

    def test_curly_plist_references_preserved(self):
        self.edit(version.PLIST, lambda s: s.replace(
            f"<string>{self.current}</string>", "<string>${MARKETING_VERSION}</string>").replace(
                "</dict>", "<key>CFBundleVersion</key><string>${CURRENT_PROJECT_VERSION}</string></dict>"))
        before = version.PLIST.read_bytes()
        version.bump("patch")
        self.assertEqual(version.PLIST.read_bytes(), before)

    def test_localization_reference_and_optional_build_preserved(self):
        path = version.LOCALIZED[0]
        self.edit(path, lambda s: s.replace(
            f'{version.SHORT} = "{self.current}";',
            f'{version.SHORT} = "$(MARKETING_VERSION)";\r'
            f'{version.BUILD} = "$(CURRENT_PROJECT_VERSION)";'))
        version.bump("patch")
        text = version.Text.read(path).text
        self.assertIn(f'{version.SHORT} = "$(MARKETING_VERSION)";', text)
        self.assertIn(f'{version.BUILD} = "$(CURRENT_PROJECT_VERSION)";', text)

    def test_missing_source_file_fails_before_any_write(self):
        missing = version.LOCALIZED[0]
        missing.unlink()
        before = {path: path.read_bytes() for path in version.SOURCE_PATHS if path.exists()}
        with self.assertRaises(OSError):
            version.bump("patch")
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_wrong_or_unknown_reference_rejected(self):
        for value in ("$(NO_SUCH_VERSION)", "${NO_SUCH_VERSION}", "$(MARKETING_VERSION)noise"):
            with self.subTest(value=value):
                original = version.PLIST.read_bytes()
                self.edit(version.PLIST, lambda s: s.replace(f"<string>{self.current}</string>", f"<string>{value}</string>"))
                self.invalid()
                version.PLIST.write_bytes(original)

    def test_canonical_rejections(self):
        for value in ("01.2.3", "1.02.3", "1.2.03", "1.2", "v1.2.3", "1.2.3-beta", "1.2.3 ", "1.2.3.4"):
            with self.subTest(value=value):
                original = version.PLIST.read_bytes()
                self.edit(version.PLIST, lambda s: s.replace(f"<string>{self.current}</string>", f"<string>{value}</string>"))
                self.invalid()
                version.PLIST.write_bytes(original)

    def test_all_authorities_drift_rejected(self):
        for path in version.SOURCE_PATHS:
            with self.subTest(path=path):
                original = path.read_bytes()
                self.edit(path, lambda s: s.replace(self.current, "99.98.97", 1))
                self.invalid()
                path.write_bytes(original)

    def test_missing_pbx_keys(self):
        for key in version.KEYS:
            original = version.PROJECT.read_bytes()
            self.edit(version.PROJECT, lambda s: re.sub(rf"\s*{key} = [^;]*;", "", s))
            self.invalid()
            version.PROJECT.write_bytes(original)

    def test_missing_single_configuration_assignment_rejected(self):
        self.edit(version.PROJECT, lambda s: s.replace(
            f"CURRENT_PROJECT_VERSION = {self.current};", "", 1))
        self.invalid()

    def test_coordinated_current_marketing_reference_preserved(self):
        self.edit(version.PROJECT, lambda s: s.replace(
            f"CURRENT_PROJECT_VERSION = {self.current};",
            'CURRENT_PROJECT_VERSION = "$(MARKETING_VERSION)";').replace(
                'CURRENT_PROJECT_VERSION = "$(MARKETING_VERSION)";',
                'CURRENT_PROJECT_VERSION = "${MARKETING_VERSION}";', 3))
        new = version.bump("patch")
        self.assertEqual(version.inspect()[0], new)
        self.assertEqual(version.Text.read(version.PROJECT).text.count(
            'CURRENT_PROJECT_VERSION = "$(MARKETING_VERSION)";'), 3)
        self.assertEqual(version.Text.read(version.PROJECT).text.count(
            'CURRENT_PROJECT_VERSION = "${MARKETING_VERSION}";'), 3)

    def test_current_self_reference_rejected(self):
        self.edit(version.PROJECT, lambda s: s.replace(
            f"CURRENT_PROJECT_VERSION = {self.current};",
            'CURRENT_PROJECT_VERSION = "$(CURRENT_PROJECT_VERSION)";'))
        self.invalid()

    def test_pbx_same_line_quoted_and_unquoted_duplicates(self):
        for key in version.KEYS:
            for prefix in ("", "OTHER_SETTING = ignored; "):
                for quoted in (True, False):
                    with self.subTest(key=key, prefix=prefix, quoted=quoted):
                        original = version.PROJECT.read_bytes()
                        second = f'"{key}"' if quoted else key
                        self.edit(version.PROJECT, lambda s: s.replace(
                            f"{key} = {self.current};",
                            f'{prefix}{key} = {self.current}; {second} = "{self.current}";', 1))
                        self.invalid()
                        version.PROJECT.write_bytes(original)

    def test_comments_and_string_noise_preserved(self):
        noise = ('\n/* MARKETING_VERSION = 0.0.0; */\n'
                 '// CURRENT_PROJECT_VERSION = 0.0.0;\n'
                 'noise = "MARKETING_VERSION = 0.0.0; CURRENT_PROJECT_VERSION = 0.0.0;";\n')
        self.edit(version.PROJECT, lambda s: s.replace("objects = {", "objects = {" + noise))
        version.bump("patch")
        self.assertIn(noise, version.Text.read(version.PROJECT).text)

    def test_quoted_real_tokens_scoped_rewrite(self):
        self.edit(version.PROJECT, lambda s: s.replace(
            f"MARKETING_VERSION = {self.current};", f'"MARKETING_VERSION" = "{self.current}";'))
        new = version.bump("patch")
        self.assertIn(f'"MARKETING_VERSION" = "{new}";', version.Text.read(version.PROJECT).text)

    def test_plist_duplicate_rejected(self):
        self.edit(version.PLIST, lambda s: s.replace(
            "</dict>", f"<key>{version.SHORT}</key><string>{self.current}</string></dict>"))
        self.invalid()

    def test_plist_whitespace_key_not_a_required_short_key(self):
        self.edit(version.PLIST, lambda s: s.replace(
            "<key>CFBundleShortVersionString</key>", "<key> CFBundleShortVersionString </key>"))
        self.invalid()

    def test_information_string_escape_before_version_preserves_byte_positions(self):
        path = version.LOCALIZED[0]
        self.edit(path, lambda s: s.replace("maddy version", r'maddy \"quoted\" version'))
        original = path.read_bytes()
        new = version.bump("patch")
        self.assertEqual(path.read_bytes(), original.replace(self.current.encode(), new.encode()))

    def test_localization_duplicate_missing_and_info_drift(self):
        path = version.LOCALIZED[0]
        original = path.read_bytes()
        for transform in (
                lambda s: s + f'\n"{version.SHORT}" = "{self.current}";',
                lambda s: re.sub(r'CFBundleShortVersionString\s*=\s*"[^"]*";', "", s),
                lambda s: re.sub(r'CFBundleGetInfoString\s*=\s*"[^"]*";', "", s),
                lambda s: s.replace(f"version {self.current}", "version 88.77.66")):
            self.edit(path, transform)
            self.invalid()
            path.write_bytes(original)

    def bundle(self):
        bundle = self.root / "built.bundle"
        (bundle / "Contents/Resources/English.lproj").mkdir(parents=True, exist_ok=True)
        shutil.copy2(version.PLIST, bundle / "Contents/Info.plist")
        for path in version.LOCALIZED:
            shutil.copy2(path, bundle / "Contents/Resources" / path)
        return bundle

    def test_built_plist_and_localized_formats(self):
        bundle = self.bundle()
        for binary in (False, True):
            for utf16 in (False, True):
                with self.subTest(binary=binary, utf16=utf16):
                    for source, target in (
                            (version.PLIST, bundle / "Contents/Info.plist"),
                            (version.LOCALIZED[0], bundle / "Contents/Resources" / version.LOCALIZED[0])):
                        values = version.built_dictionary(source)
                        raw = plistlib.dumps(values, fmt=plistlib.FMT_BINARY if binary else plistlib.FMT_XML)
                        if utf16 and not binary:
                            raw = codecs.BOM_UTF16_LE + raw.decode().replace('encoding="UTF-8"', 'encoding="UTF-16"').encode("utf-16-le")
                        target.write_bytes(raw)
                    version.verify_built(bundle)

    def test_built_missing_drift_optional_presence_rejected(self):
        bundle = self.bundle()
        path = bundle / "Contents/Info.plist"
        original = path.read_bytes()
        for values in ({}, {version.SHORT: "99.99.99"},
                       {version.SHORT: self.current, version.BUILD: self.current}):
            path.write_bytes(plistlib.dumps(values))
            with self.assertRaises(ValueError):
                version.verify_built(bundle)
        path.write_bytes(original)
        localized = bundle / "Contents/Resources" / version.LOCALIZED[0]
        localized.unlink()
        with self.assertRaises(OSError):
            version.verify_built(bundle)


if __name__ == "__main__":
    unittest.main()
