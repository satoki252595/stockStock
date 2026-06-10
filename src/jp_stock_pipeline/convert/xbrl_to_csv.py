"""EDINET XBRL/CSV → tidy 形式変換 (DESIGN.md §5.2, CONTRACTS.md tidy 列定義)。

tidy 列（CSV/Parquet 共通・この順・全列文字列）:
    code, doc_id, element, context_ref, period_start, period_end,
    instant_date, consolidated, unit, value

不変条件:
- 変換は値を一切変更しない (§5.2)。value は原文の文字列をそのまま保持し、
  数値化は transform 側で行う（丸め・補完・推定の禁止 §3）
- 判別できない項目は空文字のまま（連結/単体の推定はしない）
"""

from __future__ import annotations

import csv
import io
import logging
import re
import zipfile

import pandas as pd
from lxml import etree

from ..models import ConvertStatus, RawArtifact
from ..rawstore import converted_filename

logger = logging.getLogger(__name__)

# CONTRACTS.md「XBRL tidy 形式」の列定義（この順を厳守。B が生成し F が消費する）
TIDY_COLUMNS: list[str] = [
    "code",
    "doc_id",
    "element",
    "context_ref",
    "period_start",
    "period_end",
    "instant_date",
    "consolidated",
    "unit",
    "value",
]

_XBRLI_NS = "http://www.xbrl.org/2003/instance"
_XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

# EDINET の標準コンテキストID（メンバー無し）パターン:
# CurrentYearDuration / Prior1YearInstant / InterimDuration / CurrentYTDDuration 等。
# これに一致する場合のみ「連結既定」(EDINET コンテキスト命名規約) とみなす。
_DEFAULT_CONSOLIDATED_RE = re.compile(r"^(Current|Prior\d*|Interim)[A-Za-z]*(Duration|Instant)$")

# EDINET type=5 CSV のヘッダ名 → tidy 列の対応
_CSV_COL_ELEMENT = "要素ID"
_CSV_COL_CONTEXT = "コンテキストID"
_CSV_COL_CONSOLIDATED = "連結・個別"
_CSV_COL_UNIT = "単位"
_CSV_COL_VALUE = "値"


def consolidated_from_context(context_ref: str) -> str:
    """contextRef 文字列規則のみで連結/単体を判定する。不明は空文字 (§3-1)。

    - NonConsolidatedMember を含む → 「単体」
    - EDINET 標準のメンバー無しコンテキスト名 → 連結既定で「連結」
    - それ以外（提出日時点コンテキスト・独自メンバー等）→ 判別不能なので空文字
    """
    if "NonConsolidatedMember" in context_ref:
        return "単体"
    if "ConsolidatedMember" in context_ref:  # TDnet 短信サマリの明示メンバー
        return "連結"
    if _DEFAULT_CONSOLIDATED_RE.match(context_ref):
        return "連結"
    return ""


# ------------------------------------------------------------------
# XBRL インスタンス (type=1 zip) → tidy
# ------------------------------------------------------------------


def _contexts(root: etree._Element) -> dict[str, tuple[str, str, str]]:
    """context id → (period_start, period_end, instant_date)。ISO 日付文字列。"""
    out: dict[str, tuple[str, str, str]] = {}
    for ctx in root.findall(f"{{{_XBRLI_NS}}}context"):
        ctx_id = ctx.get("id")
        if not ctx_id:
            continue
        period = ctx.find(f"{{{_XBRLI_NS}}}period")
        start = end = instant = ""
        if period is not None:
            start = (period.findtext(f"{{{_XBRLI_NS}}}startDate") or "").strip()
            end = (period.findtext(f"{{{_XBRLI_NS}}}endDate") or "").strip()
            instant = (period.findtext(f"{{{_XBRLI_NS}}}instant") or "").strip()
        out[ctx_id] = (start, end, instant)
    return out


def _measure_local(measure: str) -> str:
    """unit の measure ("iso4217:JPY", "xbrli:shares") をローカル名にする。

    プレフィックスの除去のみで値は変更しない（例: JPY, shares §5.2 の表記）。
    """
    return measure.split(":")[-1].strip()


