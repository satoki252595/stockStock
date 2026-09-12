"""EDINETコードリスト収集 — 銘柄マスタの正本 (DESIGN.md §2.1, §4, P1)。

- 金融庁公開のコードリスト zip（CSV同梱）を取得する。商用利用可
  (公共データ利用規約 PDL1.0 準拠 §2.1) のため license_tag=commercial-ok
- zip 内 CSV は cp932。1行目はメタ行（ダウンロード実行日・件数）、
  2行目がヘッダ、3行目以降がデータ（実レスポンスで確認済み）
- 証券コードは5桁（末尾0、例 "72030"・新方式 "409A0"）→ 4桁に正規化
- 変換版は cp932→UTF-8 の文字コード正規化のみ（値不変 §5.2）
"""

from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from datetime import date

from ..config import Settings
from ..contracts.stock_code import source_code_to_ticker
from ..http import FetchError, fetch
from ..licensing import LicenseTag
from ..models import ConvertStatus, Provenance, RawArtifact, Source, StockMasterRecord, now_jst
from ..rawstore import converted_filename, save_raw

logger = logging.getLogger(__name__)

# EDINET「EDINETタクソノミ及びコードリスト」ページで公開されている EDINETコードリスト
# (zip 内に EdinetcodeDlInfo.csv)。2026-06-10 実取得確認済み
CODELIST_URL = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip"

# CSV ヘッダ名（実ファイル2行目。「ＥＤＩＮＥＴコード」は全角英字であることに注意）
_COL_EDINET_CODE = "ＥＤＩＮＥＴコード"
_COL_LISTED = "上場区分"
_COL_NAME = "提出者名"
_COL_SECTOR = "提出者業種"
_COL_SEC_CODE = "証券コード"

_LISTED_VALUE = "上場"

# メタ行（1行目）の「2026年06月10日現在」形式の日付
_META_DATE_RE = re.compile(r"(\d{4})年(\d{2})月(\d{2})日現在")


def normalize_sec_code(sec_code: str | None) -> str | None:
    """証券コードを4文字基準に正規化する。

    判定と正規化は `contracts/stock_code.py` の `source_code_to_ticker` に委譲する
    （TDnet 側の `normalize_company_code` と同一実装）。

    ここだけ「末尾0の5桁を4桁化し、それ以外は**入力をそのまま返す**」という
    規則を持っていたため、妥当性を一切検証しない素通しになっていた:

    - `"720"` / `"7203.T"` / `"A130"` / `"25935"` / `"１３０ａ"` のいずれも
      加工せずそのまま返しており、呼び出し側は不正なコードを正常値として
      受け取っていた（`edinet.py:209` は scope に、`:246` は code 列に使う）。
    - `"130a"` の大文字化と全角の半角化をしていなかったため、同じ銘柄が
      表記違いで別コードとして入りうる。

    **挙動が変わる点:** 妥当でない入力は入力の丸投げではなく None を返す
    （欠損は欠損 §3-1）。末尾0限定の扱いは旧実装から変えていないが、
    EDINET について末尾0限定の実測根拠は無い（生 secCode を保存している表が
    無く分布が取れない）。`source_code_to_ticker` の docstring を見ること。
    """
    return source_code_to_ticker(sec_code)


def fetch_codelist(settings: Settings) -> RawArtifact:
    """コードリスト zip を取得し、無加工で原本保存する (§8.1 step 1-2)。"""
    resp = fetch(CODELIST_URL)
    # マジックバイト検証 (CONTRACTS): 200 で返るエラーページを正本の原本にしない (§3)
    if not resp.content.startswith(b"PK\x03\x04"):
        raise FetchError(
            f"コードリスト応答が zip でない (エラーページ?): head={resp.content[:16]!r}"
        )
    return save_raw(
        resp.content,
        source=Source.EDINET,
        datatype="codelist",
        scope="ALL",
        data_date=now_jst().date(),  # 取得日（リスト自体が取得日現在のスナップショット）
        url=CODELIST_URL,
        ext="zip",
        license_tag=LicenseTag.COMMERCIAL_OK,
        base_dir=settings.raw_data_dir,
    )


