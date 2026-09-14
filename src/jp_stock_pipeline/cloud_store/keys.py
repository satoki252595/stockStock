"""R2 オブジェクトキーの命名 (docs/CF-CANONICAL-DESIGN.md §3)。

R2 には**オブジェクトバージョニングが存在しない**（API 未実装）。したがって
原本キーは SHA256 を含め、物理的に上書きが起こらない immutable 設計にする。

`doc_id` をキーに必須で含める理由: 現行 `rawstore.raw_filename` の
`{source}_{datatype}_{scope}_{YYYYMMDD}` では文書を一意に指せない。実測で
`edinet_pdf_8306_20240729` に 250 個の別内容原本が存在した。
"""

from __future__ import annotations

import re
from datetime import date

# キーセグメントに使える文字。これ以外は "-" に潰す（R2 のキー上限は 1,024 バイト
# だが、ここでの目的は経路記号やマルチバイトでキーが壊れるのを防ぐこと）。
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")

# 原本の SHA256 はキーを短く保つため先頭 16 桁だけ使う。衝突確率は無視できる
# (16 hex = 64bit) うえ、完全な SHA256 は D1 jss_raw_files.sha256 に残る。
SHA_PREFIX_LEN = 16

DOC_ID_FALLBACK = "_"


def _seg(value: str | None, *, fallback: str = "_") -> str:
    """1 セグメントを安全化する。空・None は fallback。"""
    text = (value or "").strip()
    if not text:
        return fallback
    cleaned = _SAFE.sub("-", text).strip("-")
    return cleaned or fallback


def raw_key(
    *,
    source: str,
    datatype: str,
    scope: str,
    data_date: date,
    sha256: str,
    ext: str,
    doc_id: str | None = None,
) -> str:
    """⑤原本の immutable キー。

    ``raw/{source}/{datatype}/{yyyy}/{yyyy-mm-dd}/{scope}/{doc_id}/{sha16}.{ext}``
    """
    if not sha256:
        raise ValueError("raw_key: sha256 は必須（immutable キーの一意性の根拠）")
    return "/".join((
        "raw",
        _seg(source.lower()),
        _seg(datatype.lower()),
        f"{data_date.year:04d}",
        data_date.isoformat(),
        _seg(scope),
        _seg(doc_id, fallback=DOC_ID_FALLBACK),
        f"{sha256[:SHA_PREFIX_LEN]}.{_seg(ext.lstrip('.').lower())}",
    ))


def derived_key(raw_object_key: str, *, suffix: str) -> str:
    """原本から派生した変換版のキー（原本と同じ木構造の derived/ 側に置く）。

    原本キーと 1:1 で対応させることで、派生だけが孤児になることを防ぐ。
    """
    if not raw_object_key.startswith("raw/"):
        raise ValueError(f"derived_key: 原本キーではない: {raw_object_key!r}")
    base, _, _ext = raw_object_key.rpartition(".")
    return f"derived/{base[len('raw/'):]}.{_seg(suffix.lstrip('.').lower())}"


def supply_key(code: str) -> str:
    """⑧'需給の per-code 時系列（jp-stock-supply バケット）。"""
    return f"supply/{_seg(code)}.json"


def margin_key(data_date: date) -> str:
    """信用残の週次/日次スナップショット（vwap-data バケット・既存互換）。

    既存の ``margin/{YYYY-MM-DD}.json`` と同じ形。既存 10 週
    (2026-06-12〜、うち 06-12〜07-31 は JPX から再取得不能) を壊さない。
    """
    return f"margin/{data_date.isoformat()}.json"