def _units(root: etree._Element) -> dict[str, str]:
    """unit id → 単位表記。divide は "分子/分母"（例 JPY/shares）。"""
    out: dict[str, str] = {}
    for unit in root.findall(f"{{{_XBRLI_NS}}}unit"):
        unit_id = unit.get("id")
        if not unit_id:
            continue
        divide = unit.find(f"{{{_XBRLI_NS}}}divide")
        if divide is not None:
            num = divide.findtext(
                f"{{{_XBRLI_NS}}}unitNumerator/{{{_XBRLI_NS}}}measure"
            )
            den = divide.findtext(
                f"{{{_XBRLI_NS}}}unitDenominator/{{{_XBRLI_NS}}}measure"
            )
            out[unit_id] = f"{_measure_local(num or '')}/{_measure_local(den or '')}"
        else:
            measure = unit.findtext(f"{{{_XBRLI_NS}}}measure")
            out[unit_id] = _measure_local(measure or "")
    return out


def _element_name(el: etree._Element) -> str:
    """要素名をプレフィックス込み（例 jppfs_cor:NetSales）で返す。"""
    qname = etree.QName(el)
    return f"{el.prefix}:{qname.localname}" if el.prefix else qname.localname


def _facts_from_instance(xml_bytes: bytes, code: str, doc_id: str) -> list[dict[str, str]]:
    """1つの XBRL インスタンス文書から全ファクトを tidy 行に展開する。"""
    root = etree.fromstring(xml_bytes)
    contexts = _contexts(root)
    units = _units(root)

    rows: list[dict[str, str]] = []
    for el in root.iter("*"):
        context_ref = el.get("contextRef")
        if context_ref is None:
            continue  # contextRef を持つ要素のみがファクト（タプルは持たない）
        if etree.QName(el).namespace == _XBRLI_NS:
            continue  # xbrli:context 等のインフラ要素は除外
        start, end, instant = contexts.get(context_ref, ("", "", ""))
        unit_ref = el.get("unitRef")
        if el.get(f"{{{_XSI_NS}}}nil") == "true":
            value = ""  # nil ファクトは欠損のまま (§3-1)
        else:
            value = el.text if el.text is not None else ""  # 原文そのまま (§5.2)
        rows.append(
            {
                "code": code,
                "doc_id": doc_id,
                "element": _element_name(el),
                "context_ref": context_ref,
                "period_start": start,
                "period_end": end,
                "instant_date": instant,
                "consolidated": consolidated_from_context(context_ref),
                "unit": units.get(unit_ref, "") if unit_ref else "",
                "value": value,
            }
        )
    return rows


# ------------------------------------------------------------------
# インラインXBRL (*-ixbrl.htm。TDnet 短信 zip は .xbrl インスタンスを含まず
# iXBRL のみ — 実フィクスチャ tanshin_xbrl_2751_20260610.zip で確認)
# ------------------------------------------------------------------

# inline XBRL 名前空間: TDnet は 1.0 (2008)、EDINET 等は 1.1 (2013) — 両対応
_IX_NAMESPACES = (
    "http://www.xbrl.org/2008/inlineXBRL",
    "http://www.xbrl.org/2013/inlineXBRL",
)


def _decode_ix_value(text: str, sign: str | None, scale: str | None) -> str:
    """ix:nonFraction の表示文字列を XBRL ファクト値へ復号する。

    inline XBRL 仕様の確定的復号（桁区切り除去 → ×10^scale → sign 適用）で
    あり、推定・丸めではない (§3-4)。復号できない場合は空文字（欠損 §3-1）。
    Decimal で計算し浮動小数の表現誤差を持ち込まない。
    """
    from decimal import Decimal, InvalidOperation

    cleaned = text.strip().replace(",", "").replace("，", "")
    if cleaned in ("", "-", "－", "―", "—"):
        return ""
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return ""
    if scale:
        try:
            value = value.scaleb(int(scale))
        except (ValueError, InvalidOperation):
            return ""
    if sign == "-":
        value = -value
    return format(value.normalize(), "f")


