"""TDnet 適時開示コレクター（やのしん TDnet WEB-API 経由）(DESIGN.md §4 トラックA, §12)。

エンドポイント (§12):
    GET https://webapi.yanoshin.jp/webapi/tdnet/list/{target}.json?limit=N
    target = "recent" | "YYYYmmdd" | 銘柄コード（公開API・キー不要）

実レスポンス構造（2026-06-10 取得のフィクスチャで確認）:
- ``items[]`` は target によって 2 形式が実在する:
  - ``recent``: ``items[].Tdnet`` 配下に各フィールド（ラップ形式）
  - ``YYYYmmdd``: ``items[]`` 直下に各フィールド（フラット形式）
  パーサは両形式を受け付ける。
- フィールド: ``id`` / ``company_code``(5桁) / ``company_name`` /
  ``pubdate``("YYYY-MM-DD HH:MM:SS" JST) / ``title`` / ``document_url`` /
  ``url_xbrl`` / ``markets_string`` / ``update_history`` 等。
- ``document_url`` は ``https://webapi.yanoshin.jp/rd.php?<直URL>`` 形式の
  リダイレクトラッパで返る場合がある（recent で確認）→ 直URLへ展開する。

ライセンス: やのしんAPIは個人運営の非公式API (§2.1)。開示メタデータは
``factual-cite`` として扱い、原文PDFは内部保管に留める。
個人運営ゆえ停止リスクがあるため、公式 TDnet ページパースの
``collectors/tdnet_official_fallback.py`` と同一インターフェースを提供する
（§4 ソース抽象化, §11）。

インターフェース契約（フォールバックと共通・§4）:
    list_disclosures*(settings, target, ...) -> (RawArtifact, list[DisclosureRecord])
真実性 (§3): 取得失敗は FetchError のまま送出し、欠損は欠損として呼び出し側
（ジョブ）が記録する。値の推定・補間は一切行わない。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from urllib.parse import urlsplit

from ..config import Settings
from ..contracts.stock_code import source_code_to_ticker
from ..http import FetchError, fetch
from ..licensing import source_license
from ..models import JST, DisclosureRecord, Provenance, RawArtifact, Source
from ..rawstore import save_raw

logger = logging.getLogger(__name__)

BASE_URL = "https://webapi.yanoshin.jp/webapi/tdnet/list"

# ---------------------------------------------------------------------------
# 書類種別の判定（④ 開示書類DB「書類種別」select と一致 §6.4）
# 判定キーワードは定数化し tests/test_tdnet_classify.py で実在タイトルにより検証する
# ---------------------------------------------------------------------------

DOC_TYPE_TANSHIN = "短信"
DOC_TYPE_FORECAST_REVISION = "業績修正"
DOC_TYPE_DIVIDEND_REVISION = "配当修正"
DOC_TYPE_BUYBACK = "自社株買い"
DOC_TYPE_LARGE_HOLDING = "大量保有"
DOC_TYPE_ANNUAL_REPORT = "有報"
DOC_TYPE_QUARTERLY_REPORT = "四半期報告"
# コーポレートアクション (§ Phase2 イベント台帳)
DOC_TYPE_SPLIT = "株式分割"
DOC_TYPE_CONSOLIDATION = "株式併合"
DOC_TYPE_DELISTING = "上場廃止"
DOC_TYPE_NEW_LISTING = "新規上場"
DOC_TYPE_YUTAI = "優待"
DOC_TYPE_OTHER = "その他"

# 比率を構造化抽出する書類種別 / 効力発生日を抽出する書類種別
RATIO_DOC_TYPES: frozenset[str] = frozenset({DOC_TYPE_SPLIT, DOC_TYPE_CONSOLIDATION})
CORPORATE_ACTION_TYPES: frozenset[str] = frozenset(
    {DOC_TYPE_SPLIT, DOC_TYPE_CONSOLIDATION, DOC_TYPE_DELISTING, DOC_TYPE_NEW_LISTING}
)

KW_TANSHIN = "決算短信"
KW_FORECAST = "業績予想"
KW_REVISION = "修正"
KW_DIVIDEND = "配当"
KW_TREASURY = "自己株式"
KW_ACQUIRE = "取得"
KW_LARGE_HOLDING = "大量保有"
KW_ANNUAL_REPORT = "有価証券報告書"
KW_QUARTERLY_REPORT = "四半期報告書"
# 実開示には「株式の分割」のように助詞「の」を挟む表記が存在する（実測1件、
# 2026-09-11時点のTDnetフィクスチャ全3,137タイトル中）。単純部分一致では
# 拾えないため、判定は正規表現で助詞ありも同一視する。
_SPLIT_RE = re.compile(r"株式の?分割")
_CONSOLIDATION_RE = re.compile(r"株式の?併合")


def _mentions_split(title: str) -> bool:
    return bool(_SPLIT_RE.search(title))


def _mentions_consolidation(title: str) -> bool:
    return bool(_CONSOLIDATION_RE.search(title))
KW_DELISTING = "上場廃止"
KW_NEW_LISTING = "新規上場"
KW_YUTAI = "株主優待"

# 上場廃止/新規上場で ① の状態を倒すのは「確定的な本体告知」のみ。否定・回避・
# リスク段階・解除・派生修正・第三者(子会社等)の言及を含むタイトルは状態を
# 倒さず「その他」へ回し人間判断に委ねる (§3-1 誤った権威的値は欠損より悪い /
# §3-7。分割/併合と対称な偽陽性抑制)。例で抑制されるもの:
#   「上場廃止に係る猶予期間入り」(まだ監理/整理段階) /「…の解消」(上場維持) /
#   「上場廃止基準への抵触回避」「…には該当しない旨」(健全) /「…のおそれ」/
#   「連結子会社◯◯の上場廃止」(開示主体は上場継続) /「上場廃止に伴う配当予想の修正」
_LIFECYCLE_NEGATION_KW: tuple[str, ...] = (
    "該当しない", "抵触しない", "非該当", "回避", "おそれ", "可能性",
    "猶予", "解除", "解消", "見送り", "中止", "撤回",
)
_LIFECYCLE_THIRD_PARTY_KW: tuple[str, ...] = (
    "子会社", "孫会社", "対象者", "関連会社", "持分法",
)
# 普通株は継続したまま別証券クラスのみを廃止する開示は、普通株コードに紐づく ① の
# 状態を倒してはならない（普通株は上場継続）。
_LIFECYCLE_OTHER_SECURITY_KW: tuple[str, ...] = (
    "優先株式", "優先出資", "種類株式", "新株予約権付社債", "優先証券",
)


def _is_genuine_lifecycle_event(title: str) -> bool:
    """上場廃止/新規上場タイトルが ① の状態を倒してよい確定的告知かを判定する。

    確信が持てない(否定/回避/段階/解除/派生修正/第三者主体/別証券クラス)場合は False
    を返し、呼び出し側は「その他」へ分類する。取りこぼし(本物を False 判定)は ① の
    状態を倒さない安全側であり、④ への記録自体は残るため人間が原文で判断できる (§3-1)。
    """
    if any(kw in title for kw in _LIFECYCLE_NEGATION_KW):
        return False
    # 「(株式交換/TOB等による)完全子会社化に伴う上場廃止」は開示主体自身の自己廃止
    # (任意廃止の主流)。第三者語「子会社」を含むが本人の廃止なので第三者抑制から外す。
    if "子会社化" not in title and any(kw in title for kw in _LIFECYCLE_THIRD_PARTY_KW):
        return False
    if any(kw in title for kw in _LIFECYCLE_OTHER_SECURITY_KW):
        return False
    # 「上場廃止に伴う配当予想の修正」のように本体が(配当/業績)修正の派生開示
    if KW_REVISION in title and ("に伴う" in title or "に関連" in title):
        return False
    return True


def classify_title(title: str) -> str:
    """開示タイトルから書類種別を判定する (§6.4 書類種別 select)。

    判定は上から順の優先度付き（例: 「第１四半期決算短信」は短信扱い、
    「業績予想及び配当予想の修正」は業績修正扱い）。コーポレートアクション
    （分割/併合/上場廃止/新規上場）は短信の次に判定する（§ Phase2）。
    どれにも該当しなければ「その他」。推定はせずキーワード一致のみ。
    """
    if KW_TANSHIN in title:
        return DOC_TYPE_TANSHIN
    if _mentions_split(title) or _mentions_consolidation(title):
        # 「株式分割に伴う配当予想の修正」のように分割を“言及”するだけで本体は修正、
        # という開示は、比率がタイトルに無く修正パターンに合致するなら修正へ回す
        # （本物の分割告知は比率を明記する。§ Phase2 偽陽性の抑制）。
        is_revision = (KW_FORECAST in title and KW_REVISION in title) or (
            KW_DIVIDEND in title and KW_REVISION in title
        )
        if parse_split_terms(title)[1] is not None or not is_revision:
            return DOC_TYPE_SPLIT if _mentions_split(title) else DOC_TYPE_CONSOLIDATION
    if KW_DELISTING in title and _is_genuine_lifecycle_event(title):
        return DOC_TYPE_DELISTING  # 「上場廃止」を「新規上場」より先に判定
    if KW_NEW_LISTING in title and _is_genuine_lifecycle_event(title):
        return DOC_TYPE_NEW_LISTING
    if KW_FORECAST in title and KW_REVISION in title:
        return DOC_TYPE_FORECAST_REVISION
    if KW_DIVIDEND in title and KW_REVISION in title:
        return DOC_TYPE_DIVIDEND_REVISION
    # 優待は業績修正・配当修正より後。実開示に「期末配当予想の修正及び株主優待制度の
    # 変更」のような複合開示があり、投資判断上は配当修正の方が重い（実データ 3 件で確認）。
    if KW_YUTAI in title:
        return DOC_TYPE_YUTAI
    if KW_TREASURY in title and KW_ACQUIRE in title:
        return DOC_TYPE_BUYBACK
    if KW_LARGE_HOLDING in title:
        return DOC_TYPE_LARGE_HOLDING
    if KW_ANNUAL_REPORT in title:
        return DOC_TYPE_ANNUAL_REPORT
    if KW_QUARTERLY_REPORT in title:
        return DOC_TYPE_QUARTERLY_REPORT
    return DOC_TYPE_OTHER


# 全角→半角（数字・コロン）正規化。値の改変ではなく形式変換のみ (§5.2)
_FW_TRANSLATE = str.maketrans("０１２３４５６７８９：", "0123456789:")

# "1株を3株に分割" / "1株につき3株" / "1株を3株とする" / "1対3株"（末尾に株）
_SPLIT_PAIR_RE = re.compile(r"(\d+)\s*株?\s*(?:を|につき|対|:)\s*(\d+)\s*株")
# "1:3" / "1対3"（株を伴わない比率表記）
_RATIO_RE = re.compile(r"(\d+)\s*[:対]\s*(\d+)")
# "効力発生日 2026年4月1日" 等（効力発生の語に日付が直近する場合のみ拾う）。
# 連結子を限定し「効力発生日に先立つ基準日を YYYY年…」のような後続の別日付
# (基準日) を誤って掴まない (§ Phase2。誤った権威的日付は欠損より悪い §3-1)。
_EFFECTIVE_DATE_RE = re.compile(
    r"効力発生日?[\s（(:：・,，。．をはがと/／]{0,6}(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
)


def parse_split_terms(title: str) -> tuple[str | None, float | None]:
    """分割/併合タイトルから (比率テキスト, 係数) を最善努力で抽出する (§ Phase2/4)。

    例: 「1株を3株に分割」→ ("1:3", 3.0)、「5株を1株に併合」→ ("5:1", 0.2)。
    係数 = 新株数 / 旧株数（分割>1, 併合<1。Phase4 の価格調整に使える一次属性）。
    タイトルから明確に取れない場合は (None, None)（推定しない §3-1。原文に委ねる）。
    """
    t = title.translate(_FW_TRANSLATE)
    m = _SPLIT_PAIR_RE.search(t) or _RATIO_RE.search(t)
    if not m:
        return None, None
    old, new = int(m.group(1)), int(m.group(2))
    if old <= 0 or new <= 0:
        return None, None
    return f"{old}:{new}", new / old


def parse_effective_date(title: str) -> date | None:
    """タイトルに「効力発生日 YYYY年M月D日」があれば date を返す (§ Phase2)。

    タイトルに効力発生日が無い場合（多くはこちら）は None。発表日(開示日)を
    効力発生日に流用しない（§3-1。権利タイミングは原文リンクで確認する）。
    """
    m = _EFFECTIVE_DATE_RE.search(title.translate(_FW_TRANSLATE))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def corporate_action_attrs(
    title: str, doc_type: str
) -> tuple[str | None, float | None, date | None]:
    """書類種別に応じてコーポレートアクション属性 (比率, 係数, 効力発生日) を抽出。"""
    split_ratio: str | None = None
    split_factor: float | None = None
    effective_date: date | None = None
    if doc_type in RATIO_DOC_TYPES:
        split_ratio, split_factor = parse_split_terms(title)
    if doc_type in CORPORATE_ACTION_TYPES:
        effective_date = parse_effective_date(title)
    return split_ratio, split_factor, effective_date


# ---------------------------------------------------------------------------
# フィールド正規化（値不変の形式変換のみ §5.2）
# ---------------------------------------------------------------------------


def normalize_company_code(raw_code: str | None) -> str | None:
    """TDnet の5文字コードを4文字基準コードへ正規化する。

    判定と正規化は `contracts/stock_code.py` の `source_code_to_ticker` に委譲する
    （kabulab-cf の `companyCodeToTicker` / `secCodeToTicker` と同一実装）。

    ここに独自の正規表現を持っていたため、同じ入力の答えが他の5実装と割れていた:

    - `"07203"` → `"0720"`、`"25935"` → `"2593"` を返していた。`"2593"` は
      伊藤園 普通株で `"25935"` は同社 第1種優先株式。本番 `core_stocks` に
      両方が実在するため、優先株の開示を普通株へ付け替える**取り違え**だった。
    - `"A130"`（1桁目英字。JPX 付番体系に無い）をそのまま通していた。
    - `"130a"`（小文字）と `"７２０３"`（全角）を None にしていた。

    **挙動が変わる点:** 末尾検査文字が "0" でない5文字（旧 docstring が
    「末尾は "4" の実例もある」として挙げていた例。ここでは合成コード
    `"12024"` → `"1202"` で示す）は
    None を返す。本番 `ir_disclosures` 37,641 行の `company_code` は全件が
    末尾 "0" で（実測）、合成コード `1202` は `core_stocks` に不在（母集団は
    内国普通株のみ）なので、旧実装でも次段の母集団突合で落ちていた。
    取りこぼしは増えない。
    """
    return source_code_to_ticker(raw_code)


def direct_document_url(url: str) -> str:
    """やのしんの rd.php リダイレクトラッパを直URLへ展開する。

    例: https://webapi.yanoshin.jp/rd.php?https://www.release.tdnet.info/...pdf
    ラッパでなければそのまま返す（値不変）。
    """
    marker = "rd.php?"
    idx = url.find(marker)
    if idx >= 0:
        return url[idx + len(marker):]
    return url


def doc_id_from_document_url(url: str | None) -> str | None:
    """document_url の PDF ファイル名から安定IDを導出する。

    TDnet の PDF ファイル名（例 140120260610567733.pdf）は開示1件に一意で、
    公式ページフォールバック (tdnet_official_fallback) と同一の doc_id になる
    ため、④ の冪等 upsert キー (CONTRACTS 不変条件5) としてソース間で安定する。
    導出できなければ None。
    """
    if not url:
        return None
    path = urlsplit(direct_document_url(url)).path
    name = path.rsplit("/", 1)[-1]
    if name.lower().endswith(".pdf") and len(name) > 4:
        return name[: -len(".pdf")]
    return None


def _parse_pubdate(pubdate: str) -> datetime:
    """pubdate "YYYY-MM-DD HH:MM:SS"（JST表記）を tz 付き datetime にする。"""
    return datetime.strptime(pubdate, "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)


def _unwrap_item(item: dict) -> dict:
    """items[] 要素を両形式（Tdnet ラップ / フラット）から取り出す。"""
    inner = item.get("Tdnet")
    return inner if isinstance(inner, dict) else item


def xbrl_url_map(payload: dict) -> dict[str, str]:
    """payload から doc_id → XBRL zip 直URL の対応を作る（短信XBRL→③用 §8.2）。

    url_xbrl が無い開示は含めない（欠損は欠損のまま §3-1）。
    """
    mapping: dict[str, str] = {}
    for item in payload.get("items", []):
        t = _unwrap_item(item)
        doc_id = doc_id_from_document_url(t.get("document_url"))
        url_xbrl = t.get("url_xbrl")
        if doc_id and url_xbrl:
            mapping[doc_id] = direct_document_url(str(url_xbrl))
    return mapping


def parse_list_payload(
    payload: dict,
    *,
    fetched_at: datetime,
    raw_page_id: str | None = None,
) -> list[DisclosureRecord]:
    """やのしん list レスポンス JSON を DisclosureRecord 群へ変換する（値不変）。

    取得できないフィールドは None のまま（§3-1）。doc_id は document_url の
    PDF ファイル名由来を優先し、無ければやのしんの id を使う。
    """
    records: list[DisclosureRecord] = []
    for item in payload.get("items", []):
        t = _unwrap_item(item)
        title = (t.get("title") or "").strip()
        pubdate = t.get("pubdate")
        if not pubdate:
            # 開示日時が無い行は④の必須キーを満たせない。捏造せずスキップして記録
            logger.warning("pubdate 欠損のためスキップ: id=%s title=%s", t.get("id"), title[:40])
            continue
        disclosed_at = _parse_pubdate(pubdate)
        source_url = direct_document_url(t["document_url"]) if t.get("document_url") else None
        doc_id = doc_id_from_document_url(source_url)
        if doc_id is None:
            yanoshin_id = t.get("id")
            if not yanoshin_id:
                logger.warning("doc_id を導出できずスキップ: title=%s", title[:40])
                continue
            doc_id = f"yanoshin-{yanoshin_id}"
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
                    data_date=disclosed_at.date(),
                    fetched_at=fetched_at,
                    raw_page_id=raw_page_id,
                ),
                code=normalize_company_code(t.get("company_code")),
                doc_type=doc_type,
                source_url=source_url,
                has_xbrl=bool(t.get("url_xbrl")),
                split_ratio=split_ratio,
                split_factor=split_factor,
                effective_date=effective_date,
            )
        )
    return records


# ---------------------------------------------------------------------------
# 取得（§8.1 step 1-2: fetch → save_raw）
# ---------------------------------------------------------------------------


def _target_data_date(target: str) -> date | None:
    """target が YYYYmmdd ならその日付。recent / 銘柄コードは特定日を持たず None。"""
    if re.fullmatch(r"\d{8}", target):
        return datetime.strptime(target, "%Y%m%d").date()
    return None


def list_disclosures(
    settings: Settings,
    target: str = "recent",
    limit: int = 300,
) -> tuple[RawArtifact, list[DisclosureRecord]]:
    """適時開示一覧を取得し、原本保存して DisclosureRecord 群を返す。

    Args:
        settings: 共通設定（raw_data_dir を使用）
        target: "recent" | "YYYYmmdd" | 銘柄コード
        limit: 最大件数（やのしん側パラメータ）

    Returns:
        (RawArtifact, list[DisclosureRecord])
        RawArtifact は datatype="tdnet_list", scope=target, license_tag=factual-cite。
        ⑤へのアップロードと provenance.raw_page_id の設定は呼び出し側ジョブが行う
        （§8.1 step 4: 原本必須）。

    Note:
        tdnet_official_fallback.list_disclosures_official と同一の返り値契約
        （ソース抽象化 §4。やのしん停止時は差し替え可能 §11）。
    """
    url = f"{BASE_URL}/{target}.json"
    resp = fetch(url, params={"limit": limit})
    content = resp.content
    try:
        payload = json.loads(content)
    except ValueError as exc:
        raise FetchError(f"やのしん list 応答が JSON でない: {url}") from exc
    # 構造検証 (CONTRACTS「HTTP 200 のエラーレスポンス」): items が list でない
    # 応答はエラー/制限メッセージとみなし、原本保存せず FetchError とする。
    # {"items": []} は正当な0件として通す
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise FetchError(f"やのしん list 応答が想定外構造 (items 欠落): {url}")
    artifact = save_raw(
        content,
        source=Source.TDNET,
        datatype="tdnet_list",
        scope=target,
        data_date=_target_data_date(target),
        url=f"{url}?limit={limit}",
        ext="json",
        license_tag=source_license(Source.TDNET),
        base_dir=settings.raw_data_dir,
    )
    records = parse_list_payload(payload, fetched_at=artifact.fetched_at)
    return artifact, records
