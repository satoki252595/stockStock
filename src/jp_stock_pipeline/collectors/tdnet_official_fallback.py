"""TDnet 公式「適時開示情報閲覧サービス」直接パースのフォールバックコレクター
(DESIGN.md §11: やのしんAPI停止リスク対応。P3で同時実装し後回しにしない)。

エンドポイント:
    GET https://www.release.tdnet.info/inbs/I_list_{ページ番号3桁}_{YYYYMMDD}.html
    例: I_list_001_20260610.html（1ページ=最大100件、当日含む直近約1ヶ月分のみ公開）

実レスポンス構造（2026-06-10 取得のフィクスチャで確認）:
- ``<table id="main-list-table">`` の各 ``<tr>`` が1開示。列は
  時刻(kjTime "HH:MM") / コード(kjCode 5桁) / 会社名(kjName) /
  表題(kjTitle, 相対PDFリンク) / XBRL(kjXbrl, あればzipリンク) /
  上場取引所(kjPlace) / 更新履歴(kjHistroy)
- ページャは ``onclick="pagerLink('I_list_002_20260610.html')"`` 形式。
  連番でループし、リンクに無いページ・404 で停止する
- 文書は UTF-8（meta が非標準形式のため lxml の自動判別に任せず明示デコード）

インターフェース契約（やのしん版と共通・§4 ソース抽象化）:
    list_disclosures_official(settings, target_date)
        -> (RawArtifact, list[DisclosureRecord])
  返り値の DisclosureRecord は collectors/tdnet_yanoshin.list_disclosures と
  完全に同一形式。doc_id は PDF ファイル名由来でやのしん側と同一値になるため、
  ④ の冪等 upsert キーがソースを跨いで安定する (CONTRACTS 不変条件5)。

原本保存 (§5.1): 1ページ取得=1原本。複数ページの日は
``fetch_list_pages()`` が全ページ分の RawArtifact を返すので、ジョブは
そちらを使って全原本を⑤へアップロードすること（原本必須 §8.1-4）。
``list_disclosures_official`` は共通シグネチャ維持のため代表として
1ページ目の RawArtifact を返す。
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from urllib.parse import urljoin

import lxml.html

from ..config import Settings
from ..http import FetchError, fetch
from ..licensing import source_license
from ..models import JST, DisclosureRecord, Provenance, RawArtifact, Source
from ..rawstore import save_raw
from .tdnet_yanoshin import classify_title, corporate_action_attrs, normalize_company_code

logger = logging.getLogger(__name__)

BASE_URL = "https://www.release.tdnet.info/inbs/"

# 連番ループの安全上限（1日100件×40ページ=4,000開示を超える日は実在しない）
MAX_PAGES = 40

_PAGE_LINK_RE = re.compile(r"I_list_(\d{3})_(\d{8})\.html")


def page_url(target_date: date, page_no: int) -> str:
    """ページURL: I_list_{ページ番号3桁}_{YYYYMMDD}.html"""
    return f"{BASE_URL}I_list_{page_no:03d}_{target_date:%Y%m%d}.html"


def _known_page_numbers(html_text: str, target_date: date) -> set[int]:
    """ページャの onclick からその日のページ番号集合を抽出する。"""
    datestr = f"{target_date:%Y%m%d}"
    return {
        int(num)
        for num, d in _PAGE_LINK_RE.findall(html_text)
        if d == datestr
    }


def parse_official_page(
    html_text: str,
    target_date: date,
    *,
    fetched_at: datetime,
    raw_page_id: str | None = None,
) -> list[DisclosureRecord]:
    """公式一覧ページ1枚分の HTML を DisclosureRecord 群へ変換する（値不変）。

    - 相対PDFリンクは BASE_URL で絶対URL化
    - doc_id は PDF ファイル名（拡張子除く）から導出（やのしん側と同一値）
    - has_xbrl は XBRL 列のリンク有無
    - 時刻は HH:MM のみ公開のため秒=00 の JST datetime（値の捏造ではなく
      公式ページの粒度そのまま）
    """
    doc = lxml.html.fromstring(html_text)
    records: list[DisclosureRecord] = []
    for row in doc.xpath('//table[@id="main-list-table"]/tr'):
        title_links = row.xpath('.//td[contains(@class,"kjTitle")]//a')
        if not title_links:
            # PDFリンクが無い行は doc_id（④の冪等キー）を導出できない。
            # 捏造IDは作らずスキップ（原本HTMLには残る §3）
            logger.warning("PDFリンクなしの行をスキップ: %s", row.text_content()[:60])
            continue
        link = title_links[0]
        href = (link.get("href") or "").strip()
        pdf_name = href.rsplit("/", 1)[-1]
        if not pdf_name.lower().endswith(".pdf"):
            logger.warning("PDF以外のリンクをスキップ: %s", href)
            continue
        doc_id = pdf_name[: -len(".pdf")]
        title = link.text_content().strip()

        time_text = _cell_text(row, "kjTime")
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", time_text)
        if not m:
            logger.warning("時刻を解釈できずスキップ: %r (doc_id=%s)", time_text, doc_id)
            continue
        disclosed_at = datetime(
            target_date.year, target_date.month, target_date.day,
            int(m.group(1)), int(m.group(2)), tzinfo=JST,
        )

        doc_type = classify_title(title)
        split_ratio, split_factor, effective_date = corporate_action_attrs(title, doc_type)
        records.append(
            DisclosureRecord(
                doc_id=doc_id,
                title=title,
                disclosed_at=disclosed_at,
                provenance=Provenance(
                    source=Source.TDNET,
                    license_tag=source_license(Source.TDNET),  # factual-cite (§2.2)
                    data_date=target_date,
                    fetched_at=fetched_at,
                    raw_page_id=raw_page_id,
                ),
                code=normalize_company_code(_cell_text(row, "kjCode")),
                doc_type=doc_type,
                source_url=urljoin(BASE_URL, href),
                has_xbrl=bool(row.xpath('.//td[contains(@class,"kjXbrl")]//a')),
                split_ratio=split_ratio,
                split_factor=split_factor,
                effective_date=effective_date,
            )
        )
    return records


def _cell_text(row: lxml.html.HtmlElement, klass: str) -> str:
    cells = row.xpath(f'.//td[contains(@class,"{klass}")]')
    return cells[0].text_content().strip() if cells else ""


def fetch_list_pages(
    settings: Settings, target_date: date
) -> list[tuple[RawArtifact, list[DisclosureRecord]]]:
    """指定日の一覧を全ページ取得する（1ページ=1原本 §5.1）。

    ページャリンクで判明したページを連番で辿り、未知ページ・404 で停止する。
    1ページ目の取得失敗はその日の一覧自体が取得不能として FetchError を送出
    （欠損として記録するのは呼び出し側 §3-2）。
    """
    results: list[tuple[RawArtifact, list[DisclosureRecord]]] = []
    known_pages = {1}
    page_no = 1
    while page_no <= MAX_PAGES and page_no in known_pages:
        url = page_url(target_date, page_no)
        try:
            resp = fetch(url)
        except FetchError:
            if page_no == 1:
                raise  # その日の一覧が存在しない（休日等）or 取得不能
            logger.warning("ページ %d の取得に失敗。ここまでで停止: %s", page_no, url)
            break
        content = resp.content
        html_text = content.decode("utf-8")  # メタ宣言どおり UTF-8 を明示
        # 構造検証 (CONTRACTS): 一覧テーブルが無い 200 応答 (メンテ/エラーページ)
        # は原本保存しない。1ページ目なら一覧自体の取得不能として FetchError (§3)
        if 'id="main-list-table"' not in html_text:
            if page_no == 1:
                raise FetchError(
                    f"公式TDnet一覧に main-list-table が無い (エラーページ?): {url}"
                )
            logger.warning("ページ %d が一覧形式でない。ここまでで停止: %s", page_no, url)
            break
        artifact = save_raw(
            content,
            source=Source.TDNET,
            datatype="tdnet_official_list",
            scope=f"p{page_no:03d}",  # ページごとに原本ファイル名を一意化
            data_date=target_date,
            url=url,
            ext="html",
            license_tag=source_license(Source.TDNET),
            base_dir=settings.raw_data_dir,
        )
        records = parse_official_page(
            html_text, target_date, fetched_at=artifact.fetched_at
        )
        results.append((artifact, records))
        known_pages |= _known_page_numbers(html_text, target_date)
        page_no += 1
    return results


def list_disclosures_official(
    settings: Settings, target_date: date
) -> tuple[RawArtifact, list[DisclosureRecord]]:
    """指定日の適時開示一覧を公式ページから取得する（やのしん版フォールバック）。

    Returns:
        (RawArtifact, list[DisclosureRecord])
        RawArtifact は1ページ目の原本（代表）。複数ページの日に全ページの
        原本 RawArtifact が必要な場合は fetch_list_pages() を使うこと
        （原本必須 §8.1-4。本関数の records は全ページ分を含む）。

    Note:
        collectors/tdnet_yanoshin.list_disclosures と同一の返り値契約
        （§4 ソース抽象化）。DisclosureRecord のフィールド・doc_id 体系も
        同一のため、やのしん停止時 (§11) にそのまま差し替えられる。
    """
    pages = fetch_list_pages(settings, target_date)
    first_artifact = pages[0][0]
    all_records = [rec for _artifact, recs in pages for rec in recs]
    return first_artifact, all_records
