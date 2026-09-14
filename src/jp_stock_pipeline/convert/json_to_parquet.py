"""APIレスポンスJSON → CSV/Parquet 変換と、非XBRL変換の共通入口 (DESIGN.md §5.2)。

値不変 (§5.2 / CONTRACTS 不変条件6):
- 行う処理は型変換・縦持ち化（フラット化）・文字コード正規化のみ
- 値の修正・丸め・補完は絶対にしない。欠損は欠損のまま (§3-1)
- dtype は文字列保持を基本とし、文字列からの数値推定はしない
  （数値・bool・ネスト残りは JSON リテラル文字列として無損失に保持する）

本モジュールは併せて、非XBRL変換（json/pdf）の共通入口
`convert_artifact(artifact, kind)` を提供する (§8.1 step 3)。
xls/xlsx 変換は L-12 で削除した（本番経路で未使用。JPX data_j.xlsx を読む
本番実装は kabulab-cf の TypeScript 側）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from ..models import ConvertStatus, RawArtifact
from ..rawstore import converted_filename

logger = logging.getLogger(__name__)

# dict レスポンスでレコード配列を探すキー（この順で推定）
_RECORD_ARRAY_KEYS = ("results", "items", "data")


def _cell_to_str(value: Any) -> str | None:
    """セル値を値不変のまま文字列化する (§5.2)。

    - None / NaN / NA は None のまま（欠損は欠損のまま §3-1。補完しない）
    - str はそのまま（数値推定はしない）
    - 数値・bool・ネスト残りの list/dict は JSON リテラル文字列
      （json.dumps は float の最短ラウンドトリップ表現を使うため丸めが起きない）
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if not isinstance(value, (list, dict)) and pd.isna(value):
        return None
    return json.dumps(value, ensure_ascii=False)


def json_records(data: bytes | dict | list) -> pd.DataFrame:
    """APIレスポンスJSONを正規化テーブル（DataFrame）にする (§5.2 JSON→CSV+Parquet)。

    - bytes は json.loads でそのまま解釈（値の解釈は JSON 仕様のみ。修正しない）
    - list ならレコード配列として直接使用
    - dict なら results / items / data の順でレコード配列キーを推定。
      いずれも無ければレスポンス全体を1行として扱う
    - ネストは pandas.json_normalize で `a.b` 形式の列名にフラット化
    - 全セルは文字列保持（_cell_to_str。数値推定なし・値不変優先）
    """
    if isinstance(data, (bytes, bytearray)):
        data = json.loads(bytes(data))
    if isinstance(data, list):
        records = data
    elif isinstance(data, dict):
        records = None
        for key in _RECORD_ARRAY_KEYS:
            if isinstance(data.get(key), list):
                records = data[key]
                break
        if records is None:
            records = [data]
    else:
        raise TypeError(f"JSONレスポンスとして解釈できない型: {type(data)!r}")

    # スカラー要素の配列はそのまま1列のレコードに包む（構造変換のみ・値不変）
    wrapped = [r if isinstance(r, dict) else {"value": r} for r in records]
    df = pd.json_normalize(wrapped, sep=".")
    return df.map(_cell_to_str).astype("object")


def to_csv_and_parquet(df: pd.DataFrame, artifact: RawArtifact) -> tuple[Path, Path]:
    """DataFrame を原本の隣に CSV + Parquet の2ファイルで書き出す (§5.2)。

    - 命名は rawstore.converted_filename（`{原本stem}_converted.{csv|parquet}`）
    - Parquet は pyarrow で全列 string 型として書く（数値推定なし・値不変優先）

    Returns:
        (csv_path, parquet_path)
    """
    # 念のため文字列保持を保証（json_records 経由なら冪等な恒等変換）
    df = df.map(_cell_to_str).astype("object")
    out_dir = artifact.local_path.parent
    csv_path = out_dir / converted_filename(artifact.filename, "csv")
    parquet_path = out_dir / converted_filename(artifact.filename, "parquet")

    df.to_csv(csv_path, index=False, encoding="utf-8")

    schema = pa.schema([pa.field(str(col), pa.string()) for col in df.columns])
    table = pa.Table.from_pandas(df, schema=schema, preserve_index=False)
    pq.write_table(table, parquet_path)
    return csv_path, parquet_path


