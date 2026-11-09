#!/usr/bin/env python3
"""Validate coordinated source versions and edit only their lexical values."""

import argparse
import codecs
from dataclasses import dataclass
from pathlib import Path
import plistlib
import re
import sys
import xml.etree.ElementTree as ET

PROJECT = Path("maddy.xcodeproj/project.pbxproj")
PLIST = Path("Info.plist")
LOCALIZED = (Path("English.lproj/InfoPlist.strings"),)
SOURCE_PATHS = (PROJECT, PLIST, *LOCALIZED)
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
REF = re.compile(
    r"\$(?:\((MARKETING_VERSION|CURRENT_PROJECT_VERSION)\)"
    r"|\{(MARKETING_VERSION|CURRENT_PROJECT_VERSION)\})\Z")
KEYS = ("MARKETING_VERSION", "CURRENT_PROJECT_VERSION")
SETTING_COUNT = 6
SHORT = "CFBundleShortVersionString"
BUILD = "CFBundleVersion"
INFO = "CFBundleGetInfoString"


def canonical(value):
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise ValueError(f"Not a canonical X.Y.Z version: {value!r}")
    return value


@dataclass
class Text:
    path: Path
    raw: bytes
    text: str
    encoding: str
    bom: bytes

    @classmethod
    def read(cls, path):
        raw = path.read_bytes()
        for bom, encoding in ((codecs.BOM_UTF8, "utf-8"),
                              (codecs.BOM_UTF16_LE, "utf-16-le"),
                              (codecs.BOM_UTF16_BE, "utf-16-be")):
            if raw.startswith(bom):
                return cls(path, raw, raw[len(bom):].decode(encoding), encoding, bom)
        return cls(path, raw, raw.decode("utf-8"), "utf-8", b"")

    def replaced(self, edits):
        text = self.text
        end = len(text)
        for start, stop, value in sorted(edits, reverse=True):
            if not 0 <= start <= stop <= end:
                raise ValueError(f"Overlapping edits in {self.path}")
            text = text[:start] + value + text[stop:]
            end = start
        return self.bom + text.encode(self.encoding)


@dataclass
class Token:
    value: str
    start: int
    end: int
    quoted: bool = False


def tokens(text):
    """Comments and quoted string contents cannot introduce assignments."""
    result = []
    i = 0
    while i < len(text):
        if text[i].isspace():
            i += 1
        elif text.startswith("//", i):
            match = re.search(r"[\r\n]", text[i:])
            i = len(text) if match is None else i + match.start()
        elif text.startswith("/*", i):
            stop = text.find("*/", i + 2)
            if stop < 0:
                raise ValueError("Unterminated comment")
            i = stop + 2
        elif text[i] == '"':
            start = i
            i += 1
            value = ""
            while i < len(text) and text[i] != '"':
                if text[i] == "\\":
                    i += 1
                    if i == len(text):
                        raise ValueError("Unterminated escape")
                value += text[i]
                i += 1
            if i == len(text):
                raise ValueError("Unterminated quoted string")
            result.append(Token(value, start + 1, i, True))
            i += 1
        elif text[i] in "{}=;(),":
            result.append(Token(text[i], i, i + 1))
            i += 1
        else:
            start = i
            while i < len(text) and not text[i].isspace() and text[i] not in '{}=;(),"':
                if text.startswith(("/*", "//"), i):
                    break
                i += 1
            if start == i:
                raise ValueError(f"Unexpected token at {i}")
            result.append(Token(text[start:i], start, i))
    return result


def assignments(text, wanted):
    ts = tokens(text)
    found = {key: [] for key in wanted}
    scopes = [set()]
    line_keys = set()
    for i, token in enumerate(ts):
        if not token.quoted and token.value == "{":
            scopes.append(set())
        elif not token.quoted and token.value == "}":
            if len(scopes) == 1:
                raise ValueError("Unbalanced dictionary")
            scopes.pop()
        elif token.value in wanted and i + 1 < len(ts) and ts[i + 1].value == "=":
            if i + 3 >= len(ts) or ts[i + 3].value != ";":
                raise ValueError(f"Malformed assignment: {token.value}")
            value = ts[i + 2]
            line = len(re.findall(r"\r\n|\r|\n", text[:token.start]))
            same_line = (line, token.value)
            if token.value in scopes[-1] or same_line in line_keys:
                raise ValueError(f"Duplicate assignment: {token.value}")
            scopes[-1].add(token.value)
            line_keys.add(same_line)
            found[token.value].append(value)
    if len(scopes) != 1:
        raise ValueError("Unbalanced dictionary")
    return found


def xml_fields(document):
    root = ET.fromstring(document.text)
    if root.tag != "plist" or len(root) != 1 or root[0].tag != "dict":
        raise ValueError("Expected plist dictionary")
    children = list(root[0])
    if len(children) % 2:
        raise ValueError("Malformed plist dictionary")
    seen = set()
    for key, _value in zip(children[::2], children[1::2]):
        if key.tag != "key" or key.text in seen:
            raise ValueError("Malformed or duplicate plist key")
        seen.add(key.text)
    fields = {}
    for key in (SHORT, BUILD):
        matches = list(re.finditer(
            r"<key>" + key + r"</key>\s*<string>([^<]*)</string>",
            document.text))
        if key in seen and len(matches) != 1:
            raise ValueError(f"{document.path}: invalid {key}")
        if matches:
            match = matches[0]
            fields[key] = Token(match.group(1), *match.span(1))
    if SHORT not in fields:
        raise ValueError(f"{document.path}: required {SHORT} is missing")
    return fields


