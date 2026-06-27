# 日本株データ収集基盤 設計書 v2（Claude Code実装指示書 兼用）

最終更新: 2026-06-10 / 作成: はぴまね × Claude（Cowork）
本ファイル1つをClaude Codeに渡せば実装を開始できる構成になっている。

---

## 0. v2での変更点（ユーザーフィードバック反映）

1. Notionは**有料プラン**前提（1ファイル20MB単純アップロード、マルチパートで最大5GB。5MB制限の考慮は撤廃）
2. 原本アップロード時に**DB扱いしやすい形式への変換版（CSV/Parquet/JSON）を同時生成・保存**する変換レイヤーを追加
3. **利用者別（投資家/データサイエンティスト/開発者）に最適なデータ形式で取得できる設計**を追加。利用のしやすさ最優先
4. **データ真実性ポリシーを新設**: フォールバックは実データの別ソースのみ。ダミー・推定値・補間値は一切生成しない
5. **商用利用コンプライアンス評価を新設**（§2）。J-Quants等の規約調査結果を反映し、全データに「ライセンスタグ」を付与する設計に変更
6. 設計書と実装ハンドオフを本ファイル1つに統合

---

## 1. 目的とゴール

日本株のファンダメンタルズ情報・テクニカル情報を無料ソースで継続的に収集し、Notionページ「株式情報」（`205d74ff84cd809e92b4dd1adc5cf186`）配下に**銘柄コード単位で構造化**して格納する。

- **取得単位ごとに、取得元の原本ファイルを専用DBへ物理保存する（必須要件）**。加えて利用しやすい変換版も保存する
- 将来的にページを公開し、投資家の参照・データサイエンティストのAPI/バルク取得に耐える構造とする
- **商用利用の可能性があるため、ライセンス上安全なデータだけを公開・商用導線に流せるアーキテクチャとする**

### 確定済み方針

| 項目 | 決定 |
|---|---|
| 対象銘柄 | 全上場銘柄（約3,900社）、段階的取り込み |
| 実行環境 | GitHub Actions（cron） |
| API登録 | EDINET APIキー（登録系）。株価は登録不要の yfinance/stooq |
| 原本保存 | Notion File Upload API（**有料プラン**: 単純UL 20MB、マルチパート最大5GB） |
| 商用利用 | 可能性あり → ライセンスタグで商用可データと私的利用限定データを厳格分離 |

---

## 2. 商用利用コンプライアンス評価（最重要・2026-06-10調査）

### 2.1 ソース別評価

| ソース | 商用利用 | 再配布・公開 | 根拠・条件 |
|---|---|---|---|
| **EDINET API v2** | ✅ **可** | ✅ **可** | 公共データ利用規約(PDL1.0)/政府標準利用規約準拠。営利目的含む二次利用が明文で可。**条件: 出典記載（「EDINET（金融庁）」等）+ 編集・加工した場合はその旨を明記** |
| **TDnet開示資料（PDF/XBRL本体）** | ⚠️ 条件付き | ⚠️ 注意 | 開示資料の著作権は各上場会社。公衆縦覧目的の法定/適時開示文書であり、**事実データの抽出・出典明記のうえの利用は一般に可**だが、PDF原文の大量転載は避け**原文はTDnet/EDINETへのリンク+内部保管に留めるのが安全**。網羅的・商用の開示配信はJPXが有料「TDnet API」を販売しており、本格商用時はJPX契約を検討 |
| **やのしんTDnet WEB-API** | ⚠️ 自己責任 | — | 個人運営の非公式API。明確な商用規約なし。**インデックス（一覧メタデータ）取得の手段**として利用し、依存しない設計（公式TDnetページパースのフォールバック必須）。商用本格運用時は公式有料APIへの移行パスを確保 |
| **J-Quants API（無料/個人版）** | ❌ **不可** | ❌ **不可** | 規約上**登録者本人の私的利用に限定。商用利用・第三者提供・SaaS組込・再配布は禁止**（商用はJ-Quants Pro法人契約が必要）。→ **2026-06 に本実装から完全に除外（採用せず）**。担っていた①③財務はEDINET/TDnet（commercial-ok/factual-cite）が上位互換で代替、②終値の突合検証はstooqへ移管（§3-5, §8.2 `reconcile_weekly`）。商用化時はPro契約で再導入も可 |
| **Yahoo Finance（yfinance）** | ❌ **不可** | ❌ **不可** | ToSが自動アクセス・商用利用・収益化を禁止（非公式ライブラリは内部APIを叩いている）。→ **同上、個人検証用トラックに隔離**。商用の直近株価は有料ベンダー（J-Quants Pro等）への移行パスを設計 |
| **JPXサイト（data_j.xls、空売り・信用残統計）** | ❌ 原則不可 | ❌ 原則不可 | サイト利用規約で「**契約または許可なき商用目的のデータ収集・二次利用は不可**」。→ 商用トラックでは銘柄マスタを**EDINETコードリスト（金融庁公開・商用可）で代替**。JPX統計は個人検証用トラック扱い |
| **stooq.com** | ⚠️ 不明確 | ❌ 避ける | ポーランドの無料サイトで明確な商用許諾なし。個人検証用フォールバックに限定 |