def attach_dataframe_parquet(artifact: RawArtifact, df: pd.DataFrame) -> RawArtifact:
    """型付き DataFrame を Parquet 変換版として原本に併置する (§5.2 株価履歴)。

    変換失敗時も原本保存は成立したまま convert_status=失敗 を記録する。
    """
    try:
        out = artifact.local_path.parent / converted_filename(artifact.filename, "parquet")
        df.to_parquet(out, index=False)
        artifact.converted_paths.append(out)
        artifact.convert_status = ConvertStatus.DONE
    except Exception:
        logger.exception("Parquet 変換失敗 (原本保存は成立 §5.2): %s", artifact.filename)
        artifact.convert_status = ConvertStatus.FAILED
    return artifact


def convert_artifact(artifact: RawArtifact, kind: str) -> RawArtifact:
    """非XBRL原本の変換版を生成する共通入口 (§5.2, §8.1 step 3)。

    Args:
        artifact: rawstore.save_raw が返した原本アーティファクト（保存済みであること）
        kind: "json" / "jsonl"（改行区切りJSON = JSON Lines）/ "pdf"

    挙動:
    - 成功: 変換版を原本の隣に生成し artifact.converted_paths へ追加、
      convert_status=完了
    - PDF でテキスト抽出不能（画像のみ等）: 変換版なし・convert_status=対象外
      （§5.2「抽出不能ならスキップし変換版なしと記録」）
    - 変換失敗: 例外を握りつぶして convert_status=失敗 を記録し、原本保存は
      成立したままにする（§5.2「変換失敗時も原本保存は成立させ、状態を記録」）

    未知の kind はプログラミングエラーとして ValueError を送出する（変換失敗とは扱わない）。
    """
    if kind not in ("json", "jsonl", "pdf"):
        raise ValueError(f"未対応の変換種別: {kind!r} (json/jsonl/pdf)")

    new_paths: list[Path] = []
    try:
        raw = artifact.local_path.read_bytes()

        if kind == "json":
            df = json_records(raw)
            csv_path, parquet_path = to_csv_and_parquet(df, artifact)
            new_paths.extend([csv_path, parquet_path])

        elif kind == "jsonl":
            # 1行=1JSON (ページ毎レスポンス等)。各行を正規化して連結
            frames = [
                json_records(line)
                for line in raw.splitlines()
                if line.strip()
            ]
            if not frames:
                raise ValueError("空の JSONL")
            df = pd.concat(frames, ignore_index=True)
            csv_path, parquet_path = to_csv_and_parquet(df, artifact)
            new_paths.extend([csv_path, parquet_path])

        elif kind == "pdf":
            from .pdf_to_text import pdf_to_text

            text = pdf_to_text(raw)
            if text is None:
                # 抽出不能（OCRはしない・真実性優先 §5.2）→ 変換版なしと記録
                artifact.convert_status = ConvertStatus.NOT_APPLICABLE
                return artifact
            txt_path = artifact.local_path.parent / converted_filename(artifact.filename, "txt")
            # newline="" で改行変換を抑止（抽出テキストをバイト単位で不変に保つ §5.2）
            txt_path.write_text(text, encoding="utf-8", newline="")
            new_paths.append(txt_path)

    except Exception:
        # 変換失敗でも原本保存は成立させる (§5.2)。部分生成物は記録しない
        logger.warning(
            "変換失敗 (kind=%s, file=%s) — convert_status=失敗 を記録",
            kind,
            artifact.filename,
            exc_info=True,
        )
        artifact.convert_status = ConvertStatus.FAILED
        return artifact

    artifact.converted_paths.extend(new_paths)
    artifact.convert_status = ConvertStatus.DONE
    return artifact
