"""原本のローカル保存・命名規則・SHA256 (DESIGN.md §5.2, §8.1 step 2)。

- 原本は無加工のバイト列をそのまま保存する
- 命名規則: 原本 `{source}_{datatype}_{scope}_{YYYYMMDD}.{ext}`
  変換版 `{同}_converted.{csv|parquet|txt}`
- SHA256 は冪等性キー (⑤ の重複スキップ・upsert キー)
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path

from .licensing import LicenseTag
from .models import ConvertStatus, RawArtifact, Source, now_jst

_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe(part: str) -> str:
    return _SAFE_RE.sub("-", part.strip()) or "unknown"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def raw_filename(
    source: Source, datatype: str, scope: str, data_date: date | None, ext: str
) -> str:
    """命名規則 (§5.2): {source}_{datatype}_{scope}_{YYYYMMDD}.{ext}"""
    datestr = data_date.strftime("%Y%m%d") if data_date else "nodate"
    ext = ext.lstrip(".")
    return f"{_safe(source.value.lower())}_{_safe(datatype)}_{_safe(scope)}_{datestr}.{ext}"


def converted_filename(raw_name: str, conv_ext: str) -> str:
    """変換版の命名: 原本名(拡張子除く) + `_converted.{ext}`"""
    stem = raw_name.rsplit(".", 1)[0]
    return f"{stem}_converted.{conv_ext.lstrip('.')}"


def save_raw(
    content: bytes,
    *,
    source: Source,
    datatype: str,
    scope: str,
    data_date: date | None,
    url: str,
    ext: str,
    license_tag: LicenseTag,
    base_dir: Path,
) -> RawArtifact:
    """取得した生バイト列を無加工で保存し RawArtifact を返す (§8.1 step 2)。

    - 同名ファイルが既に存在し SHA256 が一致する場合は上書きせずそのまま返す
    - 同名で内容が異なる場合（同日再取得 §5.1）は SHA256 先頭8桁を付与した
      別名で保存し、旧原本の物理コピーを失わない（正本は ⑤ だがローカルも保全）
    """
    base_dir.mkdir(parents=True, exist_ok=True)
    digest = sha256_bytes(content)
    path = base_dir / raw_filename(source, datatype, scope, data_date, ext)
    if path.exists() and sha256_bytes(path.read_bytes()) != digest:
        stem, dot, suffix = path.name.rpartition(".")
        path = base_dir / f"{stem}_{digest[:8]}{dot}{suffix}"
    if not (path.exists() and sha256_bytes(path.read_bytes()) == digest):
        path.write_bytes(content)
    return RawArtifact(
        source=source,
        datatype=datatype,
        scope=scope,
        data_date=data_date,
        fetched_at=now_jst(),
        url=url,
        local_path=path,
        sha256=digest,
        size_bytes=len(content),
        license_tag=license_tag,
        converted_paths=[],
        convert_status=ConvertStatus.NOT_APPLICABLE,
    )