### 2.2 設計への反映: ライセンスタグと2トラック構成

全データ行・全原本ファイルに**ライセンスタグ**を必須付与する。

| タグ | 意味 | 該当ソース | 公開/商用導線 |
|---|---|---|---|
| `commercial-ok` | 商用・再配布可（出典記載条件） | EDINET、自前計算指標（commercial-okデータのみから算出したもの） | ✅ 公開可 |
| `factual-cite` | 事実データの抽出利用可・原文は内部保管 | TDnet開示メタデータ+抽出事実 | ⚠️ メタデータ+リンクのみ公開 |
| `personal-only` | 私的利用限定。公開・商用組込禁止 | yfinance、stooq、JPXサイト統計 | ❌ 非公開ビューに隔離 |

- **計算指標の汚染防止**: `personal-only`データを入力に含む計算結果も`personal-only`を継承する（最も厳しいタグを継承）
- 公開ページ・エクスポートAPIは`commercial-ok`（+`factual-cite`のメタデータ）のみをフィルタして提供する実装とする
- **商用化判断時のチェックリスト**（実装に含める）: J-Quants Pro契約可否、JPX TDnet API契約可否、株価ベンダー選定、公開前の各規約再確認

> 注: 本評価は2026-06-10時点の各公開規約の調査に基づく整理であり、法的助言ではない。商用ローンチ前に必ず原文規約の確認（必要に応じ専門家確認）を行うこと。

---

## 3. データ真実性ポリシー（金融情報のため必須遵守）

実装上の不変条件として全コードに適用する。

1. **ダミー・サンプル・推定値・補間値の生成禁止**。取得できなかった値はnull（Notionでは空欄）とし、欠損を欠損として表示する
2. **フォールバックは「実在する別ソースの実データ」のみ**。例: yfinance失敗→stooqの実データ。全ソース失敗→欠損として記録し、ジョブログに失敗を明記。決して前日値コピーや平均値埋めをしない
3. **来歴（プロベナンス）の必須記録**: 全行に「ソース」「データ基準日」「取得日時」「原本ファイルへのリレーション」を持たせ、どの値も原本まで遡れる状態を保証する
4. **加工の明示**: テクニカル指標等の自前計算値は「計算値（算式名・パラメータ）」であることをプロパティ名とデータカタログで明示（例: RSI14、SMA25乖離率%）。EDINET由来の編集・加工も明記（規約条件でもある）
5. **整合性検証ジョブ**: 独立した第2ソース（stooq の実データ。J-Quants 廃止後の指定フォールバック §11）の同一基準日終値と②を突合し、乖離閾値（例: 終値±1%超）を超えた行に「要確認」フラグを自動付与。値の自動書き換えはせず、より信頼できるソースで更新する場合のみソース名を更新する。第2ソースが取得できない銘柄は「突合対象外」として正直に記録し、欠損を埋めない（§3-1）
6. テスト用フィクスチャは実APIレスポンスの保存物のみ使用し、本番DBにはテストデータを書き込まない（dry-runモードで分離）

### 3-7 コーポレートアクション（分割・併合・上場廃止・新規上場）

株価・財務の連続性を壊すイベントを、**一次開示（TDnet/EDINET）から捕捉し人間判断に委ねる**方針で扱う（黙って価格を調整しない）。実装は段階的:

- **Phase 1（正直化・実装済）**: yfinance は未調整終値のため、テクニカル窓（直近260営業日）に株式分割/併合とみられる単日不連続（既定 ±35%超）があれば ② を `要確認` にする（自動調整はしない §3-5。`transform/technicals.has_probable_split`）。新規上場等で履歴不足（SMA25 すら出ない）の行は `欠損あり` とし「正常」と偽らない。
- **Phase 2（イベント台帳・実装済）**: TDnet タイトルから `株式分割 / 株式併合 / 上場廃止 / 新規上場` を分類し ④ へ。分割/併合は**比率（例 "1:3"）・係数（新株数/旧株数）・効力発生日**をタイトルから最善努力で抽出（取れなければ None、原文リンクに委ねる §3-1）。④ は一次開示由来で commercial-ok/factual-cite。**偽陽性抑制**: 分割/併合は「比率なし＋"〜の修正"」を修正へ回す。上場廃止/新規上場も対称に、否定・回避・段階・解除・派生修正・第三者主体を示す語（`該当しない/抵触しない/回避/おそれ/猶予/解除/解消/見送り/中止/撤回` や `子会社/対象者/関連会社…`、`に伴う◯◯予想の修正`）を含むタイトルは状態を倒さず「その他」へ回す。取りこぼし（本物を「その他」化）は ① の状態を倒さない安全側で、④ への記録自体は残り人間が原文で判断できる（§3-1 誤った権威的値は欠損より悪い）。
- **Phase 3（ライフサイクル・実装済）**: ① に `状態(上場/監理/整理/上場廃止) / 上場日 / 上場廃止日`。フィールドの**所有を分離**して二重書き込みの衝突を防ぐ — 名称/業種/`listed(在リスト=True)` は codelist(`master_sync`) 所有、`状態/上場日/上場廃止日` は**一次開示イベント（`apply_disclosure_lifecycle`）が所有**（`master_sync` は `include_lifecycle=False` でこれらを上書きしない）。`上場廃止` 開示は発表時点では `状態=上場廃止 + 上場廃止日(判明時)` のみ設定し **listed は触らない**（効力発生まで売買継続＝取得継続 §3-1）。確定的な `listed=False`（取得停止）は EDINET コードリストからの消失検知だけが行い、**状態は倒さない**（コードリストの一時的揺らぎで誤検知された銘柄に「上場廃止」という誤った権威的状態を書かない／`listed=True ∧ 状態=上場廃止` の矛盾行を作らない §3-1。状態確定は一次開示に一本化）。消失検知には安全弁（取得コードが既存の50%未満なら一括廃止せず中止し ⑦ に失敗記録）を設ける。日付は開示タイトルから判明した場合のみ書き、不明時は**キー自体を送らず既存値を保持**する（発表日≠効力発生日を流用しない §3-1。かつ効力発生日を持たない後続の同種開示が、先行開示で取り込んだ確定日付を消去しない）。
- **Phase 4（厳密調整・未実装）**: ④ に蓄積した `split_factor` と効力発生日を一次情報として、② テクニカルおよび ③ の EPS/BPS 等を分割調整した連続系列を別途生成する（PER=価格/EPS の基準ズレ解消）。yfinance の `Adj Close`（取得済・未使用）は補助確認に用いる。調整値は必ず開示までトレース可能にし、生の未調整値も保持する。

---

## 4. データソースと役割（2トラック構成）

### トラックA: 公開・商用セーフ（commercial-ok / factual-cite）

