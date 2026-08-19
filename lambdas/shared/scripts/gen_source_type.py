#!/usr/bin/env python3
"""Generate the frontend's TypeScript `Source` type from the Python SpeechResult contract.

The frontend previously hand-mirrored this shape. That is the highest-risk silent-failure
surface in the system: a Python-side rename produces no TS error, no runtime error and no
failing test — citations simply render blank. Generating the type makes drift a red build.

Usage (from repo root):
    python lambdas/shared/scripts/gen_source_type.py            # write the file
    python lambdas/shared/scripts/gen_source_type.py --check    # verify it is up to date (CI)
"""
from __future__ import annotations

import argparse
import dataclasses
import pathlib
import sys
import typing

# Make the package importable when run directly from the repo root.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "python"))

from gov_debates.contracts import SpeechResult  # noqa: E402

OUT_PATH = (
    pathlib.Path(__file__).resolve().parents[3] / "frontend" / "src" / "lib" / "generated" / "source.ts"
)

HEADER = """// GENERATED FILE — DO NOT EDIT BY HAND.
// Generated from lambdas/shared/python/gov_debates/contracts.py (SpeechResult) by
// lambdas/shared/scripts/gen_source_type.py. Run `make gen-types` after changing the contract;
// `make gen-types-check` fails the build if this file is stale.
"""


def _ts_type(field: dataclasses.Field) -> str:
    """Map a dataclass field's annotation to a TypeScript type."""
    ann = field.type
    # Annotations are strings under `from __future__ import annotations`; resolve textually.
    text = ann if isinstance(ann, str) else getattr(ann, "__name__", str(ann))
    optional = "Optional[" in text or "| None" in text

    if "dict[str, Any]" in text or text.startswith("dict"):
        base = "Record<string, unknown>"
    elif "bool" in text:
        base = "boolean"
    elif "int" in text or "float" in text:
        base = "number"
    else:
        base = "string"

    return f"{base} | null" if optional else base


def render() -> str:
    lines = [HEADER, "", "/** One normalized parliamentary contribution (speech or intervention). */",
             "export type Source = {"]
    for field in dataclasses.fields(SpeechResult):
        has_default = (
            field.default is not dataclasses.MISSING
            or field.default_factory is not dataclasses.MISSING  # type: ignore[misc]
        )
        ts = _ts_type(field)
        # Fields with defaults may be absent from a partially-populated payload.
        marker = "?" if has_default else ""
        lines.append(f"  {field.name}{marker}: {ts};")
    lines.append("};")
    lines.append("")
    lines.append("/** The envelope every fetcher Lambda returns. */")
    lines.append("export type ResultsEnvelope = {")
    lines.append("  results: Source[];")
    lines.append("  total: number;")
    lines.append("  jurisdiction: string;")
    lines.append("  truncated: boolean;")
    lines.append("  cursor?: string;")
    lines.append("};")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if the file is stale")
    args = parser.parse_args()

    rendered = render()
    if args.check:
        if not OUT_PATH.exists():
            print(f"MISSING: {OUT_PATH} — run `make gen-types`", file=sys.stderr)
            return 1
        current = OUT_PATH.read_text(encoding="utf-8")
        if current != rendered:
            print(
                f"STALE: {OUT_PATH} does not match the Python contract — run `make gen-types`",
                file=sys.stderr,
            )
            return 1
        print(f"OK: {OUT_PATH.name} is up to date")
        return 0

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(rendered, encoding="utf-8")
    print(f"wrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