def _read_codelist_csv(zip_bytes: bytes) -> str:
    """zip 内のコードリスト CSV を cp932 でデコードして返す（値不変）。"""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            raise ValueError("コードリスト zip 内に CSV が見つからない")
        # 実物は EdinetcodeDlInfo.csv の1ファイルのみ。複数あれば先頭（名前順）を使う
        return zf.read(sorted(csv_names)[0]).decode("cp932")


def _meta_row_date(meta_row: list[str]) -> date | None:
    """1行目メタ行の「YYYY年MM月DD日現在」からデータ基準日を読む。

    読めない場合は None（推定はしない §3-1）。
    """
    for cell in meta_row:
        m = _META_DATE_RE.search(cell)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def parse_codelist(
    zip_bytes: bytes, *, raw_page_id: str | None = None
) -> list[StockMasterRecord]:
    """コードリスト zip をパースし、証券コードを持つ上場企業のみ返す (§10 P1)。

    - 1行目=メタ行（ダウンロード実行日=データ基準日として使用）、2行目=ヘッダ
    - 証券コードは5桁→4桁化、EDINETコード・提出者名・業種（33業種相当）を設定
    - Provenance: source=EDINET / commercial-ok (§2.1) / data_date=メタ行の取得日
    """
    text = _read_codelist_csv(zip_bytes)
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 2:
        raise ValueError("コードリスト CSV の行数が不足（メタ行+ヘッダ行が必要）")

    data_date = _meta_row_date(rows[0])
    header = rows[1]
    try:
        idx = {
            name: header.index(name)
            for name in (_COL_EDINET_CODE, _COL_LISTED, _COL_NAME, _COL_SECTOR, _COL_SEC_CODE)
        }
    except ValueError as exc:
        raise ValueError(f"コードリスト CSV のヘッダが想定と不一致: {header}") from exc

    fetched_at = now_jst()
    records: list[StockMasterRecord] = []
    for row in rows[2:]:
        if len(row) <= max(idx.values()):
            continue  # 列不足行（末尾の空行等）はスキップ
        if row[idx[_COL_LISTED]].strip() != _LISTED_VALUE:
            continue  # 上場企業のみ
        code = normalize_sec_code(row[idx[_COL_SEC_CODE]])
        if code is None:
            continue  # 証券コードを持つ企業のみ
        records.append(
            StockMasterRecord(
                code=code,
                name=row[idx[_COL_NAME]].strip(),
                edinet_code=row[idx[_COL_EDINET_CODE]].strip() or None,
                sector33=row[idx[_COL_SECTOR]].strip() or None,  # 提出者業種（33業種相当）
                listed=True,
                status="上場",  # コードリストは上場区分=上場 のみ通すため (§ Phase3)
                provenance=Provenance(
                    source=Source.EDINET,
                    license_tag=LicenseTag.COMMERCIAL_OK,
                    data_date=data_date,
                    fetched_at=fetched_at,
                    raw_page_id=raw_page_id,
                ),
            )
        )
    return records


def convert_codelist(artifact: RawArtifact) -> RawArtifact:
    """原本 zip から変換版 CSV（cp932→UTF-8 正規化のみ・値不変 §5.2）を生成する。

    変換失敗時も原本保存は成立済みのまま convert_status=失敗 を記録して続行する
    (§8.1 step 3)。
    """
    try:
        text = _read_codelist_csv(artifact.local_path.read_bytes())
        out_path = artifact.local_path.parent / converted_filename(artifact.filename, "csv")
        # 文字コード正規化のみ。行・値・改行は一切変更しない (§5.2)
        out_path.write_bytes(text.encode("utf-8"))
        artifact.converted_paths.append(out_path)
        artifact.convert_status = ConvertStatus.DONE
    except Exception:
        logger.exception("コードリスト変換失敗 (原本は保存済み): %s", artifact.local_path)
        artifact.convert_status = ConvertStatus.FAILED
    return artifact