def _facts_from_inline(html_bytes: bytes, code: str, doc_id: str) -> list[dict[str, str]]:
    """1つのインラインXBRL文書 (ixbrl.htm) から全ファクトを tidy 行に展開する。

    - context/unit は ix:header/ix:resources 内の xbrli 要素から収集
    - ix:nonFraction は sign/scale を復号した数値文字列、ix:nonNumeric は
      原文テキストをそのまま value にする
    """
    parser = etree.XMLParser(recover=True, huge_tree=True)
    root = etree.fromstring(html_bytes, parser=parser)
    if root is None:
        return []
    contexts = {
        ctx.get("id"): _context_period(ctx)
        for ctx in root.iter(f"{{{_XBRLI_NS}}}context")
        if ctx.get("id")
    }
    units = {}
    for unit in root.iter(f"{{{_XBRLI_NS}}}unit"):
        if unit.get("id"):
            measure = unit.findtext(f"{{{_XBRLI_NS}}}measure")
            units[unit.get("id")] = _measure_local(measure or "")

    rows: list[dict[str, str]] = []
    fact_tags = [
        (f"{{{ns}}}{local}", local == "nonFraction")
        for ns in _IX_NAMESPACES
        for local in ("nonFraction", "nonNumeric")
    ]
    for tag, is_numeric in fact_tags:
        for el in root.iter(tag):
            name = el.get("name")
            context_ref = el.get("contextRef")
            if not name or not context_ref:
                continue
            if el.get(f"{{{_XSI_NS}}}nil") == "true":
                value = ""  # nil ファクトは欠損のまま (§3-1)
            else:
                text = "".join(el.itertext())
                if is_numeric:
                    value = _decode_ix_value(text, el.get("sign"), el.get("scale"))
                else:
                    value = text.strip()  # 原文そのまま (§5.2)
            start, end, instant = contexts.get(context_ref, ("", "", ""))
            unit_ref = el.get("unitRef")
            rows.append(
                {
                    "code": code,
                    "doc_id": doc_id,
                    "element": name,
                    "context_ref": context_ref,
                    "period_start": start,
                    "period_end": end,
                    "instant_date": instant,
                    "consolidated": consolidated_from_context(context_ref),
                    "unit": units.get(unit_ref, "") if unit_ref else "",
                    "value": value,
                }
            )
    return rows


def _context_period(ctx: etree._Element) -> tuple[str, str, str]:
    period = ctx.find(f"{{{_XBRLI_NS}}}period")
    if period is None:
        return ("", "", "")
    return (
        (period.findtext(f"{{{_XBRLI_NS}}}startDate") or "").strip(),
        (period.findtext(f"{{{_XBRLI_NS}}}endDate") or "").strip(),
        (period.findtext(f"{{{_XBRLI_NS}}}instant") or "").strip(),
    )


def xbrl_zip_to_tidy(zip_bytes: bytes, code: str, doc_id: str) -> pd.DataFrame:
    """XBRL zip (EDINET type=1 / TDnet 短信) を tidy にする。

    - zip 内の全 *.xbrl（XBRL/PublicDoc/ 配下等）を名前順にパースし連結する
    - *.xbrl インスタンスが1つも無い場合（TDnet 短信 zip は iXBRL のみ）は
      *-ixbrl.htm をインラインXBRLとしてパースする
    - どちらも無ければ空の DataFrame（列はスキーマどおり）を返す
    """
    rows: list[dict[str, str]] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = sorted(zf.namelist())
        instance_names = [n for n in names if n.lower().endswith(".xbrl")]
        for name in instance_names:
            try:
                rows.extend(_facts_from_instance(zf.read(name), code, doc_id))
            except etree.XMLSyntaxError:
                logger.exception("XBRL インスタンスのパース失敗（スキップ）: %s", name)
        if not instance_names:
            for name in (n for n in names if n.lower().endswith("-ixbrl.htm")):
                try:
                    rows.extend(_facts_from_inline(zf.read(name), code, doc_id))
                except Exception:
                    logger.exception("インラインXBRL のパース失敗（スキップ）: %s", name)
    return _as_tidy_frame(rows)


# ------------------------------------------------------------------
# EDINET CSV (type=5 zip) → tidy
# ------------------------------------------------------------------


