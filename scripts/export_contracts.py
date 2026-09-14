"""共有契約ファイルの生成（既定は dry-run = 標準出力へ出すだけ）。

使い方（リポジトリ直下で）:

    uv run python scripts/export_contracts.py              # 標準出力へ出す
    uv run python scripts/export_contracts.py --check      # 現行ファイルとバイト一致しなければ exit 1
    uv run python scripts/export_contracts.py --write      # 現行ファイルへ書き込む

`tests/fixtures/contracts/d1-license-map.json` の機械可読部
（`tags` / `column_license` / `table_license` / `column_groups` /
`writer_claims`）を Python リテラルから生成する（L-36）。
`$` で始まる注記と `$kinds` の説明文は prose なので現行ファイルから引き継ぐ。
直列化は `json.dumps(ensure_ascii=False, indent=2) + "\\n"` に固定し、
kabulab-cf 側と同一バイト列になることを `--check` と CI の
`cross-repo-contract` で担保する。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from jp_stock_pipeline.cloud_store import governance as G  # noqa: E402
from jp_stock_pipeline.cloud_store import schema as S  # noqa: E402
from jp_stock_pipeline.licensing import LicenseTag  # noqa: E402

CONTRACT_PATH = REPO_ROOT / "tests" / "fixtures" / "contracts" / "d1-license-map.json"

# prose は現行ファイルから引き継ぐ（Python リテラルの正本ではない）。
_PRESERVED_KEYS = (
    "$schema_note",
    "$source_of_truth",
    "$undeclared_policy",
    "$tag_semantics",
    "$provenance_note",
    "$kinds",
    "$writer_claim_note",
)


def build_contract(existing: dict) -> dict:
    """Python リテラル + 現行の prose から契約ファイルを組み立てる。"""
    declared_columns: dict[str, dict[str, str]] = {}
    for table, column, tag in S.column_license_rows():
        declared_columns.setdefault(table, {})[column] = tag
    declared_tables = {
        table: {"kind": spec.kind.value, "tag": spec.tag.value if spec.tag else None}
        for table, spec in sorted(G.TABLE_LICENSE.items())
    }
    declared_claims = [
        {
            "dataset": c.dataset,
            "column_group": c.column_group,
            "writer": c.writer,
            "declared": c.declared,
        }
        for c in G.WRITER_CLAIMS
    ]
    # キー順は現行ファイルと同じにする（バイト一致のため）。
    return {
        "$schema_note": existing["$schema_note"],
        "$source_of_truth": existing["$source_of_truth"],
        "$undeclared_policy": existing["$undeclared_policy"],
        "$tag_semantics": existing["$tag_semantics"],
        "$provenance_note": existing["$provenance_note"],
        "$kinds": existing["$kinds"],
        "tags": sorted(t.value for t in LicenseTag),
        "column_license": declared_columns,
        "table_license": declared_tables,
        "$writer_claim_note": existing["$writer_claim_note"],
        "column_groups": dict(G.COLUMN_GROUPS),
        "writer_claims": declared_claims,
    }


def render(contract: dict) -> str:
    return json.dumps(contract, ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="共有契約ファイルを Python リテラルから生成する")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="現行ファイルとバイト一致しなければ exit 1")
    group.add_argument("--write", action="store_true", help="現行ファイルへ書き込む")
    args = parser.parse_args(argv)

    existing = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    missing = [k for k in _PRESERVED_KEYS if k not in existing]
    if missing:
        print(f"現行ファイルに prose が無い: {missing}", file=sys.stderr)
        return 2
    text = render(build_contract(existing))
    if args.check:
        current = CONTRACT_PATH.read_text(encoding="utf-8")
        if current != text:
            print("契約ファイルが Python リテラルと一致しない (--write で再生成)", file=sys.stderr)
            return 1
        print("一致している")
        return 0
    if args.write:
        CONTRACT_PATH.write_text(text, encoding="utf-8")
        print(f"書き込んだ: {CONTRACT_PATH}")
        return 0
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