def resolved(value, values):
    reference = REF.fullmatch(value)
    if reference:
        return values[reference.group(1) or reference.group(2)]
    return canonical(value)


def localized_fields(document):
    fields = assignments(document.text, (SHORT, BUILD, INFO))
    for key in (SHORT, INFO):
        if len(fields[key]) != 1:
            raise ValueError(f"{document.path}: required localized {key} is missing")
    return {key: values[0] for key, values in fields.items() if values}


def inspect():
    project = Text.read(PROJECT)
    settings = assignments(project.text, KEYS)
    values = {}
    for key in KEYS:
        if len(settings[key]) != SETTING_COUNT:
            raise ValueError(f"{PROJECT}: expected {SETTING_COUNT} real {key} assignments")
    marketing = {canonical(token.value) for token in settings[KEYS[0]]}
    if len(marketing) != 1:
        raise ValueError(f"{PROJECT}: MARKETING_VERSION drift")
    current = marketing.pop()
    values[KEYS[0]] = current
    for token in settings[KEYS[1]]:
        reference = REF.fullmatch(token.value)
        if reference and (reference.group(1) or reference.group(2)) == KEYS[0]:
            continue
        if canonical(token.value) != current:
            raise ValueError("Coordinated MARKETING_VERSION/CURRENT_PROJECT_VERSION drift")
    values[KEYS[1]] = current
    edits = [(project, [(t.start, t.end) for key in KEYS for t in settings[key]
                        if not REF.fullmatch(t.value)])]
    source_plist = Text.read(PLIST)
    fields = xml_fields(source_plist)
    plist_edits = []
    for key, token in fields.items():
        if resolved(token.value, values) != current:
            raise ValueError(f"{PLIST}: {key} drift")
        if not REF.fullmatch(token.value):
            plist_edits.append((token.start, token.end))
    edits.append((source_plist, plist_edits))
    for path in LOCALIZED:
        document = Text.read(path)
        fields = localized_fields(document)
        spans = []
        for key in (SHORT, BUILD):
            if key not in fields:
                continue
            token = fields[key]
            if resolved(token.value, values) != current:
                raise ValueError(f"{path}: {key} drift")
            if not REF.fullmatch(token.value):
                spans.append((token.start, token.end))
        if INFO in fields:
            token = fields[INFO]
            raw_value = document.text[token.start:token.end]
            matches = list(re.finditer(r"(?<![0-9.])[0-9]+\.[0-9]+\.[0-9]+(?![0-9.])", raw_value))
            if len(matches) != 1 or canonical(matches[0].group()) != current:
                raise ValueError(f"{path}: localized information version drift")
            match = matches[0]
            spans.append((token.start + match.start(), token.start + match.end()))
        edits.append((document, spans))
    return current, edits


def bump(mode):
    current, edits = inspect()
    parts = list(map(int, current.split(".")))
    index = {"major": 0, "minor": 1, "patch": 2}[mode]
    parts[index] += 1
    parts[index + 1:] = [0] * (2 - index)
    new = ".".join(map(str, parts))
    pending = [(document, document.replaced([(start, end, new) for start, end in spans]))
               for document, spans in edits]
    # Validate every input before writing any file; write_bytes preserves modes.
    for document, raw in pending:
        if document.raw != raw:
            document.path.write_bytes(raw)
    return new


def built_dictionary(path):
    raw = path.read_bytes()
    if raw.startswith(b"bplist"):
        value = plistlib.loads(raw)
    else:
        document = Text.read(path)
        if document.text.lstrip().startswith("<"):
            # Validate duplicate XML keys before plistlib can silently discard them.
            xml_fields(document)
            value = plistlib.loads(raw)
        else:
            return {key: token.value for key, token in localized_fields(document).items()}
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected dictionary")
    return value


def verify_built(bundle):
    current, _edits = inspect()
    values = dict.fromkeys(KEYS, current)
    source = xml_fields(Text.read(PLIST))
    built = built_dictionary(bundle / "Contents/Info.plist")
    for key in (SHORT, BUILD):
        if key not in source:
            if key in built:
                raise ValueError(f"Unexpected built {key}")
        elif built.get(key) != resolved(source[key].value, values):
            raise ValueError(f"Built {key} differs from selected source")
    for path in LOCALIZED:
        expected = localized_fields(Text.read(path))
        destination = bundle / "Contents/Resources" / path
        actual = built_dictionary(destination)
        for key in (SHORT, BUILD, INFO):
            if key not in expected:
                if key in actual:
                    raise ValueError(f"{destination}: unexpected {key}")
                continue
            value = expected[key].value
            if key != INFO:
                value = resolved(value, values)
            if actual.get(key) != value:
                raise ValueError(f"{destination}: {key} differs from selected source")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    read = sub.add_parser("read")
    read.add_argument("--github-output", type=Path)
    edit = sub.add_parser("bump")
    edit.add_argument("mode", choices=("patch", "minor", "major"))
    verify = sub.add_parser("verify-built")
    verify.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "verify-built":
        verify_built(args.bundle)
    else:
        version = bump(args.mode) if args.command == "bump" else inspect()[0]
        print(version)
        if args.command == "read" and args.github_output:
            with args.github_output.open("a", encoding="utf-8") as output:
                output.write(f"version={version}\ntag=v{version}\n")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, ET.ParseError, plistlib.InvalidFileException) as error:
        print(f"Version validation failed: {error}", file=sys.stderr)
        sys.exit(1)