| ソース | 取得データ | エンドポイント |
|---|---|---|
| EDINET API v2 | 書類一覧、有報・四半期/半期報告・大量保有等のXBRL/PDF/**CSV**(type=5)、財務諸表詳細、EDINETコードリスト（銘柄マスタ正本） | `GET https://api.edinet-fsa.go.jp/api/v2/documents.json?date=YYYY-MM-DD&type=2&Subscription-Key=KEY` / `GET .../documents/{docID}?type={1\|2\|5}` |
| TDnet（やのしん経由、公式ページパースのフォールバック付き） | 適時開示メタデータ（開示日時・銘柄・タイトル・原文リンク）、決算短信XBRLから抽出した事実数値 | `GET https://webapi.yanoshin.jp/webapi/tdnet/list/{YYYYmmdd\|recent\|code}.json?limit=N`（2026-06-10動作確認済み） |

トラックAだけでも: 銘柄マスタ、財務実績・予想（短信・有報由来）、開示イベント、決算スケジュール（短信予告）、自前計算のファンダ指標が成立する。

### トラックB: 個人検証用（personal-only・非公開ビューに隔離）

| ソース | 取得データ | 備考 |
|---|---|---|
| yfinance | 直近株価OHLCV、時価総額、PER/PBR、配当利回り | `7203.T`形式。スリープ+リトライ+バージョン追従 |
| stooq | 日足CSV（`https://stooq.com/q/d/l/?s=7203.jp&i=d`） | yfinance障害時の実データフォールバック＋②終値の突合検証（`reconcile_weekly` §3-5） |
| JPXサイト統計 | 空売り比率・信用残（週次） | Phase 4 |

> 株価・テクニカルはトラックBでしか成立しないため、**当面は非公開（自分用）**。公開・商用化する場合はJ-Quants Pro等の正規契約で置換する（コレクターを差し替えるだけで済むようソース抽象化インターフェースを切る）。

### 収集対象データ全体マップ

| カテゴリ | 項目 | ソース | タグ | フェーズ |
|---|---|---|---|---|
| 銘柄属性 | コード、名称、業種、EDINETコード | EDINETコードリスト | commercial-ok | P1 |
| 株価 | 日足OHLCV、52週高安、出来高、売買代金 | yfinance/stooq | personal-only | P2 |
| テクニカル | SMA(5/25/75/200)、RSI14、MACD(12,26,9)、BB(20,2σ)、ATR14、乖離率、出来高25日平均比 | 計算（株価由来） | personal-only(継承) | P2 |
| バリュエーション | 時価総額、PER、PBR、配当利回り | yfinance+計算 | personal-only | P2 |
| 財務実績 | 売上・各利益、EPS、BPS、ROE、ROA、自己資本比率、CF、配当 | EDINET XBRL/CSV、短信XBRL | commercial-ok / factual-cite | P3 |
| 業績予想 | 会社予想・修正履歴 | 短信・適時開示 | factual-cite | P3 |
| 開示イベント | 短信、修正、自社株買い、増資、大量保有 | TDnet+EDINET | factual-cite / commercial-ok | P3 |
| 需給 | 信用残、空売り比率 | JPX統計 | personal-only | P4 |
| マクロ | 指数・為替・金利 | yfinance/stooq | personal-only | P4 |

---

## 5. 原本保存と変換レイヤー（必須要件の拡張）

### 5.1 取得単位の定義

APIコール1回 or ダウンロード1回で得たレスポンス/ファイル = 1原本 = 原本ファイルDBの1行。

### 5.2 保存ルール（原本+変換版のペア保存）

| 元形式 | 原本（無加工） | 変換版（同時生成） | 変換方法 |
|---|---|---|---|
| EDINET XBRL(zip) | zipそのまま | **CSV + Parquet**（財務項目をフラットなtidy形式: 銘柄コード, 期間, 項目, 値, 単位, コンテキスト） | EDINET type=5のCSVを優先取得し、無い書類はlxmlでパース |
| EDINET/TDnet PDF | PDFそのまま | **テキスト(.txt)**（抽出可能な場合のみ。抽出不能ならスキップし変換版なしと記録） | pypdfium2等。OCRはしない（真実性優先） |
| APIレスポンスJSON | JSONそのまま | **CSV + Parquet**（正規化テーブル） | pandas |
| xls/xlsx | そのまま | **CSV** | openpyxl/pandas |
| 株価履歴 | 取得生CSV/JSON | **Parquet**（型付き: date, code, OHLCV, adj） | pandas |

- 命名規則: 原本 `{source}_{datatype}_{scope}_{YYYYMMDD}.{ext}` / 変換版 `{同}_converted.{csv|parquet|txt}`
- 変換版は原本と同じDB行のファイルプロパティに併置（1行=1取得単位、ファイル複数添付）し、「変換状態」プロパティで管理
- **変換は値を一切変更しない**（型変換・縦持ち化・文字コード正規化のみ）。変換失敗時も原本保存は成立させ、変換状態=失敗を記録
- 有料プランのため20MB単純アップロードで大半は1ファイルで収まる。20MB超はマルチパートUL（分割gzipは廃止）

---

## 6. Notionデータベース設計

### 6.1 制約

- Notion APIレート制限: 平均3req/s（実測約2,700コール/15分/トークン）→ 2.5req/sにスロットル
- Notionは大量時系列に不向き → 構造化DBは「最新スナップショット」+「イベント行」のみ。**全履歴はParquet/CSV（原本DB添付）で提供**

### 6.2 DB構成（「株式情報」配下に7DB+1ページ）

```
株式情報（既存ページ）
├── 📖 データカタログ（ページ）… スキーマ辞書・ライセンス・更新頻度・利用ガイド(利用者別)
├── ① 銘柄マスタDB        … 1行=1銘柄(構造化ハブ。銘柄コードでユニーク)
├── ② 株価テクニカルDB     … 1行=1銘柄(毎営業日upsertの最新スナップショット) [personal-only]
├── ③ 財務サマリDB        … 1行=銘柄×決算期×開示種別
├── ④ 開示書類DB          … 1行=1開示(TDnet/EDINET統合)
├── ⑤ 原本ファイルDB       … 1行=1取得単位(原本+変換版を添付。必須要件)
├── ⑥ 時系列エクスポートDB  … 1行=1データセット(全銘柄株価全履歴Parquet等のバルク配布物)
└── ⑦ 収集ジョブログDB     … 1行=1ジョブ実行
```

「銘柄コードごとの構造化」は①をハブに②〜⑤をリレーションで紐付けて実現。銘柄ページを開けばその銘柄の株価・財務・開示・原本が全部辿れる。

### 6.3 全DB共通プロパティ（真実性・コンプラ担保）

| プロパティ | 型 | 内容 |
|---|---|---|
| ソース | select | EDINET/TDnet/yfinance/stooq/計算 |
| ライセンスタグ | select | commercial-ok / factual-cite / personal-only |
| データ基準日 | date | その値が指す時点 |
| 取得日時 | date | パイプラインが取得した時刻 |
| 原本 | relation→⑤ | 由来する原本ファイル行 |
| データ品質 | select | 正常 / 要確認(突合乖離) / 欠損あり |

### 6.4 各DB固有プロパティ

**① 銘柄マスタ**: 銘柄名(title)、銘柄コード(text)、市場区分・33業種・17業種(select)、EDINETコード、上場状態、最終データ更新日、②③④⑤へのrelation
**② 株価テクニカル**: 銘柄コード(title)、四本値、前日比率%、出来高、売買代金、時価総額、52週高安、SMA5/25/75/200、SMA25乖離率%、RSI14、MACD/シグナル/ヒストグラム、BB±2σ、ATR14、出来高25日平均比、PER、PBR、配当利回り%
**③ 財務サマリ**: タイトル(例: 7203 2026/03期 本決算)、決算期末、開示種別(本決算/1Q/2Q/3Q/修正/予想)、連結単体、会計基準、売上高〜純利益、EPS、BPS、ROE%、ROA%、自己資本比率%、CF3種、1株配当(実績/予想)、来期予想、開示日
**④ 開示書類**: 開示タイトル、開示日時、書類種別(短信/有報/四半期報告/業績修正/配当修正/大量保有/自社株買い/その他)、書類管理番号(docID等)、取得元URL、XBRL有無
**⑤ 原本ファイル**: ファイル名(title、命名規則)、ファイル(原本+変換版を複数添付)、データ種別、対象銘柄コード(一括は`ALL`)、対象期間、取得URL、SHA256、サイズ、変換状態(完了/失敗/対象外)、関連銘柄relation
**⑥ 時系列エクスポート**: データセット名(title)、ファイル(Parquet/CSV)、対象期間、行数、スキーマ説明、更新日
**⑦ ジョブログ**: ジョブ名、実行日時、ステータス(成功/一部失敗/失敗)、処理/失敗件数、失敗銘柄、GitHub Run URL、所要時間

---

## 7. 利用者別データ提供設計（利用のしやすさ最優先）

| 利用者 | ニーズ | 提供形式 | 実装 |
|---|---|---|---|
| **投資家（見る人）** | 銘柄を開けば全情報が見える。スクリーニング | ①銘柄ページ（リレーションで全情報集約）+ Notionビュー: 「高ROEランキング」「業種別」「直近開示」「決算カレンダー」等のフィルタ/ソート済みビューを自動整備 | `notion-create-view`相当のセットアップをschema.pyに含める |
| **データサイエンティスト** | 機械可読・型付き・一括取得 | **Parquet（第一推奨）+ CSV**。⑥時系列エクスポートDBから全銘柄×全期間のデータセットを直接DL。各原本行にも変換版併置 | 日次/週次でエクスポートジョブが⑥を更新 |
| **開発者/API利用者** | プログラムからの逐次取得 | Notion API（DBが正規化済みなのでそのままREST的に使える）。将来: GitHub上にParquet/CSVを自動コミットしraw URLでレート制限なしのバルク配布（commercial-okのみ） | データカタログにAPI利用例（Python/curl）を記載 |
| **自分（運用者）** | 全トラック横断の確認 | 非公開ビュー含む全DB+ジョブログ | — |

- **データカタログページ**を必ず整備: 各DBのスキーマ辞書（項目名・型・単位・算式・ソース・ライセンス・更新頻度）、利用者別クイックスタート、免責（情報提供のみ・投資助言ではない）、出典表記（EDINET等）
- 公開時は`commercial-ok`/`factual-cite`のみのフィルタビューを公開対象にする

### 7.1 ローカル端末への dual-write + REST API（端末B）

Notion を正本としつつ、収集ジョブが LAN 内の別端末（端末B）の PostgreSQL へ同時ミラー書き込みし、ユーザーが FastAPI（REST + `X-API-Key`）で逐次アクセスできる（`local_store/`）。接続情報は全て `.env`（`LOCAL_DB_*` / `LOCAL_API_KEY`）で管理しコードに埋め込まない（§9）。

- **トポロジ**: 接続プロファイルを収集ジョブの `--db-target` で選ぶ（既定 **cloud**）。cloud=クラウド(GitHub Actions 等)から端末B へ `LOCAL_DB_HOST`+`sslmode=require` で接続。`require` は暗号化のみでサーバ認証はしないため直公開は能動的 MITM に弱く **VPN(Tailscale 等)経由を推奨**（非VPNなら `verify-full`+ルートCA）。GitHub Actions は同名 Secrets で cron 時に自動 dual-write。lan=同一 LAN で `LOCAL_DB_LAN_HOST`+`sslmode=prefer`。両プロファイルとも host を明示しない限り dual-write は無効（lan を localhost へ暗黙フォールバックして誤ミラーしない。API の自DB接続のみ localhost を補完）。`connect_local_store` は対象 host 未設定/接続不可なら `None` を返し Notion のみで稼働。
- **双方向フェールセーフ dual-write**: Notion とローカルへ**独立に**書き込み、片系統が失敗してももう片系統は必ず試みる。**どちらか一方にでも永続化できればその取得単位は成功扱い**（`JobContext.persist`/`persist_lifecycle`/`persist_mark_absent`/`upload_raw` がこのプロトコルを実装）。設計上の正本は Notion だが、ローカル API の可用性のため対称化した。失敗は隠さず `ctx.notion_failed`/`ctx.mirror_failed` に計上し warning 記録（§3-2）、**両系統とも失敗した取得単位のみ** 呼び出し側が `ctx.failed`（⑦ failed）に数える。①relation 解決（Notion クエリ）失敗は relation 無しで本体を書く degrade、原本 `⑤` のみ両系統失敗でその取得単位を中止（原本ゼロ＝トレーサビリティ喪失 §3-3/§8.1-4）。`①` の状態/上場日/上場廃止日は Notion と対称に二重所有を回避（codelist 同期は `listed` のみ、状態確定は一次開示）。完全置換（`ON CONFLICT DO UPDATE` で `EXCLUDED` 上書き）で前回値を残さない（§3-1）。値はプレースホルダ渡しで SQL インジェクションを避ける。
- **②の時系列化**: ローカルは `(code, data_date)` を主キーに時系列を蓄積（Notion は最新スナップショット）。API の `/prices/{code}?from=&to=` で期間取得できる。
- **テーブル**: ①〜⑤⑦ に対応。`②`（personal-only）はローカル自己利用に限定し、公開 API として外部提供する場合は `commercial-ok`/`factual-cite`（①③④）のみをフィルタする。
- **起動**: `uvicorn jp_stock_pipeline.local_store.api:app`（`/docs` に OpenAPI。`/health` のみ認証不要）。

---

## 8. アーキテクチャとパイプライン

```
GitHub Actions (cron)
  collectors/ → data/raw/ → [必須] 原本+変換版を⑤へUL → transform/ → loaders/ → Notion 7DB
```

### 8.1 共通フロー（全コレクター共通の契約）

1. **Fetch**: ソース取得（リトライ3回・指数バックオフ）
2. **Save raw**: 無加工でローカル保存、SHA256計算（既存SHA256と一致なら重複スキップ）
3. **Convert**: §5.2の規則で変換版生成（値の変更禁止。失敗しても続行し状態記録）
4. **Upload**: ⑤へ原本+変換版をアップロード+メタ行作成（Notion ⑤ とローカル ⑤ へ独立に保存）。**両系統とも保存に失敗した取得単位のみ構造化を書かず中止**（原本ゼロ＝§3-3 トレーサビリティ喪失。片系統にでも原本が残れば構造化は書く。詳細 §7.1）
5. **Transform**: パース・指標計算・正規化（銘柄コードがキー）
6. **Upsert**: キー検索→update/create（冪等。キー=銘柄コード/docID/SHA256）。ライセンスタグ・来歴を必ず設定
7. **Log**: ⑦へ記録

### 8.2 スケジュール（JST）

| ジョブ | 頻度 | 内容 | トラック |
|---|---|---|---|
| `master_sync` | 月1 | EDINETコードリスト→①upsert | A |
| `prices_daily` | 毎営業日19:30 | yfinance一括→テクニカル計算→②upsert。stooqフォールバック。原本=一括取得単位のCSV/JSON+Parquet変換版 | B |
| `tdnet_hourly` | 平日9-19時毎時 | やのしん当日分→④+原本。短信XBRL検出時は③へ反映 | A |
| `edinet_daily` | 毎営業日21:00 | 当日書類一覧→XBRL/CSV/PDF取得→③④⑤ | A |
| `reconcile_weekly` | 週1土曜 | 第2ソース(stooq)による②終値の突合検証（§3-5）。③財務はEDINET/TDnetが担う | B |
| `export_weekly` | 週1日曜 | 全履歴Parquet/CSVを⑥へ更新 | A/B別ファイル |

### 8.3 レート試算（検証済み）

- Notion: 全銘柄日次更新(②upsert=銘柄ごと検索+更新2req)≒7,800req → 2.5req/sで約52分/日。原本は一括取得単位のため数十req
- stooq(reconcile_weekly): 銘柄ごとに1コール（=1取得単位）。件数が多い場合は `--limit` で段階実行（週1・低頻度）
- yfinance: `yf.download`一括+100銘柄ごと2〜5秒スリープ、429時60秒待機
- GitHub Actions: fetchはmatrix並列可、Notion書き込みは直列キュー（レート制限はトークン単位）

---

## 9. リポジトリ設計

```
jp-stock-data-pipeline/
├── flake.nix              # nix開発環境(Python3.12+uv) ※必須
├── pyproject.toml         # requests, pandas, pyarrow, yfinance, notion-client, lxml, openpyxl, pypdfium2, tenacity
├── .github/workflows/     # prices_daily / tdnet_hourly / edinet_daily / reconcile_weekly / master_sync / export_weekly
├── src/jp_stock_pipeline/
│   ├── config.py          # NOTION_TOKEN, EDINET_API_KEY, DB IDs
│   ├── licensing.py       # ライセンスタグ定義・継承ルール(§2.2)・公開フィルタ
│   ├── collectors/        # edinet / tdnet_yanoshin(+tdnet_official_fallback) / yfinance_prices / stooq_prices / edinet_codelist
│   ├── convert/           # xbrl_to_csv / json_to_parquet / pdf_to_text / xls_to_csv(値不変・§5.2)
│   ├── transform/         # technicals.py / normalize.py / reconcile.py(突合検証§3-5)
│   ├── notion/            # client.py(2.5req/sスロットル) / schema.py(7DB+ビュー冪等セットアップ) / upsert.py / file_upload.py(20MB/マルチパート)
│   └── jobs/              # スケジュール単位エントリポイント(全てdry-run対応)
└── tests/                 # 実レスポンスフィクスチャでユニットテスト
```

- nix: `flake.nix`でPython3.12+uvのdevShell（aarch64-darwin / x86_64-linux）。CIは`DeterminateSystems/nix-installer-action`+`nix develop -c uv run ...`
- GitHub Secrets: `NOTION_TOKEN` / `EDINET_API_KEY` / 各DB ID

---

## 10. 実装フェーズ

| フェーズ | 内容 | 完了条件 |
|---|---|---|
| **P0** | 雛形+flake.nix+Notion 7DB・ビュー・データカタログ自動作成（冪等、DB ID出力）+licensing.py | 「株式情報」配下に全DB生成 |
| **P1** | 銘柄マスタ同期（EDINETコードリスト→①、原本+CSV変換版→⑤） | 全上場銘柄が①に存在 |
| **P2** | 株価+テクニカル（yfinance→②、stooqフォールバック、原本+Parquet→⑤、週次エクスポート→⑥） | 営業日翌朝に②が全銘柄最新化 |
| **P3** | 開示+財務（TDnet毎時→④、公式ページパースフォールバック、EDINET日次→③④、短信XBRL→③、stooq突合） | 開示当日中に④、決算が③へ反映 |
| **P4** | 需給・指数・為替、エラー通知、公開準備（commercial-okフィルタビュー検証） | 週次需給反映+通知動作 |

各フェーズで必須: ユニットテスト（実レスポンスフィクスチャ）、dry-runモード、⑦への記録。

---

## 11. リスクと対応

| リスク | 対応 |
|---|---|
| yfinance仕様変更・BAN | stooq実データフォールバック（突合検証の第2ソースも兼ねる §3-5）。**ダミー埋めは絶対にしない**（§3）。恒久対策は商用株価ベンダー導入 |
| やのしんAPI停止（個人運営） | 公式TDnetページ直接パースのフォールバック実装（P3で同時実装、後回しにしない） |
| 商用化時のライセンス違反 | ライセンスタグ+公開フィルタで構造的に防止（§2.2）。商用ローンチ前チェックリスト実行 |
| 開示DB行数肥大（年数万件） | 年次アーカイブDBへの移動ジョブ |
| Notionレート制限変更 | client.pyのスロットル値を設定化 |
| 投資判断への利用 | データカタログと公開ページに「情報提供のみ・投資助言ではない」免責を明記 |

---

## 12. Claude Codeへの実装指示（本ファイルとセットで読むこと）

日本株データ収集パイプライン `jp-stock-data-pipeline` を本設計書に厳密に従って実装すること。

**前提**: 開発環境はnix（flake.nix+Python3.12+uv、必須）。実行はGitHub Actions。Notion親ページ=`205d74ff84cd809e92b4dd1adc5cf186`。Notionは有料プラン。

**不変条件（絶対遵守）**:
1. 取得単位ごとに原本（+可能なら変換版）を原本ファイルDB（Notion ⑤/ローカル ⑤）へアップロード。全系統とも保存できなかった取得単位のみ構造化を書かない（片系統に残れば書く §7.1）
2. **ダミー・推定・補間データの生成禁止**。フォールバックは実在ソースの実データのみ。欠損は欠損として記録（§3全項目）
3. 全行にソース・ライセンスタグ・データ基準日・取得日時・原本リレーションを設定（Notion 単独 UL 失敗時は relation 空で degrade しうる §7.1）。personal-onlyの継承ルール実装（§2.2）
4. Notion APIは2.5req/sスロットル、429は指数バックオフ
5. 全upsertは冪等（キー=銘柄コード/docID/SHA256）。全ジョブはジョブログDBに記録
6. 変換レイヤーは値を一切変更しない（§5.2）

**実装順序**: P0→P1→P2→P3→P4（§10）。各フェーズでユニットテスト（実APIレスポンスのフィクスチャ）とdry-runモードを実装。

**主要エンドポイント**:
- EDINET: `GET https://api.edinet-fsa.go.jp/api/v2/documents.json?date=YYYY-MM-DD&type=2&Subscription-Key=KEY` / `GET .../documents/{docID}?type={1|2|5}`（1=XBRL,2=PDF,5=CSV）
- EDINETコードリスト: EDINETサイトの`EdinetcodeDlInfo.zip`（CSV同梱）
- TDnet: `GET https://webapi.yanoshin.jp/webapi/tdnet/list/{YYYYmmdd|recent|code}.json?limit=N`（レスポンスは`items[].Tdnet`配下に`company_code`(5桁)/`pubdate`/`title`/`document_url`等。動作確認済み）+ 公式`www.release.tdnet.info`日付ページパースのフォールバック
- yfinance: `7203.T`形式（**personal-only厳守**）/ stooq: `https://stooq.com/q/d/l/?s={code}.jp&i=d`（②終値の突合にも使用 §3-5）
- Notion File Upload: `POST /v1/file_uploads`→send→attach（20MB超はマルチパート）

---

## 13. 参考情報源

- EDINET利用規約（公共データ利用規約準拠・商用可）: https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/WZEK0030.html
- EDINET API関連資料: https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/WZEK0110.html
- J-Quants利用目的・データ利用（私的利用限定）: https://jpx-jquants.com/ja/help/usage / 規約: https://jpx-jquants.com/termsofservice
- JPXサイト利用上の注意（商用データ収集不可）: https://www.jpx.co.jp/term-of-use/
- Yahoo Developer API Terms（商用・収益化禁止）: https://legal.yahoo.com/us/en/yahoo/terms/product-atos/apiforydn/index.html
- TDnet WEB-API（やのしん）: https://webapi.yanoshin.jp/tdnet/
- JPX TDnet API（有料・商用移行先候補）: https://www.jpx.co.jp/markets/paid-info-listing/tdnet/02.html
- Notion File Upload API: https://developers.notion.com/docs/sending-larger-files