def _decode_auto(data: bytes) -> str:
    """EDINET type=5 CSV のデコード。仕様は UTF-16LE(BOM)・タブ区切り。

    実フィクスチャ未取得のため BOM/エンコーディングを自動判別する実装にし、
    実物取得後に検証できるようにしている（値は一切変更しない）。
    """
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16")
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig")
    if b"\x00" in data[:200]:  # BOM 無し UTF-16 の痕跡（NUL バイト）
        return data.decode("utf-16-le")
    for enc in ("utf-8", "cp932"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("EDINET CSV のエンコーディングを判別できない")


def _csv_consolidated(raw: str, context_ref: str) -> str:
    """EDINET CSV「連結・個別」列の表記を tidy の「連結/単体」に対応付ける。

    表記の対応のみで値の解釈はしない。列値が無ければ contextRef 規則で判定。
    """
    raw = raw.strip()
    if raw == "連結":
        return "連結"
    if raw == "個別":
        return "単体"  # tidy スキーマ上の表記（CONTRACTS.md）に対応付け
    return consolidated_from_context(context_ref)


def _rows_from_edinet_csv(text: str, code: str, doc_id: str, name: str) -> list[dict[str, str]]:
    rows_iter = csv.reader(io.StringIO(text), delimiter="\t")
    try:
        header = next(rows_iter)
    except StopIteration:
        return []
    header = [h.strip() for h in header]
    required = (_CSV_COL_ELEMENT, _CSV_COL_CONTEXT, _CSV_COL_VALUE)
    if any(col not in header for col in required):
        raise ValueError(f"EDINET CSV のヘッダが想定と不一致: {name}: {header}")
    idx = {col: header.index(col) for col in header}

    def cell(row: list[str], col: str) -> str:
        i = idx.get(col)
        return row[i] if i is not None and i < len(row) else ""

    rows: list[dict[str, str]] = []
    for row in rows_iter:
        if not any(c.strip() for c in row):
            continue  # 空行
        context_ref = cell(row, _CSV_COL_CONTEXT).strip()
        rows.append(
            {
                "code": code,
                "doc_id": doc_id,
                "element": cell(row, _CSV_COL_ELEMENT).strip(),
                "context_ref": context_ref,
                # type=5 CSV は絶対日付を持たないため期間列は空のまま（補完しない §3-1）
                "period_start": "",
                "period_end": "",
                "instant_date": "",
                "consolidated": _csv_consolidated(cell(row, _CSV_COL_CONSOLIDATED), context_ref),
                "unit": cell(row, _CSV_COL_UNIT).strip(),
                "value": cell(row, _CSV_COL_VALUE),  # 原文そのまま (§5.2)
            }
        )
    return rows


def edinet_csv_zip_to_tidy(zip_bytes: bytes, code: str, doc_id: str) -> pd.DataFrame:
    """EDINET type=5 zip 内の CSV 群を tidy にする。CSV が無ければ空フレーム。"""
    rows: list[dict[str, str]] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in sorted(zf.namelist()):
            if not name.lower().endswith(".csv"):
                continue
            rows.extend(_rows_from_edinet_csv(_decode_auto(zf.read(name)), code, doc_id, name))
    return _as_tidy_frame(rows)


# ------------------------------------------------------------------
# tidy 書き出し
# ------------------------------------------------------------------


def _as_tidy_frame(rows: list[dict[str, str]]) -> pd.DataFrame:
    """tidy 列順・全列文字列の DataFrame を構築する。"""
    df = pd.DataFrame(rows, columns=TIDY_COLUMNS)
    return df.fillna("").astype("string")


def write_tidy(df: pd.DataFrame, artifact: RawArtifact) -> RawArtifact:
    """tidy DataFrame を CSV+Parquet の変換版として書き出し artifact を更新する。

    - ファイル名は rawstore.converted_filename（§5.2 命名規則）
    - 全列文字列・value は原文のまま（値不変）
    - 失敗時は原本保存を成立させたまま convert_status=失敗 を記録 (§8.1 step 3)
    """
    try:
        out = df.reindex(columns=TIDY_COLUMNS).fillna("").astype("string")
        base = artifact.local_path.parent
        csv_path = base / converted_filename(artifact.filename, "csv")
        parquet_path = base / converted_filename(artifact.filename, "parquet")
        out.to_csv(csv_path, index=False, encoding="utf-8")
        out.to_parquet(parquet_path, index=False)
        artifact.converted_paths.extend([csv_path, parquet_path])
        artifact.convert_status = ConvertStatus.DONE
    except Exception:
        logger.exception("tidy 変換版の書き出し失敗 (原本は保存済み): %s", artifact.local_path)
        artifact.convert_status = ConvertStatus.FAILED
    return artifact
