# リファクタ台帳（2026-09-13〜14 調査、公開版）

`docs/HANDOFF-2026-09.md` の §5 が参照する台帳の全文。9 本の調査を統合した候補 68 件（L-01〜L-68）、決着した矛盾、不変条件 A〜J を含む。
実在の銘柄コードと区分の対応・ローカルパス・別リポジトリ名は伏せてある（原本は運用者の非公開手順書）。
**2026-09-14 09:30 JST の決定（移行計画中止・Notion ②⑥⑦廃止・公開面の変更許容・ローカル用途 4 件は残す・prices_daily 廃止）はこの台帳より後**なので、
各件の最終的な扱いは `HANDOFF-2026-09.md` §5 の「状態」列を正とする（例: L-01 / L-11 / L-14 は残す、L-09 の本番適用は完了済み、L-18 / L-24 は不要）。
USER_DECISIONS 節の 22 件も同じ理由で、§3 の決定と §8.2 で上書きされている。

---

# SUMMARY
9 本の調査（stockStock: 死んだコード / 古い文書・コメント / コスト性能 / テスト、kabulab-cf: 死んだコード / 古い文書・コメント / D1 構造 / Actions・配信性能、両リポ: 不変条件）を 66 件の台帳に統合した。同一対象は 1 件にまとめ、source_scouts に出典 ID を残した（例: local_store 廃止は C01+CP-07+T-01+DOC-07）。

決着の要点: (1) local_store / stooq+reconcile_weekly / price_history / Neon 遺物 / drizzle 旧 snapshot 26,822 行 は全レーンが一致し P1。(2) kabulab-cf の日次 sync が swing_daily_ohlcv を 3 回全走査（≈1M rows_read/日）し、stockStock 側は G-core-5 孤児検査が ≈95 万行/日 —— この 2 件が D1 読取の最大消費者で、いずれも DDL 無しで消せる（L-17, L-40）。(3) VWAP 系の 3 候補（母集団を core_stocks に揃える / intra を月別シャードに / 日足二重取得の統合）は、不変条件レーンが見つけた外部読者（別リポジトリ（動画制作用） の intra/{code}.json 直読、新高値ブレイク検証の daily/<日経225 連動 ETF>.json ETF）と正面衝突するため P3・high に降格した。(4) core_stocks_migrate の --state-dump/--compare-to は CF-CANONICAL:3276 が P4b の適用直前に使うと明記しているので、削るのは --apply/--sql-dump だけに絞った。(5) ops_check は「何も書かない」が不変条件（ops_check.py:3）なので jss_job_runs の prune は runner 側に置く。

ランニングコストへの影響: すべての候補で $ 増は無い（Actions は PUBLIC で無料、D1 は Paid 含み枠内）。減るのは D1 rows_read（月 −2,000 万超）、Notion 要求（−5〜7 千/日）、R2 増分（3.6→0.7 GB/月）、Actions 所要時間（月 −1,500〜2,500 分）。新 cron は 1 本も足さず、reconcile_weekly と export_weekly の 2 本が減る。

本番 D1 への読取合計（全レーン）: 約 40.5 万行（コスト性能 145,100 + D1 構造 239,651 + kabulab 死コード 280 + 不変条件 229 + テスト 122 + 死コード 65 + kabulab 文書 19,734 + 性能 91）。書込 0。

# TOTALS
## 候補数（68 件）
- リポジトリ別: stockStock 37 / kabulab-cf 27 / both 4
- カテゴリ別: dead-code 14 / workflow 7 / dependency 2 / schema 7 / cost 4 / perf 11 / duplication 8 / stale-doc 6 / comment-noise 4 / test 5
- 優先度別: P1 27 / P2 25 / P3 16
- 本番 migration 要: 15 件（L-09 Notion/D1 データ、L-20 新表、L-23 R2 レイアウト、L-45/46/50/52/53/54/55 D1 DDL、L-51 新表、L-58 R2、L-64 DELETE）
- ユーザー判断要: 35 件（うち「確認のみ・推奨あり」が 12 件）

## 行数削減の見積もり（全候補実施時。判断待ち分を含む）
### stockStock ≈ −18,500 行
- 死んだコード・一度きりツール・依存: ≈ −11,000（L-01 2,900 / L-02 1,000 / L-03 560 / L-04+05 650 / L-06 739 / L-07 830 / L-08 280 / L-09 997 / L-10 1,384 / L-11 270 / L-12 220 / L-13 400 / L-14 143 / L-15 350 / L-25+26 460）。うち判断依存 ≈ −3,800（L-05/06/07/11/14/15/26）
- 文書: ≈ −4,250（docs 5,081→約 830、README 159→50）
- コメント: ≈ −2,100（src −1,900〜−2,300、tests −150）
- テスト内だけで閉じる分: ≈ −360（L-34 280 / L-35 80）
- 追加: ≈ +600（性能改修 L-16〜L-24 の +、契約テスト L-36）
- 依存 −5 パッケージ（psycopg, fastapi, uvicorn, openpyxl, xlrd）、cron −2 本（reconcile_weekly, export_weekly）、手動 workflow −2 本（yutai_backup, cloud_check）

### kabulab-cf ≈ −37,000 行（うち drizzle 旧 snapshot 26,822）
- 死んだコード・Neon 遺物: ≈ −31,600（L-38 1,100 / L-39 1,330 / L-40 26,822 / L-41 1,900 / L-42 300 / L-43 55 / L-44 70）
- 重複統合: ≈ −1,300（L-60 260 / L-61 500〜1,000 / L-52 100 / L-53 45）
- 文書: ≈ −3,000（L-65 900 / L-66 1,000 / L-67 1,100〜1,500）
- コメント: ≈ −1,100（L-68）
- 追加: ≈ +450（L-47〜L-57 の性能改修）
- 依存 −4 パッケージ（@neondatabase/serverless, vercel, @hono/node-server, prettier）、node_modules −18 MB、Worker バンドル −480 KB raw（L-59 実施時）

## ランニングコスト・性能の合計効果（$ は不変、含み枠と時間のみ）
- D1 rows_read: −約 2,000 万/月（kabulab-cf 日次 sync、L-47）−約 2,000 万/月（stockStock G-core-5、L-16）−1.4M/月（freshness、L-17）+ 公開面 1 表示あたり −1.5 万〜−8 万（L-48/50/51）
- D1 rows_written: −約 35 万/月（L-49/45/46/52/53）
- Notion API: −5,000〜−7,000 req/日（L-03/19/20/21/24/25、② を残す前提）
- R2: 増分 3.6 → 約 0.7 GB/月（L-22 + L-15）、Class A/B 各 −91k/月（L-23 実施時）
- GitHub Actions（PUBLIC で $0）: 月 −1,500〜−2,500 分（prices_daily −900〜−1,700、master_sync −43、export −95、tdnet −230〜−700、CI −140、kabulab 日次 −250〜−400）

## 本調査で消費した本番 D1 rows_read
約 40.5 万行（全レーン合計、担当ごとは 30 万以内）。rows_written 0。

# USER_DECISIONS
ユーザーの判断が要る事項（選択肢と推奨）。番号は依存する台帳 ID。

1. [L-06/L-08/L-30] P4b（+733 行）・P5（③断面 writer 交代）・P6〜P8 の移行計画を続けるか。選択肢: (a) 続ける → fin_parity / --state-dump / --compare-to / build_column_update を残し docstring だけ圧縮 (b) やめる → 約 1,000 行を追加削除し CF-CANONICAL を「現在形」だけに。推奨: TARGET-ARCHITECTURE:387（Yahoo/JPX writer は TS に残す）と整合する (b)。ただし P4b の「9 銘柄を戻すか」は別途決める。

2. [L-07] universe_guards.py（①母集団 writer の Python 移植・呼び出し 0）を削除するか。推奨: 削除（定数 4 つは契約 JSON へ移す）。1 で (a) を選ぶ場合も P4b は kabulab-cf 側 writer のままなので削除で問題ない。

3. [L-33] JPX 信用残 writer を 2026-09-28 に stockStock（Python）へ切り替えるか、TS に残して Python 実装（約 800 行）を消すか。推奨: TARGET-ARCHITECTURE:387 どおり TS に残し Python 側を削除（cron 総数も増えない）。

4. [L-05] yutai_backup を「1 行直して一度流し、⑨ の LLM 派生値を R2 に退避してから削除」するか「退避せず削除」か。推奨: 退避してから削除（CF-CANONICAL:3634 の推奨どおり、再取得不能資産）。

5. [L-09] migrate_halfyear_labels の --apply をいつ本番へ流すか（Notion ③④ 約 17,000 update）。推奨: 早期（流すまで新旧ラベルが混在し続ける）。

6. [L-15/L-25/L-26] Notion ②⑥⑦ を人が見ているか。推奨: ⑦（jss_job_runs と完全重複）と ⑥（毎週失敗・配布不能）は廃止。② は「見ていない」なら廃止（prices_daily −40 分/run）だが writer 設計と絡むので 1 の後に。

7. [L-22] ⑤「原本添付必須」を 20 MB 超だけ R2 キー参照に緩めてよいか。推奨: 緩める（R2 が唯一伸びている課金軸）。

8. [L-24] yfinance 404 が固定の 101 銘柄を母集団から外してよいか。推奨: 外す（欠損は件数で ⑦/jss_job_runs に残す）。

9. [L-01/L-11/L-14] 端末B の PostgreSQL、kabuMCP キャッシュ引渡し、cloud_check を手動で使っているか。推奨: いずれも削除（本番影響ゼロ）。

10. [L-29/L-31/L-32] docs を SPEC / TARGET / DECISIONS の 3 本に再編し § ID を維持してよいか。AGENTS.md の「README に更新記録を残す」ルールを git log に一本化してよいか。slo.py 祝日 3 案のどれを採るか。推奨: 3 本構成・§ ID 維持・git log 一本化。祝日は「緑を 1 営業日緩める」（実装最小・新依存なし）。

11. [L-28] EDINET 由来フィクスチャ 5 件を commit してよいか。technicals 数式テストに合成 OHLCV を許すか。推奨: EDINET は commit（commercial-ok）。合成 OHLCV は §3-6 との整合をユーザーが決める。

12. [L-58] VWAP の母集団（stocks.json 4,445 件・全種別）から ETF/REIT/PRO を外してよいか。外部読者 2 リポ（別リポジトリ（動画制作用） の intra 直読、新高値ブレイク検証の daily/<日経225 連動 ETF>.json）の改修を含む。推奨: 今回のリファクタでは触らず、P6 の設計として別途決める。

13. [L-54] otakara_stock_financials 廃止で読取が 1 表示 +1,600 増える点を「二重の真実の解消」より優先するか。推奨: 見送り（増やさない制約）。

14. [L-55] swing_daily_ohlcv を R2 に置き換え、capm/bs/low-vol の日足が最大 2 営業日古くなることを受け入れるか。推奨: L-47（走査 3 本の削除）で十分効くので見送り。

15. [L-56] D1 書込のまとめ方: 案A（multi-row upsert、DDL なし）/ 案B（Worker に書込ルート）。推奨: 案A。

16. [L-57] stock-sync の「1 件でも失敗なら run 失敗」を維持するか閾値付き警告にするか。推奨: 回収を時間予算制にした上で、失敗率 ≤1% は成功扱い + Issue コメント。

17. [L-43/L-62] rsi の JSON API 2 本（personal-only 派生値を JSON 公開）を撤去してよいか。SSR に public キャッシュを付けてよいか。推奨: API 撤去。キャッシュは EDINET 由来のみのページに限定。

18. [L-59/L-61] zod/mini への書き換え（20 ファイル）、CSS 統合と Noto Sans JP 外しの見た目変更を許容するか。推奨: 許容（バンドル −1/3、外部 CSS −459 KB/ページ）。

19. [L-65/L-67] ADR-0001 を 5 行要約にして削除するか。サービス別 README.md を CLAUDE.md に統合するか。CLAUDE.md ルール4（不在の .claude/agents）・ルール5（push 必須 vs PR 運用）をどう書き直すか。推奨: ADR 削除・README 統合・ルール4 削除・ルール5 は「PR を作る」に変更。

20. [L-64/L-44] swing_sector_daily の 2026-09-14 未満の行を DELETE してよいか。旧 URL 互換シムと Notion 旧フラット DB 退避コードを消してよいか。推奨: 月曜 cron 実行後に DELETE、シムは削除（Notion 側は目視確認後）。

21. [L-39] Neon 解約は完了しているか（README:178-188）。prettier をエディタで使っているか。推奨: 完了なら節ごと削除、prettier は痕跡なしのため削除。

22. [L-68] active-equity.ts の「承認待ち」2 点（instrument_type を述語に使う承認、公開面に ETF/REIT を出さない）への回答。推奨: 両方承認し stockStock 側の地図に記録。

# CONTRADICTIONS
1. **core_stocks_migrate の --state-dump / --compare-to**: 死んだコードレーン C08 は「--verify だけ残す」、テストレーン T-05 と不変条件 K1 は P4b で再利用と主張。決着: CF-CANONICAL-DESIGN.md:3276-3277 を読み「--state-dump は P4b 適用直前に取る」「--compare-to は P4b 用に G-core-2 の別モードを足す」と明記されているため、削るのは --apply / --sql-dump に限定（L-08）。P4b 中止なら全部削除。

2. **jss_job_runs の prune 場所**: コスト性能 CP-16 は ops_check に DELETE を足す提案、不変条件 E3 は「ops_check.py は何も書かない」。決着: ops_check.py:3 の module docstring で確認。DELETE は runner の record_job_run 側に置く（L-17）。

3. **freshness の COUNT(*) → EXISTS**: CP-15 の提案は test_ops_slo.py:267 の `assert "COUNT(" in upper` と衝突。決着: テストを EXISTS 許容に直した上で採用（L-17）。「0 行は赤」の判定は EXISTS でも保てる。

4. **datasets.py の prices_daily writer**: CP-13 が「stockStock prices_daily と書かれているが実 writer は kabulab-cf daily.ts」と指摘、不変条件 E2 は test_governance が writer と claim を突合すると主張。決着: datasets.py:105-112 を読み writer="prices_daily" / db=DB_KABULAB / 実表 core_stock_financials を確認。test_governance.py:386-396 は core_stocks / yutai_benefits の 2 件しか検査しないため矛盾が残っていた。CP-13 が正（L-27）。

5. **swing_daily_ohlcv の走査削減の方法**: D1-01（Phase 6 の D1 読み直しをやめて Phase 3 のメモリから投影）と KCF-PERF-03（prune を Phase 6 の走査に統合）は両立しない。決着: D1-01 を採る（両走査が消え、KCF-PERF-03 は片方が残る）。不変条件レーンの「rebuildMomentumProjection 案 A 確定・再提案しない」は両リポの docs/src を grep しても該当文言が無く出典不明（未確認）。D1-01 は投影の存在も列も変えないので projection-schema.ts の撤去条件には触れない。

6. **VWAP 母集団と R2 契約**: kabulab 死コード KC-19 と性能 KCF-VWAP-06/07/08 は stocks.json → core_stocks 普通株、intra の月別シャード、日足二重取得の統合を提案。不変条件 G1/G3/G4 は daily/ の母集団が全種別（日経225 連動 ETF 1 本 を外部読者が読む）、intra/{code}.json は外部読者が直読、「1 バイトも変えない」と主張。決着: 別リポジトリ（動画制作用）の intraday.py と 別リポジトリ（ブレイク検証）の README の当該 ETF への参照を grep で確認し、不変条件側が正。3 候補は 1 件（L-58）に畳み P3・high・「今回は不実施」とした。

7. **yutai_backup の削除 vs test_yutai_backup_is_manual_only**: テストレーンは同テストを「新 cron 禁止の実体」として must_keep、死コードレーン C05 は job ごと削除。決着: test_jobs_smoke.py:703-711 の TestWorkflowCrons.EXPECTED が有効 cron 全 7 件を等式で固定しており、こちらが「新 cron 禁止」の実体。job 削除で当該テストが消えても不変条件は保たれる（L-05）。

8. **local_store 削除のユーザー判断の要否**: C01 は「不要（TARGET-ARCHITECTURE で決定済み）」、CP-07/T-01/DOC-07 は「端末B を今も使うか」の判断が要る。決着: secret 不在・workflow 配線なしで本番影響はゼロなので「確認のみ」に降格し P1 のまま（L-01）。

9. **cron 本数**: 依頼文「cron 9 本 + 手動 3 本」に対し不変条件レーンは「schedule 8 本」。決着: .github/workflows を ls し 12 ファイル、schedule を持つのは 8（margin_weekly.yml:21 はコメントアウト）。依頼文は margin_weekly の無効 cron を数えている。

10. **otakara_stock_financials 廃止（D1-11）と「rows_read を増やさない」制約**: D1 構造レーン自身が +1,600/表示を申告。決着: 両論を残し P3・needs_user_decision（L-54）。

11. **Workers プランの記述**: wrangler.toml「無料プランで運用」vs TARGET-ARCHITECTURE §9.1「Workers Paid $5 確定」。決着: どのレーンも wrangler whoami が権限不足で再確認できず。§9.1 の実測記録（D1 16 個・Time Travel 使用可）を採り、コメント側を直す（L-65）。ユーザーにダッシュボードでの再確認を依頼（未確認）。

12. **kabulab-cf 死コードレーンと文書レーンの ID 衝突**: 両レーンが KC-xx を別内容に使っていた（KC-02: drizzle 遺物 vs public-columns コメント等）。本台帳では全件を L-xx に振り直し、source_scouts に「(docs lane)」を付けて区別した。

13. **日次 cron の rows_read 推定の食い違い**: D1 構造レーン ≈1.0M/run（月 ≈2,100 万）vs TARGET §9.1「月 約 900 万」。決着不能（§9.1 が p_momentum 追加前の値かは未確認）。L-47 の効果は EXPLAIN と既知行数 336,169 からの推定値として記載。

# LEDGER

## L-01 [P1/medium/stockStock/dead-code] local_store（PostgreSQL dual-write + FastAPI）と deploy/ を丸ごと削除し runner を Notion+Cloudflare の 2 系統に縮める
- action: src/jp_stock_pipeline/local_store/ (1,172 行)、deploy/ (273 行)、runner.py の _mirror/mirror*/persist*/connect_local_store と --db-target、config.LocalStoreSettings/DB_TARGET_*、pyproject の psycopg/fastapi/uvicorn と filterwarnings、tests/test_local_store.py (1,037)・test_local_api.py (99)、test_ops_slo/test_license_map の connect_local_store パッチ、README §ローカルAPI L43-96、DESIGN §7.1、.env.example LOCAL_* を削除。`nix develop -c uv lock` で uv.lock 再生成。migrate_halfyear_labels の target=local は L-09 と同時。
- evidence: gh secret list 14 個に LOCAL_DB_*/LOCAL_API_KEY 無し。workflow に LOCAL_DB/tailscale 0 行（#33 018e9b7 で除去）。runner.py:325 connect_local_store は host 未設定で None。TARGET-ARCHITECTURE:160「secret 不在で既に死んでいる」。deploy/ 最終コミット 2026-06-28。
- loc_delta: 約 −2,900（src −1,172、deploy −273、runner/config −240、tests −1,136、docs −100）+ uv.lock −11 パッケージ | cost: $±0。CI の uv sync が軽くなる（fastapi/pydantic/uvicorn 系 wheel 不要）
- prod_migration: False | user_decision: 端末B の PG+FastAPI を手動でも使っていないことの確認のみ（本番影響ゼロ。推奨: 削除）
- depends_on: L-09 と同 PR か先行 | scouts: C01, CP-07, T-01, DOC-07, INV open_q 9

## L-02 [P1/low/stockStock/workflow] stooq コレクター・prices_daily の stooq フォールバック・reconcile_weekly（週次 cron）・transform/reconcile.py を削除
- action: collectors/stooq_prices.py (161)、jobs/reconcile_weekly.py (208)、transform/reconcile.py (153)、.github/workflows/reconcile_weekly.yml (65)、config.stooq_enabled、prices_daily.py:147-157 の分岐（yfinance 失敗→add_failure に直結）、capture_prices.py の stooq 部、tests/test_stooq_prices.py (179)・test_reconcile.py (158)・test_jobs_smoke TestReconcileWeekly (:628-697)・stooq_fail 分岐 (:298-330)、fixtures/prices/stooq_challenge_response.html と test_fixture_policy ALLOWED の行、notion/schema.py:617-618,723,754 のカタログ文を削除。TestWorkflowCrons.EXPECTED から reconcile_weekly を外す。models.Source.STOOQ と licensing.py:36 は残す（2026-06 期の Notion 行が値を持ちうる）。DataQuality.NEEDS_REVIEW は残す。
- evidence: config.py:191-194「stooq は PoW を返し CSV を返さない＝実質死亡」、既定 False、STOOQ_ENABLED は secret/variable に無し。reconcile_weekly.py:148-152 で即 return、gh run list 08-22〜09-12 毎週 success（proc=0）。TARGET-ARCHITECTURE:431「第2ソース突合は目標に置かない」。
- loc_delta: 約 −1,000（src −536、workflow −65、tests −420） | cost: cron −1 本、GH Actions −9 分/月、Notion ⑦ −4 行/月。$±0
- prod_migration: False | user_decision: 
- depends_on:  | scouts: C02, C03, CP-06, T-02

## L-03 [P1/low/stockStock/dead-code] Notion ①配下の株価履歴子DB（price_history / --enable-history）を削除し、prices_daily の ① 二重スキャンを止める
- action: notion/price_history.py (289)、schema.py の MASTER_PROP_HISTORY_*/HISTORY_*/history_*_schema、prices_daily.py:176-192,255-280 の分岐、tests/test_price_history.py (183) と test_notion_create_race/test_jobs_smoke の該当 3 件を削除。prices_daily.py:189 の load_stock_master_state は resolve_codes の ① 結果から master_map を作る形に置換（upsert.load_stock_master_map）。schema.master_schema から 3 列定義を外す（本番 ① のプロパティは消さない）。
- evidence: prices_daily.py:176-183 の廃止根拠（R2 daily/ と重複、Notion 日次要求の 71%、8,000 行シャードは 32 年後まで不発）。842a9208 #17 で既定オフ、prices_daily.yml に --enable-history 無し。run 34610776800 の query 118 のうち ① state 39 req は history OFF でも発行。
- loc_delta: 約 −560（src −350、tests −210） | cost: Notion −39 req/日、−20 s/run。$±0
- prod_migration: False | user_decision: 2026-09-07〜11 に作られた ① 配下の履歴子DB を Notion 上でアーカイブするか（放置で無害）
- depends_on:  | scouts: C04, CP-10

## L-04 [P1/low/stockStock/dead-code] KABULAB_D1_DATABASE_ID（2 DB 前提のフォールバック）を config/datasets/3 job/3 workflow から削除
- action: config.kabulab_d1_database_id、datasets.DB_KABULAB と db フィールド、freshness_probe.py:111-120 / master_sync.py:124-133 / core_stocks_migrate.py:68 の `or d1_database_id`、master_sync.yml:50・ops_check.yml:85-98,135-149・yutai_backup.yml の env 行、test_ops_slo.py:306-309 の legacy 集合テストを削除。
- evidence: gh secret list に KABULAB_D1_DATABASE_ID 無し。PRAGMA table_info(core_stocks)=21 列を CF_D1_DATABASE_ID で確認、ops_check は同 DB で日次 success → 全表が 1 DB。worker/wrangler.*.jsonc も database_name kabulab-cf 1 個。
- loc_delta: 約 −60 | cost: $±0
- prod_migration: False | user_decision: 
- depends_on: L-05 の分岐を先に決める（yutai_backup.py:36-41 が同フィールドで no-op する） | scouts: C05(1)

## L-05 [P2/medium/stockStock/dead-code] yutai_backup（実行履歴 0・現状 no-op）を「一度流してから削除」か「即削除」に分岐
- action: 退避する場合: yutai_backup.py:36-41 を settings.d1_database_id 読みに 1 行直し、`--allow-shrink` で 1 回 dispatch（R2 backup/yutai/ に写す）→ 記録後に jobs/yutai_backup.py (148)・cloud_store/yutai.py (202)・yutai_backup.yml (85)・tests/test_cloud_yutai.py (138)・test_jobs_smoke:742-757 を削除。退避しない場合は即削除。test_yutai_backup_is_manual_only は消えるが「新 cron 禁止」は TestWorkflowCrons.EXPECTED が引き続き固定する。
- evidence: gh run list yutai_backup 0 件、jss_job_runs に行なし。CF-CANONICAL:3634「一度も実行されていないので R2 に写しは無い」。9 銘柄削除後の写しはローカル JSONL のみ。
- loc_delta: 約 −590 | cost: $±0。退避時に yutai_benefits 8,295 行を 1 回走査
- prod_migration: False | user_decision: ⑨優待の LLM 派生値（要約・推定額）を R2 へ退避してから消すか、退避せず消すか
- depends_on:  | scouts: C05(2), T must_keep, INV G5

## L-06 [P2/low/stockStock/dead-code] cloud_store/fin_parity.py（P5 判定 G-fin-1）: P5 を行わないなら削除、行うなら job に接続し実測記述を圧縮
- action: P5 中止: fin_parity.py (485) と tests/test_fin_parity.py (254) を削除、CONTRACTS.md G-fin-1 節を 1 行に。P5 継続: evaluate_parity を core_stocks_migrate --verify 相当の job から呼ぶ形に接続し、module docstring L1-86 の実測表・仮説を削って a/b/c/d 判定と「閾値なし=観測のみ」15 行に。いずれでも TestOldDefinitionIsUnreachable の実測定数固定 4 件（:42-71）は削除。
- evidence: reach.py: jobs/scripts から import 0（自ファイルのみ）。TARGET-ARCHITECTURE:387 は Yahoo writer を TS に残す方針で P5 前提が揺らぐ。CF-CANONICAL:3177-3192 は閾値を暫定と明記。
- loc_delta: −739（削除時）/ −160（圧縮時） | cost: $±0
- prod_migration: False | user_decision: P5（③断面 writer を kabulab-cf daily.ts → stockStock へ交代）をまだ行うか
- depends_on:  | scouts: C06, T-16, CMT-04, INV K2

## L-07 [P2/medium/stockStock/duplication] cloud_store/universe_guards.py（kabulab-cf universe.ts の行番号同期移植・本番呼び出し 0）を削除するか、残すなら docstring を 25 行に圧縮
- action: 削除: universe_guards.py (361)、tests/test_universe_guards.py (469)、test_stock_code_contract.py:110-116 TestUniverseGuardsReExport を削除し、kabulab-cf instrument-type.ts:32-33,77 のコメント参照を「共有文字列 'equity'」に書き換える。閾値定数 4 つは契約 JSON へ移してから消す案も可。残す: L1-118 の docstring を規則表（4 条件の向き・分母・未充填縮退・equity⊆active）25 行に、移植元行番号併記は残す。
- evidence: grep -rln universe_guards → tests 2 ファイルのみ。docstring 自身が「stockStock 側にはまだ呼び出し元が無い」。governance.WRITER_CLAIMS は core_stocks/base=kabulab-cf を動かさないと宣言。TARGET-ARCHITECTURE:387-388「JPX は TypeScript」。
- loc_delta: −830（削除）/ −170（圧縮） | cost: $±0
- prod_migration: False | user_decision: ①銘柄マスタ writer を将来 stockStock へ移す（P4b 移管）計画を維持するか。維持しないなら削除
- depends_on:  | scouts: C07, INV-C3, CMT-02, T-08 open_q 8

## L-08 [P2/medium/stockStock/dead-code] core_stocks_migrate の --apply / --sql-dump（P4a 一度きり DDL 発行）を落とし、--verify / --snapshot / --state-dump / --compare-to は P4b 用に残す
- action: jobs/core_stocks_migrate.py の apply/sql-dump モードと cloud_store/core_stocks.py:177-260,357-418 のうち plan_ddl / normalize_sql の DDL 生成部を削除。unexpected_columns/unexpected_indexes（E7）は残す。tests/test_core_stocks_migrate.py の TestDdlShape(6)/TestAgainstRealSchema(5)/TestD1StoreUpsertIsNotUsable(3) と test_core_stocks_job.py TestApply(4)/TestAlreadyApplied(2) を「適用済みなら何も計画しない」1 件に縮小。AST テスト 2 件（build_column_update 呼び出し 0 / build_sector33_updates 呼び手）は conftest の callers_in_jobs(name) 1 本に共通化、build_column_update を本番未使用のまま P4b まで残すなら維持。
- evidence: 本番 PRAGMA table_info(core_stocks)=21 列（P4a 適用済み）、ops_check.yml:150 は --verify のみ。CF-CANONICAL:3276-3277「--state-dump は P4b 適用直前に取る」「--compare-to は P4b 用に G-core-2 の別モードを足す」→ 削除対象は --apply/--sql-dump に限定（死コードレーンの提案を修正）。
- loc_delta: 約 −280（src −130、tests −150） | cost: $±0（日次 --verify の rows_read は不変）
- prod_migration: False | user_decision: P4b（+733 行）を今後実施するか。実施しないなら --state-dump/--compare-to/build_column_update も削除（追加 −120 行）
- depends_on: L-04 | scouts: C08, T-05, T-06, INV K1

## L-09 [P2/medium/stockStock/schema] scripts/migrate_halfyear_labels.py を本番で --apply してから削除（適用まで ③/④ に旧ラベルと新ラベルが混在）
- action: ユーザー実行: `--target options --apply` → financials → disclosures → d1 の順で本番へ流す（Notion ③ 約 8,300 行 + ④ 約 8,600 行の update、D1 31 行 UPDATE）。適用結果を PR に記録後、スクリプト (681) と tests/test_migrate_halfyear_labels.py (316) を削除。upsert.py LEGACY_DISCLOSURE_TYPES は移行後も無害なので残置可。
- evidence: PR #51 本文「本番では --apply を実行していない」「select の選択肢に『中間』『半期報告』はまだ無い」。書き手 transform/normalize.interim_disclosure_type は既に新ラベルを書く。docs に実行記録なし（3 レーンとも未確認）。
- loc_delta: −997（適用後） | cost: 適用時に Notion API 約 17,000 update が一度だけ。以後 $±0
- prod_migration: True | user_decision: 移行をいつ流すか（流すまで削除不可）
- depends_on: L-01（target=local を先に外す） | scouts: C09, TST-02, T-03

## L-10 [P1/low/stockStock/dead-code] scripts/dedupe_notion_disclosures.py（④ 重複掃除の一度きりツール）とテストを削除
- action: scripts/dedupe_notion_disclosures.py (777) と tests/test_dedupe_notion_disclosures.py (607) を削除（git タグで場所を残す）。upsert.oldest_page / _oldest_page_ids は本番の書き手が使うので残す。
- evidence: PR #50 で追加、背景「Notion ④ の重複掃除は完了」。再発防止本体は upsert.py #46/#13 の収束ロジック（test_notion_create_race.py 692 行が固定）。バックアップはリポジトリ外。
- loc_delta: −1,384 | cost: $±0
- prod_migration: False | user_decision: ④ の掃除が完了し再実行予定が無いことの確認
- depends_on:  | scouts: C10, TST-02, T-03

## L-11 [P3/low/stockStock/dead-code] edinet_daily の kabuMCP キャッシュ引渡し（--kabumcp-cache-dir）を削除
- action: edinet_daily.py:54-120 の _copy_kabumcp_csv/_export_kabumcp_cache、argparse、呼び出し :285,379-383、tests/test_kabumcp_cache.py (176)、README L97-116 を削除。
- evidence: edinet_daily.yml に kabumcp 0 行。R2 jp-stock-raw に同じ原本が置かれるようになり役割が重なる。TARGET-ARCHITECTURE:717「kabuMCP の提供範囲は対象外」。
- loc_delta: 約 −270 | cost: $±0
- prod_migration: False | user_decision: kabuMCP へのローカル引渡しをまだ使うか
- depends_on:  | scouts: C11, T-13

## L-12 [P1/low/stockStock/dependency] convert/xls_to_csv.py と xlrd / openpyxl（本番経路で未使用）を削除
- action: xls_to_csv.py (48)、json_to_parquet.py:130-143,180-185 の xls 分岐、tests/test_convert_xls.py (134、全件 skip)、capture_convert_fixtures.py の data_j.xlsx 取得部を削除し、pyproject から openpyxl/xlrd を外して uv.lock 再生成。file_upload.py:64 の拡張子リストは残す。
- evidence: src/scripts に openpyxl/xlrd の直接 import 0。xls を作る job/collector 無し。JPX data_j.xlsx を読む本番実装は kabulab-cf TS 側（TARGET-ARCHITECTURE:606）。
- loc_delta: 約 −220 + 依存 2 個 | cost: $±0。uv sync 僅かに軽く
- prod_migration: False | user_decision: 
- depends_on:  | scouts: C12, T-12(iv)

## L-13 [P3/low/stockStock/dead-code] 参照ゼロ・テスト専用の小シンボル 13 個と RETIRED_TABLES（finmath 遷移コード）を削除
- action: upsert.load_edinet_code_map、supply._point、tdnet_yanoshin.KW_SPLIT/KW_CONSOLIDATION/parse_yutai_action/fetch_disclosure_pdf、licensing.COMMERCIALIZATION_CHECKLIST（DESIGN §2.2 へ文章として移す）、yfinance_prices.from_ticker、normalize.yf_info_to_valuation、core_stocks.render_sector33_backfill、financials._license_rank_sql、governance.CoverageReport.clean / schema.ReferenceDiff.clean、http.post_json と対応テスト（test_tdnet_yutai.py 全体ほか）を削除。governance.py:251-290 RETIRED_TABLES と :681-705 の分岐、tests/test_governance.py TestRetiredTables(5) を削除（本番に finmath_* 無し）。
- evidence: deadcode.py（word 境界）で src/scripts/workflows 参照 0。parse_yutai_action は tests/test_tdnet_yutai.py のみ。本番 sqlite_master に finmath_* 0 表（rows_read 122 で確認）。
- loc_delta: 約 −400（src −225、tests −175） | cost: $±0
- prod_migration: False | user_decision: parse_yutai_action（優待開示の下位区分）を将来 tdnet_hourly で使う予定が無いこと
- depends_on: L-01, L-02 | scouts: C13, T-04

## L-14 [P3/low/stockStock/workflow] cloud_check（手動疎通診断・2026-09-11 の 4 回のみ）を削除するか README に「初期設定時のみ」と明記
- action: 削除する場合 .github/workflows/cloud_check.yml (62) と jobs/cloud_check.py (81) を削除、README ジョブ表から外す。残す場合は役割を 1 行明記。
- evidence: gh run list: 2026-09-11 dispatch 4 回のみ。以後 D1 疎通は freshness_probe、R2 疎通は supply_daily/edinet_daily の日次書込が兼ねる。
- loc_delta: −143 | cost: $±0
- prod_migration: False | user_decision: 資格情報ローテーション時の診断として残したいか
- depends_on:  | scouts: C14

## L-15 [P2/medium/stockStock/workflow] export_weekly（③④ は毎週 10,000 件打切りで必ず失敗、⑥ は配布不能な personal-only）を止める
- action: export_weekly.yml (60) と jobs/export_weekly.py (:85-219) と upsert.create_export_row、対応テストを削除し、TestWorkflowCrons.EXPECTED から外す。③④ の全件エクスポートが要るなら D1 jss_financials / ir_disclosures から生成して R2 キーだけを書く形で別途設計（Notion 10,000 件制限を踏まない）。
- evidence: run 34735043686（2026-09-13, 31 分）: ③④ とも QueryTruncatedError、processed=1 failed=103。5y yfinance 291 MB を再取得し ⑤⑥ へ同じ 291 MB + 55 MB zip を 2 回 UL、R2 +360 MB/週。Notion 241 query のうち 200 は失敗する読み。
- loc_delta: 約 −350 | cost: cron −1 本、GH −95 分/月、Notion −1,400 req/月、R2 −1.4 GB/月（唯一伸びている課金軸）、Notion ストレージ −1.4 GB/月
- prod_migration: False | user_decision: ⑥ の Parquet/CSV を誰かが DL しているか（Notion で見ているか）
- depends_on:  | scouts: CP-02, C open_q 7

## L-16 [P1/low/stockStock/cost] G-core-5 孤児検査を FK 宣言のある 15 表で毎日回さない（stockStock 側 D1 読取の 95% を消す）
- action: core_stocks.py:380-433 と core_stocks_migrate._observe: 日次 --verify では SOFT_CHILD_TABLES（jss_financials）と FK の無い p_momentum（現在 ALL_CHECKED_TABLES に無い→追加）だけを数える。FK 表の検査は --compare-to 経路（移行時）か週 1 回に限定。_observe で PRAGMA foreign_keys を読み 0 なら全表検査に戻す安全弁を付ける。
- evidence: 本番 sqlite_master: stock_id を持つ 16 表中 15 表が REFERENCES core_stocks + NOT NULL、PRAGMA foreign_keys=1。chunk3 実測 rows_read 136,112（走査行 = 子表行×2）。swing_daily_ohlcv 336,170 行から chunk2 ≈ 70 万 → 日次 約 95 万行（推定）。2026-09-13 run で孤児 0 件。
- loc_delta: −10〜+15 | cost: D1 rows_read −約 95 万/日（月 −2,000 万）。$ 不変（Paid 含み枠内）
- prod_migration: False | user_decision: 
- depends_on:  | scouts: CP-01, INV E4

## L-17 [P2/low/stockStock/cost] freshness_probe の COUNT(*) 全走査（ir_disclosures 37,641 + yutai_benefits 8,295/日）を EXISTS に緩め、jss_job_runs の窓関数走査と行数増を定数化、license_map の不変参照行を差分時のみ書く
- action: (1) datasets.py:114-129,210-229 の大きい表は MAX(pubdate)+EXISTS にし、row_or_object_count の意味を「件数 or 存在」に緩める。test_ops_slo.py:267 の `"COUNT(" in upper` を EXISTS 許容に直す。(2) ops_check.py IDLE_RUNS_SQL に WHERE finished_at > 30 日を足す。90 日より古い jss_job_runs の DELETE は ops_check ではなく runner の record_job_run（既に書いている側）に置く（ops_check.py:3「何も書かない」を守る）。(3) license_map.py:180-200,311 は ReferenceDiff で照合し差分 0 なら upsert しない。
- evidence: EXPLAIN: ir_disclosures は SCAN USING COVERING INDEX（37,641）、yutai_benefits SCAN（8,295）= freshness 58k の 79%。IDLE_RUNS_SQL は SCAN jss_job_runs（+20 行/日で無制限）。license_map は値の変わらない 40 行を毎日 upsert。
- loc_delta: +35 | cost: D1 rows_read −約 46k/日（−1.4M/月）、走査を ≤600 行に固定、rows_written −40/日
- prod_migration: False | user_decision: 
- depends_on:  | scouts: CP-15, CP-16, D1-19, INV E1/E3

## L-18 [P2/medium/stockStock/perf] prices_daily の fetch_valuation（72 分 = 55%）を ② PATCH と並走させ get_info を ③ の EPS/BPS/DPS×終値の導出に置き換え、② PATCH を共有スロットルで 2〜3 並列に
- action: (a) valuation をワーカースレッドで先行し、到着した銘柄から順に ② upsert（キュー）。(b) PER/PBR/配当利回りは ③ 由来の導出、fast_info の market_cap だけ残す。(c) upsert_price_technical を ThreadPoolExecutor(2〜3) で投げ、ctx カウンタはメインスレッドで集計、429 で並列度 1 に落とす。
- evidence: run 34610776800: valuation 14:52:50→16:04:35（71.7 分、3,720×fast_info+get_info+0.5 s）、② PATCH 16:07:25→16:43:07（35.7 分、実効 1.74 req/s、スロットル 2.5 rps 未達）。両者は独立 I/O 待ちで直列。
- loc_delta: +70〜+110 | cost: prices_daily −45〜−80 分/run（月 −900〜−1,700 分）、yfinance −3,720 req/日。Notion 要求数は不変。$±0
- prod_migration: False | user_decision: 
- depends_on: L-03 | scouts: CP-03, CP-12

## L-19 [P1/low/stockStock/perf] master_sync が ① 全 3,841 行を毎月無条件に PATCH（43 分）→ 事前マップとの差分だけ書く
- action: upsert._master_map_from_pages で page ごとの properties も保持し、stock_master_properties(record, include_lifecycle=False) と比較して同値なら PATCH を省く。
- evidence: run 33569311162（2026-09-01）: ① マップ 40 query の後 23:08:17〜23:51:18 に PATCH 3,841 件。load_stock_master_map は全 properties を受け取りながら page_id しか使っていない。D1 sector33 差分は 0〜数文。
- loc_delta: +30〜+50 | cost: master_sync 47→約 4 分/月、Notion −3,800 req/月
- prod_migration: False | user_decision: 
- depends_on:  | scouts: CP-04

## L-20 [P2/medium/stockStock/perf] tdnet_hourly の ④ 毎時再 PATCH を同値 skip にし、① の code→page_id を D1 新表 jss_notion_pages に置いて 1 日 14 回の ① フルスキャン（546 req/日）を消す
- action: (1) load_disclosure_page_map の properties と disclosure_properties(record) を比較し同値なら skip。(2) 新表 jss_notion_pages(code, db, page_id, updated_at) を master_sync が書き、tdnet/edinet/prices は 1 SELECT で読む。TABLE_LICENSE（OPERATIONAL）/ d1-license-map.json（両リポ）/ PROD_TABLE_NAMES / OBSERVED_TABLE_COUNT=30 を同 PR 群で更新（B2）。
- evidence: run 34603928079: query 198 / PATCH 395 / create 20 で processed 333 → その日の ④ 全件を再 PATCH。毎 run ① 39 req（同じ 3,846 行）×11 = 429 req/日 + prices 2 + edinet 1。
- loc_delta: +75（DDL +15） | cost: Notion −2,000〜−3,000 req/日、tdnet_hourly −1〜3 分/run（月 −230〜−700 分）。D1 rows_read +3,846×14/日（無視できる）
- prod_migration: True | user_decision: 
- depends_on: L-04, L-45（表数 29→30 の 4 箇所同時更新） | scouts: CP-05

## L-21 [P1/low/stockStock/perf] ⑤ の sha256 重複検索を原本ごと 1 req から対象日 1 回のマップに変える
- action: file_upload.find_raw_page_by_sha256 の呼び出し前に、対象日の ⑤ を 1 回クエリして {sha256: page_id} を作り upload_raw_artifact に渡す（④ の date-scoped map と同じ手法）。edinet_daily.py:206-217、tdnet_hourly.py:169-171。
- evidence: run 34617018639（67 書類）の query 157 = ① 39 + ④ 1 + ⑤ 検索 ≈117。tdnet_hourly も毎時一覧原本 + XBRL 77 件/日で同じ形。
- loc_delta: +25 | cost: Notion −100〜−200 req/日
- prod_migration: False | user_decision: 
- depends_on:  | scouts: CP-20

## L-22 [P2/low/stockStock/cost] 116〜291 MB の非圧縮 CSV を毎日 R2 と Notion ⑤ に置くのをやめる（gzip、⑤ は 20 MB 超を R2 キー参照に）
- action: yfinance_prices.serialize_long_csv → gzip（sha256 は圧縮前）で R2 raw/…/{sha16}.csv.gz、jss_raw_files.sha256 は完全ハッシュのまま。Notion ⑤ は 20 MB 超を添付せず R2 キー（url プロパティ）を書く（file_upload.py:144-160,250-286）。
- evidence: 09-11 実測: prices_daily が R2 に 116 MB CSV + 26 MB parquet + 8.6 MB = 144 MB/日、jss_raw_files SUM(size_bytes) は 2 日で 407 MB → 約 3.6 GB/月。無料枠 10 GB は約 3 ヶ月で尽きる。TARGET-ARCHITECTURE §9.3 は gzip 前提で見積もっているが現行コードは非圧縮。Notion ⑤ にも同じ 116 MB を 12 パート UL。
- loc_delta: +30 | cost: R2 増分 3.6→約 0.7 GB/月（超過後の $0.015/GB-月の増え方が 1/5）、Notion 多パート UL −15 req/日、Notion ストレージ −4 GB/月
- prod_migration: False | user_decision: ⑤「原本添付必須」（不変条件 I1）を 20 MB 超のみ R2 キー参照に緩めてよいか
- depends_on: L-15（export_weekly の 291 MB は L-15 で消える） | scouts: CP-08

## L-23 [P3/high/stockStock/perf] supply_daily の supply/{code}.json 全置換 RMW（4,351 GET+PUT/日、10 分）を日次シャードに変える
- action: supply/{date}.json（1 日 1 PUT、全銘柄）+ D1 jss_supply_latest（既存）に変え、worker /v1/supply/:code と MCP jp_supply_series は D1 断面 + 直近 N 日シャードを読む。既存 per-code オブジェクトは 1 回だけ変換し、G5 の「既存点を落とさない」「writer 必須」ガードと SCHEMA_VERSION を新形式でも維持（test_supply_daily を書き換え）。MCP の返り値封筒（H2）は変えない。
- evidence: run 34613253360: R2 書込 613 s / 618 s、Class A 4,351 PUT/日 ≈ 91k/月、Class B 同数。オブジェクトは日数に比例して肥大。TARGET-ARCH §4.5 は RMW を構造的に不可能にすると定める。
- loc_delta: −40 (python) +60 (worker) | cost: R2 Class A/B 各 −91k/月、supply_daily 11→約 1 分（月 −210 分）
- prod_migration: True | user_decision: jp-stock-supply の R2 レイアウト変更（不変条件 G5 の schema=1 契約）と MCP jp_supply_series の読み替えを許容するか
- depends_on:  | scouts: CP-09, INV G5/H2

## L-24 [P2/low/stockStock/workflow] 毎日同じ 101 銘柄が yfinance 404 で失敗し ⑦/jss_job_runs が常に failed>0 → 連続 N 回 404 を ① に記録して母集団から外す
- action: prices_daily.py:147-157 で連続 N 回 404 の銘柄を ① の新プロパティ（価格取得不可）に記録し resolve_codes の母集団から外す。欠損は隠さず件数を ⑦ に出す。kabulab-cf #30 の普通株母集団と整合させる。
- evidence: 4 run（09-08〜11）で failed=104/103/101/101、内訳は数値コード 92 + 英字付き 9 が固定。「一部失敗」の恒常化で SLO の信号価値が下がる（TARGET §7.4）。
- loc_delta: +30 | cost: Notion/yfinance −200 req/日、監視ノイズ消滅
- prod_migration: False | user_decision: 404 銘柄（101 件、内訳は件数のみ）を母集団から外してよいか
- depends_on:  | scouts: CP-11

## L-25 [P2/low/stockStock/duplication] Notion ⑦ 収集ジョブログ（jss_job_runs と完全重複・読み手なし）を廃止
- action: upsert.write_job_log、runner.py:346-361、config.DB_REGISTRY の job_log、schema.py JOB_PROP_*、secret NOTION_DB_JOB_LOG を削除。test_schema の 7 DB 固定を 6 に。
- evidence: ops_check が読むのは jss_job_runs（ops_check.py:37-69）。⑦ は runner が書くだけで ~25 行/日、ops_check.yml 1 回で 4 行。
- loc_delta: −60 | cost: Notion −25 req/日、secret 1 本減
- prod_migration: False | user_decision: ⑦ を Notion UI で見ているか
- depends_on:  | scouts: CP-18

## L-26 [P3/high/stockStock/duplication] Notion ②株価テクニカル（読み手なし・毎日 3,720 行全置換）を廃止し prices_daily を R2/D1 のみにする（Notion 降格前提）
- action: prices_daily.py:176-264 の ② 書込、upsert.py:772-787,942-966 を削除し、R2 原本 + D1 断面（kabulab-cf の core_stock_financials か新投影）に書く。TARGET-ARCH §10.7 の方向。L-18 の並列化と排他（② を消すなら L-18(c) は不要）。
- evidence: ② を読むのは prices_daily 自身の map（38 req）と reconcile_weekly（L-02 で消滅）のみ。同じ値は R2 daily/ と core_stock_financials（3,755 行）にある。② PATCH 36 分/run。
- loc_delta: −400 | cost: prices_daily −40 分/run（月 −840 分）、Notion −3,800 req/日
- prod_migration: False | user_decision: TARGET-ARCH §12-8: Notion ② を人が見ているか。D1 側の writer を stockStock にするなら P5 と同じ判断
- depends_on: L-15, L-25, L-06 の判断 | scouts: CP-19

## L-27 [P1/low/stockStock/stale-doc] datasets.py の prices_daily データセットの writer 名（stockStock prices_daily）を実 writer（kabulab-cf daily.ts）に直す
- action: datasets.py:105 writer="prices_daily" を "kabulab-cf daily.ts" に、名前も実表に合わせる（例 d1_core_prices）。jss_writer_claims / jss_dataset_freshness.writer との整合を test_governance の鮮度 writer 突合（現在 core_stocks / yutai_benefits のみ検査）に追加。
- evidence: datasets.py:105-112: writer=prices_daily, db=DB_KABULAB, 実表 core_stock_financials は kabulab-cf daily.ts が書く。stockStock prices_daily は D1 を書かない。同じ取り違えを core_stocks/yutai_benefits で直した経緯が :167-168 に残る。
- loc_delta: ±5 | cost: $±0
- prod_migration: False | user_decision: 
- depends_on:  | scouts: CP-13（本統合で datasets.py:105 を確認）

## L-28 [P2/low/stockStock/workflow] ci.yml を on.push=main 限定にして PR ブランチの二重実行を止め、pytest に -rs を足して skip 理由を CI に出し、EDINET 由来フィクスチャ 5 件を追跡に戻す
- action: ci.yml:3-5 の on.push を branches:[main] に。pyproject addopts を "-q -rs"。scripts/capture_edinet.py で EDINET 5 件（documents_list_sample.json 等）を取得して commit し test_fixture_policy.ALLOWED に追加。残り 95 件の skip は test_fixture_policy に「skip を許す fixture_path 一覧」を固定して増加を検出。
- evidence: 30 日で ci 183 run（push 124 / PR 59、374 分）。pytest 1,478 passed / 103 skipped、skip 本体 1,026 行/17 ファイル。EDINET 由来だけが commercial-ok。TARGET-ARCHITECTURE:454「skip を CI で報告」。
- loc_delta: +20（yml/pyproject）+ フィクスチャ（サイズ未確認） | cost: GH −120 分/月（無料）、並列枠競合減
- prod_migration: False | user_decision: EDINET フィクスチャ（API キー要）を commit してよいか。DESIGN §3-6 捏造禁止の下で technicals の数式テストに合成 OHLCV を許すか
- depends_on:  | scouts: CP-14, CP-17, T-12, T-14

## L-29 [P1/low/stockStock/stale-doc] CLOUDFLARE-CONSOLIDATION.md 削除、DESIGN.md / CONTRACTS.md / README.md / AGENTS.md を現在形に圧縮（実銘柄コード列挙は件数へ）
- action: CONSOLIDATION (150) 削除、§4 硬い制約 5 項とみんかぶ逐条表 22 行を SPEC へ。DESIGN.md 350→約 90（§0/§7/§9/§10/§11/§12 削除、§1/§4/§6/§8 を現行 12 workflow に更新、§3-1 等の ID 維持）。CONTRACTS.md 204→約 105（L75-121 解消済み経緯・L123-148 バンドル表を削除、不変条件 #1 を runner.py の「片系統に残れば成功」に合わせる、L83-115 の実銘柄コード×区分を件数に）。README 159→約 50（ジョブ表を実 workflow に、L43-96 ローカル API・L117-144 更新記録を削除）。AGENTS.md L1 の存在しない @RTK.md 参照を削除。
- evidence: CONSOLIDATION §0「CF リソースを 1 つも持たない」は cloud_store 16 モジュールと矛盾。README「株価突合(stooq)」「ローカル API」は #33 で配線消失。CONTRACTS.md L83-115 は PUBLIC リポで実コードと種類株の対応を列挙（CF-CANONICAL:3603 自身が禁止規則を提案）。ls RTK.md → 無し。
- loc_delta: 約 −620（150+260+100+110+1） | cost: $±0
- prod_migration: False | user_decision: AGENTS.md「変更時は README に更新記録を残す」ルールを git log/PR 本文への一本化に置き換えてよいか
- depends_on:  | scouts: DOC-01, DOC-02, DOC-03, DOC-06, DOC-08, DOC-10

## L-30 [P2/medium/stockStock/stale-doc] CF-CANONICAL-DESIGN.md（3,661→約 680 行）と TARGET-ARCHITECTURE.md（717→約 190 行）を現在形の契約・配置表・未決事項だけに圧縮
- action: CF-CANONICAL: 決定表 1-10、D1 判定 3 条件、配置表（⑨を現在形に）、R2 キー書式・契約キー・後退禁止・ライフサイクル、core_stocks 絶対規則、jss_* DDL、冪等性、degrade 表、M1-M4、ゲート、全面中止手順、未決、H チェックリストを残し、実施記録・訂正経緯・レビュー F1-F9・容量試算・Notion 雛形（未実装）・REST 21/MCP 12（未実装、worker/README が現状正）を削除。完了フェーズ P2/P3/P4a は状態表 1 行へ。TARGET: §2/§5/§9/§10/§11 大半/付記を削除、§8.5 の LOCAL_DB 記述と §7.1 ACCEPTED_RED 2 件（現在空）を訂正、行数内訳（:706）を更新。src の docs 参照 25 箇所と worker/README のリンクを更新。
- evidence: 取込ジョブ節のジョブ名（supply_jsf/supply_jpx/yutai_monthly）が jobs/ と不一致。Notion 雛形は DB_REGISTRY 7 キーのまま未実装。移行フェーズ節の約 250 行が経緯。TARGET §8.5「LOCAL_DB_* が 7 workflow に並ぶ」は #33 で除去済み、§7.1 ACCEPTED_RED は slo.py:159 で空。
- loc_delta: 約 −3,500 | cost: $±0
- prod_migration: False | user_decision: P5〜P8 の移行計画を今後も実行するか（残すなら手順だけ短く保持、やめるなら DECISIONS に 1 行）。目標像文書を残すか
- depends_on: L-29 | scouts: DOC-04, DOC-05, C open_q 10

## L-31 [P2/medium/stockStock/duplication] docs を SPEC.md（現在形）/ TARGET-ARCHITECTURE.md（目標像）/ DECISIONS.md（決定と未決）の 3 本に再編し、§ ID を SPEC 見出しに維持
- action: DESIGN+CONTRACTS+CF-CANONICAL の現在形部分 → SPEC.md（約 550 行）、決定表・未決 → DECISIONS.md（約 60 行）。src の docs ファイル名参照 25 箇所（CF-CANONICAL 16 / TARGET 5 / CONTRACTS 3 / DESIGN 1）だけ置換し、§3-1 等 528 箇所のコードは無変更。
- evidence: 同じ規則が 3〜4 文書に重複（ライセンス表 4 箇所、sector/sector33 4 箇所、D4 分母 3 箇所、後退禁止ガード 3 回）。grep -rc '§' src → 528。
- loc_delta: docs 合計 5,081→約 830（L-29/L-30 の圧縮を含めた最終形） | cost: $±0
- prod_migration: False | user_decision: 3 本構成でよいか / § ID を維持する方針でよいか（削るなら 528 箇所の編集）
- depends_on: L-29, L-30 | scouts: DOC-09, CMT-09

## L-32 [P1/low/stockStock/comment-noise] src の経緯コメント（採らなかった案・当初の誤認・2026-09-1x 実測・PR 番号）を規則 + テスト名に圧縮（上位 15 モジュールで約 −1,500 行）
- action: governance.py 310→約 80（語彙表 3 行 + 照合順序 + 戻し方）、core_stocks.py 197→約 55（4 規則 + テスト名）、slo.py 176→約 65（測り方 3 規則 + 祝日 3 案）、upsert.py 347→約 220（#13/#14 を規則 1 行ずつ）、license_map/financials/schema 448→約 155、tdnet_yanoshin normalize_company_code 19 行→1 行、stock_code/mappers/core_stocks_migrate/runner/datasets/freshness_probe/master_sync 720→約 390。universe_guards/fin_parity は L-06/L-07 の判断後。「テストが固定している不変条件」（invariants §F4/B6/B3/E2 等 14 項目）は削除せず 3〜6 行の「規則 + テスト名」に。
- evidence: src 17,833 行中 コメント 1,575 + docstring 3,452 = 5,027 行（28%）、経緯キーワード 956 行、長ブロック 77 箇所 1,935 行。Issue/PR 番号参照 #13 16 / #14 5 / #27 4 / #39 3。テスト docstring の経緯約 150 行（test_universe_guards L1-18 等）も同時に。
- loc_delta: 約 −2,100（src −1,900〜−2,300、tests −150） | cost: $±0
- prod_migration: False | user_decision: slo.py 祝日の扱い 3 案（緑を 1 営業日緩める / 祝日カレンダー / yellow を Issue にしない）を決めれば未決コメント 20 行も消せる
- depends_on: L-06, L-07（対象モジュールの存廃） | scouts: CMT-01〜CMT-08, TST-01, T-10

## L-33 [P3/low/stockStock/comment-noise] margin_weekly.yml 冒頭 17 行の切替手順と test_margin_weekly_schedule_is_deliberately_disabled は 2026-09-28 の writer 切替時に反転・削除（新 cron は足さず既存コメントアウトを有効化）
- action: P2 切替 PR で EXPECTED に margin_weekly を足し当該テストを削除、yml は見出し 2 行だけ残す。TARGET-ARCHITECTURE:387（JPX は TS）と矛盾するのでユーザー判断が先。切替しないなら jobs/margin_weekly.py 158 + collectors/jpx_margin.py 193 + cloud_store/margin.py 125 + yml + tests(37 件) が丸ごと削除候補（約 −800 行）。
- evidence: margin_weekly.yml:21 cron コメントアウト、gh run 0 件。yml 冒頭は 2026-09-26 に kabulab-cf 側停止→09-28 から stockStock。TARGET-ARCHITECTURE:387「JPX は TypeScript、重複実装は削除」。
- loc_delta: −35（切替時）/ −800（切替中止時） | cost: cron 総数不変（kabulab-cf −1 / stockStock +1）
- prod_migration: False | user_decision: JPX 信用残 writer を 2026-09-28 に stockStock へ切り替えるか、TARGET-ARCHITECTURE どおり TS に残して Python 実装を消すか
- depends_on:  | scouts: WF-01, T-17, C open_q 1, INV K4

## L-34 [P2/medium/stockStock/test] D1 ダブル 8 個・R2 ダブル 3 個を tests/_doubles.py に統合し、_settings×7 / _prov×4 / NOTION ENV×3 等の小ヘルパを conftest fixture に集約
- action: SqliteD1(D1Store)(ddl, seed, fail_on) / RecordingD1 / FakeR2(writer) の 3 クラス（約 110 行）で 11 クラスを差し替え（test_license_map の「upsert を上書きせず D1Store 継承」方針を踏襲）。conftest に dry_settings(tmp_path)・notion_env・provenance(**kw) を追加。
- evidence: `def query(self, sql` 9 定義/8 ファイル、sqlite3.connect(':memory:') 9 箇所、差は DDL・D1Error 包み直し・bind 上限・fail_on_update のみ。_settings 7 本のうち 5 本は同一。
- loc_delta: 約 −280 | cost: $±0（pytest 2.5 s のまま）
- prod_migration: False | user_decision: 
- depends_on: L-01, L-10, L-08（削除後に差し替え対象が減る） | scouts: T-08, T-09

## L-35 [P3/low/stockStock/test] リファクタで邪魔になるテスト固定を落とす: 文言固定→属性照合、振る舞いテストと重複するリテラル固定、ops_check.yml 文言固定、test_jobs_smoke の主題分割
- action: (1) 日本語 match= 65 箇所 / caplog 部分一致 23 箇所は、メッセージを変える改修と同時に GuardError/D1Error/CoverageReport に kind 属性を持たせ等価比較へ（文言を変えないなら触らない）。(2) test_cloud_financials.py:160-176 SQL 文字列 3 件（TestCorrectionMerge が実行で検証）、test_upsert.py:227-245 dict リテラル 3 件、test_governance.py:239-245、TestSetTargetExtractor（ヘルパ自身のテスト）を削除。(3) test_jobs_smoke.py:797-815 の step 名・jq 式固定を削除（:783-795 の「id 一覧 ⊆ close 条件」は残す）。(4) test_jobs_smoke.py:818-900 TestEdinetLargeHolding → test_edinet.py、:700-815 → test_workflows.py に移動。
- evidence: grep 集計。TestCorrectionMerge は本物 DDL の sqlite で「NULL は上書きしない」を実行検証。test_jobs_smoke.py は 900 行・8 テーマで docstring と内容が乖離。
- loc_delta: 約 −80（移動分は 0） | cost: $±0
- prod_migration: False | user_decision: 
- depends_on: L-02, L-32 | scouts: T-07, T-10, T-11, T-15

## L-36 [P1/medium/both/test] jss-api の RESTRICTED_COLUMNS を d1-license-map.json と突合するテストを足し、地図を kabulab-cf にも同一バイト列で置いて ci.yml の pending-peer を畳む
- action: (1) stockStock worker/test に d1-license-map.json を読み column_license.core_stocks の personal-only 集合 == RESTRICTED_COLUMNS.core_stocks を等号固定するテスト（stock-code-contract.test.ts と同手法）。(2) kabulab-cf tests/fixtures/contracts/ に同一 JSON を追加し public-columns.test.ts で PERSONAL_ONLY_COLUMNS と突合、kabulab ci.yml を compare_contract 関数形にして 2 ファイル突合、stockStock ci.yml から pending-peer を外す（両 main 同時マージ必須）。(3) JSON を Python リテラルから生成する scripts/export_contracts.py（dry）を置く。
- evidence: grep -rn d1-license-map stockStock/worker → license.ts のコメント 1 件のみ。kabulab-cf tests/fixtures/contracts/ に不在。stockStock ci.yml コメント「相手リポへ入ったら必ず pending-peer を外すこと」。29 表の一覧が 4 箇所に手書き。
- loc_delta: stockStock +65 / −8、kabulab-cf +30 + JSON 10 KB | cost: $±0（CI 5 分枠内）
- prod_migration: False | user_decision: 
- depends_on:  | scouts: INV-C1, INV-C2, INV-C7

## L-37 [P3/low/both/duplication] 「[ジョブ失敗] Issue を 1 本立てる」シェルを kabulab-cf composite action に寄せ、探索を ops_check.yml と同じ完全一致 jq に揃える。datasets.py の license_tag を TABLE_LICENSE から導出
- action: kabulab-cf .github/actions/notify-failure（composite、workflow ではない）に catchup/stock-sync/vwap-ingest の 3 箇所を寄せ、`gh issue list --search "$TITLE in:title"` の部分一致を完全一致 jq に。stockStock ci.yml:35-52 も同修正。datasets.py の uniform 表は TABLE_LICENSE[location].tag から導出し test_ops_slo に一致テストを追加。
- evidence: 4 箇所同一ロジック、ops_check.yml だけが「部分一致は無関係な Issue を拾う」と直して乖離。datasets.py:95-236 の license_tag 7 件と governance.TABLE_LICENSE が二重宣言でタグ一致テスト無し。
- loc_delta: kabulab-cf −40/+25、stockStock −6/+10 | cost: $±0
- prod_migration: False | user_decision: 
- depends_on:  | scouts: INV-C5, INV-C8

## L-38 [P1/low/kabulab-cf/dead-code] どの根からも到達しない一度きりスクリプト 7 本と package.json の yuho:investigate を削除
- action: scripts/migrate/yuho-neon-to-d1.ts (409)、services/financial-math/scripts/verify-capm-bs.ts (74)、services/otakara-yutai/data-scripts/{test-parse (31), verify-data (257), fix-stock-names (75)}.ts、services/yuho-quant/data-scripts/{investigate (175), investigate2 (79)}.ts を削除。scripts/README.md の migrate/ と yuho CLAUDE.md の investigate2 言及を消す。export-benefit-descriptions.ts と fetch-yutai-full.ts は all-monthly.ts が spawn するので残す。
- evidence: importgraph.mjs: worker/scripts/tests/drizzle のどの根からも到達しない。verify-data.ts:6 は廃止済み kabulab.vercel.app を叩く。yuho-neon-to-d1.ts は @neondatabase/serverless の唯一の利用者。最終 commit 2026-06-18。
- loc_delta: −1,100 | cost: $±0
- prod_migration: False | user_decision: 一度きりの移送ツールを履歴として残すか（git 履歴には残る）
- depends_on:  | scouts: KC-01, KL-04, KL-02

## L-39 [P1/low/kabulab-cf/dependency] Neon(pg) 期の drizzle config 4 本・移行 SQL 15 本・package.json スクリプト 12 本・DATABASE_URL・未使用依存（@neondatabase/serverless, vercel, @hono/node-server, prettier）を撤去
- action: drizzle.{rsi-screening,otakara-yutai,swing-trading,ir-catalog}.config.ts、drizzle/*.sql(7)、services/*/drizzle/**(8 + meta)、db:push:*×4 / db:generate:{rsi,otakara,swing,ircat} / db:studio:*×4 を削除。.env.example:1 と README:217 の DATABASE_URL、src/shared/env.ts:13-14・services/yuho-quant/src/env.ts:23-25 のアクセサ、tsconfig include の drizzle.*.config.ts を d1 に限定、exclude .vercel / include api/**/* と eslint ignores .vercel/ を削除。4 パッケージを外して pnpm-lock.yaml 再生成。
- evidence: 全 config が dialect=postgresql、drizzle.d1.config.ts:14-15 と README:106,187 が obsolete と明記。grep: vercel 0、@hono/node-server 0、prettier 0（設定ファイル無し）、@neondatabase は L-38 の migrate のみ。DATABASE_URL の読み手は pg config と migrate だけ。api/・.vercel/ は存在しない。サイズ vercel 9.1 MB、prettier 8.3 MB。
- loc_delta: −1,330 + lockfile 数百行 | cost: $±0。CI pnpm install が僅かに軽く、node_modules −18 MB
- prod_migration: False | user_decision: prettier をエディタで手動利用しているなら残す。README:178-188「残るは Neon 解約」が完了しているか（完了なら節ごと削除）
- depends_on: L-38 | scouts: KC-02, KC-07, KL-01, KL-02, D1-22, KCF-DEP-15, KD-20

## L-40 [P1/low/kabulab-cf/dead-code] drizzle/d1/meta の旧スナップショット 0000〜0011（26,822 行）を git rm
- action: 12 ファイルを削除、_journal.json と 0012_snapshot.json は残す。drizzle/d1/README.md に「旧 snapshot は残さない」を 1 行追記。
- evidence: 一時ディレクトリへの git archive 複製で 0000-0011 を消して drizzle-kit generate → No schema changes、check → Everything's fine を実測。generate は _journal の最新 idx の snapshot だけを読む。ci.yml:52-63 の再生成漏れ検出も同コマンド。
- loc_delta: −26,822 | cost: $±0（リポジトリ約 1 MB 減）
- prod_migration: False | user_decision: 
- depends_on:  | scouts: KC-03

## L-41 [P1/low/kabulab-cf/dead-code] otakara-yutai の未マウント middleware 2 本・旧 v1 スクレイパ一式・src/shared/db/core-repo.ts を削除
- action: middleware/{error-handler,rate-limiter}.ts と tests/unit の 2 テスト (393 行)、services/yutai-scraper.ts (281)・yutai-data-provider.ts (142)・validators/yutai-scraper.ts (38)・tests/unit/yutai-scraper.test.ts (556)・data-scripts/fetch-yutai-data.ts (260) を削除。yutai-stock-universe.test.ts は契約 2（core_stocks へ INSERT するのは universe.ts だけ）だけ残す。core-repo.ts (120) は共通化 P1 を続けないなら削除。docs/002 と otakara CLAUDE.md のツリーから消す。
- evidence: importgraph.mjs: いずれもテストからのみ到達。app.ts は独自 onError(:53) と hono logger を使い import しない。本番取込は fetch-yutai-full.ts(v2)。core-repo.ts の 5 関数は外部参照 0 なのに 3 commit で保守されている。
- loc_delta: −1,900 | cost: $±0
- prod_migration: False | user_decision: core-repo.ts への集約（共通化 P1）を続ける意思があるか。無ければ削除
- depends_on:  | scouts: KC-04, KC-05, KC-06, KL-03

## L-42 [P1/low/kabulab-cf/dead-code] VPN ローテーション・動作不能な build_stocks.py・flake.nix の openvpn・到達不能な public/sw.js・重複 script/未使用 export を削除
- action: scripts/vpn/vpngate-rotate.sh (111)、scripts/vwap/build_stocks.py (94、契約外の CODE_RE を持つ)、flake.nix:19-24 openvpn、public/sw.js (18)、package.json dev:cf/deploy:cf、scripts/vwap/lib/r2.ts r2Delete、design.ts BASE_CSS（L-56 で使わないなら）、型 export の不要な export を削除。stocks.json 自体は触らない（不変条件 G3）。
- evidence: vpngate-rotate.sh 参照 0（YAHOO_PROXY_BASE で代替）。build_stocks.py:22-29 が自ら「動かない・呼ばれない」と明記、出力先 docs/data/ は不在。sw.js は旧 vercel オリジンの SW 後始末で workers.dev では register 履歴なし。r2Delete 呼び出し元 0（stockStock 側は delete 非実装が不変条件）。
- loc_delta: −300 | cost: $±0（devShell から openvpn が消える）
- prod_migration: False | user_decision: 
- depends_on:  | scouts: KC-08, KC-09, KC-21, KL-04, INV-C6, INV open_q 10

## L-43 [P3/medium/kabulab-cf/dead-code] rsi-screening の JSON API 2 本（利用者ゼロ・Yahoo 由来派生値の JSON 公開）を撤去
- action: services/rsi-screening/src/index.ts:11-12、routes/screening.ts (26)、routes/stocks.ts (22) を削除、docs/001 の API 節を消す。公開 URL /rsi-screening/api/screening と /api/stocks/:code は 404 になる。
- evidence: SSR ビュー・public/・テストからの参照 0。返す内容は rsi_percentile（personal-only 由来の派生値）。
- loc_delta: −55 | cost: 外部から叩かれていれば D1 rows_read 減（現状の呼び出し量は未確認）
- prod_migration: False | user_decision: 外部ツール・ブックマークからこの JSON を叩いていないか。personal-only 派生値の JSON 公開がライセンス境界に触れないかの判断
- depends_on:  | scouts: KC-12

## L-44 [P3/medium/kabulab-cf/dead-code] 一過性の互換シム 3 つ（yuho metric=backlog・otakara ?sort=&order=・Notion 旧フラット DB 退避）を撤去
- action: services/yuho-quant/src/routes/pages.ts:58-60,107-145 detectDeprecatedParams、services/otakara-yutai/app.ts:915-918、src/shared/notion-archive/dataset.ts:172,183,262-272,282（archiveFlatDbOnce/obsoleteFlatTitle）を削除。dataset.ts は削除前に Notion「バックアップ」配下に `適時開示｜ir-catalog` が無いことを目視確認。
- evidence: detectDeprecatedParams を参照するテスト 0。旧フラット DB 退避は初回 1 回の移行処理（Notion 側の残存は未確認）。
- loc_delta: −70 | cost: $±0
- prod_migration: False | user_decision: 旧 URL のブックマーク互換を切ってよいか。Notion の旧フラット DB は退避済みか
- depends_on:  | scouts: KC-14(docs lane)

## L-45 [P2/low/kabulab-cf/schema] 本番 D1 の冗長索引 4 本（同一列に UNIQUE + 通常索引）を DROP
- action: src/shared/db/core-schema.ts:171 idx_core_financials_stock_id、services/otakara-yutai/src/db/schema.ts:107,126、services/ir-catalog/src/db/schema.ts:73 uniqueIndex(ir_disclosures_tdnet_uq) の宣言を外す → pnpm db:generate:d1 → 生成 SQL が DROP INDEX ×4 だけであることを読む → wrangler d1 execute --remote --file で適用 → sqlite_master で確認。EXPLAIN で stock_id/tdnet_id 等値検索の計画が unique 側のままであることを確認。stockStock E7 は core_stocks の索引しか見ないので影響なし。
- evidence: sqlite_master 実測（両レーン一致）: core_stock_financials(stock_id) 2 本、otakara_stock_financials/otakara_stock_scores(stock_id) 各 2 本、ir_disclosures(tdnet_id) UNIQUE 2 本。原因は drizzle の .unique() と index()/uniqueIndex() の二重宣言。EXPLAIN は全て unique 側を使う。
- loc_delta: −6 + 生成 SQL 4 文 + snapshot 1 本 | cost: 日次 upsert の索引書込 −3,700、月次 −3,272、TDnet −800〜1,500/日（D1 が索引更新を rows_written に数えるかは未実測）。索引ストレージ減
- prod_migration: True | user_decision: 
- depends_on:  | scouts: KC-10, D1-05

## L-46 [P2/low/kabulab-cf/schema] 読み手の無い索引（idx_swing_indicators_trend_long / turnover、ir_disclosures_pdf_sentiment_idx / pubdate_idx、planner が使わない idx_rsi_percentile_min）を DROP
- action: D1-13 の sweep を採るなら idx_swing_signals_computed は残す。L-48 で rsi JOIN 順を直すなら idx_rsi_percentile_min は残す。他 4 本は宣言を消して generate → DROP INDEX。
- evidence: EXPLAIN 77 本のどれにも現れない（swing の並び替えは USE TEMP B-TREE、ir home は primary_tag 索引）。grep: trendLong / entrySignals.computedAt / disclosures.pdfSentiment を WHERE/ORDER BY に使う経路 0。
- loc_delta: −8 | cost: 日次 indicators 索引書込 −7,400、TDnet −800〜1,500×2。rows_read 不変
- prod_migration: True | user_decision: ir_disclosures_pubdate_idx に将来の期間検索の用途を予定しているか
- depends_on: L-48, L-52 | scouts: D1-06

## L-47 [P1/low/kabulab-cf/perf] 日次 sync の swing_daily_ohlcv フルスキャン 3 本（Phase 1 MAX(date) GROUP BY / Phase 4 prune COUNT / Phase 6 投影 rowid カーソル）を消す
- action: (1) Phase 6: rebuildMomentumProjection の D1 読み直し（40k 行×9 ページ）をやめ、Phase 3 の各銘柄で snap.ohlcv6mo の末尾 90 本から encodeCloses して p_momentum を upsert（1 銘柄 1 文、掃除 DELETE はそのまま、source_max_date は末尾 1 文）。(2) Phase 1: MAX(date) を swing_stock_indicators.latest_date の LEFT JOIN で代替（latest_date NULL の銘柄だけ従来どおり 6mo upsert）。(3) Phase 4 の全銘柄 sweep は月曜のみ（同 run 内の曜日分岐、新 cron なし）。/emh の数値は同じ入力・同じ関数で不変。
- evidence: EXPLAIN（本番）: Phase 1 と Phase 4 は SCAN swing_daily_ohlcv USING COVERING INDEX idx_swing_ohlcv_stock_date（全 336,169 行）、Phase 6 は SEARCH rowid>? で ≈336k。3 本で ≈1.0M rows_read/run。buildSnapshot は Yahoo 5y を既にメモリに持つ（daily.ts L787-806）。KCF-PERF-03 の「prune を Phase 6 走査に統合」案より D1-01 の「Phase 6 の走査自体を消す」案を採る（両走査が消える）。
- loc_delta: −80〜−120 | cost: rows_read −672k/run 確定 + Phase 4 週 1 で −269k/run 平均 → 月 ≈ −2,000 万。Actions −数分/run。$ 不変
- prod_migration: False | user_decision: 
- depends_on:  | scouts: D1-01, KCF-PERF-02, KCF-PERF-03

## L-48 [P1/low/kabulab-cf/perf] rsi /screening・swing signals/dashboard・/emh 各タブの JOIN 順を派生表外側に固定して走査行を桁で下げる
- action: rsi screening-service.ts:140-181 を `.from(stockRsiPercentile).crossJoin(stocks)` + WHERE 等値に（swing /screening の前例 pages.ts:196-205）、EXPLAIN で idx_rsi_percentile_min が外側になることを確認。swing pages.ts:125-145,283-313 を entry_signals 外側に。emh pages.ts:313-461 は count と rows を window 関数 count(*) over() で 1 クエリに。
- evidence: 実測 rows_read: rsi 一覧 9,031 + 鮮度 COUNT 7,399 = 16,430/表示（EXPLAIN は core_stocks(is_active) が外側）。swing top5 6,700 / signals 6,700（表は 1,021 行）。emh small-cap count 7,399 + rows 9,771。swing /screening は CROSS JOIN で 7,585→354 に下げた前例。
- loc_delta: ±40 | cost: rsi 16,430→≈1,500、swing dashboard ≈17,600→≈8,500、signals 6,700→≈2,050、emh 各タブ −7,000〜−9,000/表示
- prod_migration: False | user_decision: 
- depends_on:  | scouts: D1-07, D1-10

## L-49 [P1/low/kabulab-cf/cost] core_stock_annual_financials の毎日 ≈15,900 行 upsert を週 1 回（または差分時のみ）にし、TDnet 7 日窓 upsert を「変わった行だけ更新」にする
- action: daily.ts:946-964 の年次 upsert を月曜のみに分岐、または Phase 1 で `SELECT stock_id, MAX(fiscal_year)` を 1 回引いて既存年度は書かない。ir-catalog ingest.ts:222-260 の onConflictDoUpdate に `setWhere`（title/tags/primary_tag/document_url の差分）を足す。DDL なし。
- evidence: annual 15,936 行に対し sqlite_sequence.seq 1,454,013 → 毎 run 全行 DO UPDATE。読み手は rsi 詳細の売上推移 4 行のみ。ir_disclosures 37,641 行に対し MAX(id) 244,343（6.5 倍の churn）、直近 7 日 793 行を毎日書き直し。
- loc_delta: +16 | cost: rows_written −13,000〜−15,900/run（月 −30 万）+ TDnet −700〜−1,400/日、HTTP 往復 −3,700/run
- prod_migration: False | user_decision: 
- depends_on:  | scouts: D1-03, D1-04

## L-50 [P2/low/kabulab-cf/perf] ir-catalog home/signals の「高シグナル最新 25 件」（12,485 rows_read）を部分索引で引く
- action: services/ir-catalog/src/db/schema.ts に `CREATE INDEX ir_disclosures_high_signal_pubdate ON ir_disclosures(pubdate DESC) WHERE primary_tag IN (7 タグ)` を drizzle の .where(sql) で宣言し、query.ts recentHighSignal:214-253 の WHERE を classify.ts HIGH_SIGNAL_TAGS から生成した同じ IN リストにして planner が部分索引を選ぶことを EXPLAIN で確認。代替: is_high_signal 列 + 索引。
- evidence: 実測 12,485 で 25 行を返す（primary_tag 索引で 7 タグ全件を集めて USE TEMP B-TREE）。高シグナル行 ≈1.2 万は TDnet 増加に比例。
- loc_delta: +15 | cost: home/signals 各 12,485→≈50/表示。TDnet upsert 時の索引 +1 本（高シグナル行のみ）
- prod_migration: True | user_decision: 
- depends_on:  | scouts: D1-08

## L-51 [P2/medium/kabulab-cf/perf] yuho-quant の受注/海外スクリーニング（6 万超 / 2.5 万 rows_read）を投影表 p_yuho_growth に事前集計し、otakara の権利月・ジャンル絞込（2.8 万）を scores 側の集計列で引く
- action: projection-schema.ts に p_yuho_growth（stock_id PK、CAGR/YoY/比率/地域比率、as_of、source_max_date）を宣言し EDINET catchup の末尾（既存 run に相乗り、新 cron なし）で純関数から再生成、画面は投影を WHERE/ORDER BY で引く。TABLE_LICENSE に COMMERCIAL_OK で追加、d1-license-map 両リポ更新（B2）。otakara: monthly Phase 1 で otakara_stock_scores に yutai_months / yutai_genre_ids を書き、app.ts:192-203,936-944 の yutai_benefits IN 副問合せをやめる（本文には触れない）。
- evidence: 実測: 受注プルダウン 27,393（本体は推定 3.5〜4 万）、海外本体 19,887（4,916 行返却）、全ファクトを毎表示 JS へ転送。otakara /api/screening?month= は SCAN yutai_benefits 8,295 を rows と count で 2 回（14,533 + 同額）。
- loc_delta: +190 / −200 | cost: yuho −5〜8 万 rows_read/表示、otakara ≈2.8 万→≈7,500。書込 +≈1,500 行/日 + 1,636/月
- prod_migration: True | user_decision: 地域バケット別比率 4 列を投影に持つか、地域指定時だけ従来クエリへ落とすか
- depends_on: L-36（表数の 4 箇所同時更新） | scouts: D1-09, D1-21

## L-52 [P2/low/kabulab-cf/schema] swing_stock_screening（指標の純関数 4 bool）を swing_stock_indicators の列に畳み、swing_entry_signals の銘柄ごと無条件 DELETE（3,755 文/日）を run 末尾の sweep 1 文にする
- action: indicators に liquidity_ok/volatility_ok/trend_ok_long/trend_ok_short/all_passed_* を追加し索引を移す → daily.ts:1151-1182 は 1 文で書く → 読み手 3 箇所を indicators へ → 表 DROP（RETIRED_TABLES / CHILD_TABLES / d1-license-map / PROD_TABLE_NAMES を更新、stockStock 先行）。signals: writeStockSnapshot は INSERT のみ（computed_at = run 開始秒）、Phase 3 後に `DELETE WHERE computed_at < :runStartedSec` 1 文（idx_swing_signals_computed を使う）。
- evidence: screenStock は indicators 6 値の純関数（daily.ts:1152-1159）、supply_note/catalyst_note は定数文字列。表 1,021 行なのに 3,755 銘柄分の DELETE（大半 0 行）。
- loc_delta: −100 | cost: rows_written −3,755/run、HTTP 往復 −7,500/run
- prod_migration: True | user_decision: 取得失敗銘柄の前日シグナルを『残す（現状）』か『消す（鮮度のない値を出さない）』か
- depends_on: L-36 | scouts: D1-12, D1-13

## L-53 [P3/medium/kabulab-cf/schema] 小さなスキーマ整理: 1 銘柄 1 行の表からサロゲート id+AUTOINCREMENT を外す、swing_sector_daily の保持 30 日と常時 NULL の pct_5d、rsi_percentile.operating_margin_ttm の二重持ち
- action: (a) core_stock_financials / rsi_percentile / otakara 2 表を stock_id PK に（表の作り直し SQL を手で読む。yuho_documents / ir_disclosures は他表から参照されるので対象外）。(b) Phase 5 の delete に date < now−30d を足し pct_5d を DROP COLUMN。(c) 読み手を stockFinancials.operatingMargin に付け替え列を DROP。
- evidence: sqlite_sequence が upsert の DO UPDATE でも進む（rsi_percentile 264,244 / 3,755 行）。sector_daily 2,727 行で読み手は最新日 5 行、pct5d は常に null（daily.ts L755）。TARGET §4.3: operating_margin_ttm と operating_margin は 3,764 件完全一致。
- loc_delta: −45 | cost: 索引 −4 本、sqlite_sequence 書込 −(3,700×3+15,900)/run、storage 数 MB。一度きりの rows_written ≈3 万
- prod_migration: True | user_decision: 
- depends_on: L-45 | scouts: D1-14, D1-17, D1-18

## L-54 [P3/medium/both/duplication] otakara_stock_financials（core_stock_financials + swing_stock_indicators の月次コピー）を廃止し、固有列 yutai_yield を scores へ移す
- action: otakara_stock_scores に yutai_yield を ALTER ADD → monthly.ts が scores 1 文だけ書く → 読み手を core_stock_financials LEFT JOIN + swing_stock_indicators.rsi_14 へ → 表 DROP。stockStock: RETIRED_TABLES、CHILD_TABLES、d1-license-map を先に更新。
- evidence: 19 列のうち固有は yutai_yield と data_date だけ（monthly.ts L137-158）。ただし読取は一覧 8,518→≈10,100 に増える（JOIN 先 +1 表）。設計書 A-4/A-7 は VIEW 化を採らない。
- loc_delta: −120 | cost: storage −1 表/−2 索引、rows_written −1,636/月。rows_read は +≈1,600/表示（「増やさない」制約に触れる）
- prod_migration: True | user_decision: 月次断面（data_date）の意味付けを捨てて日次値を出してよいか。読取 +1,600/表示を受け入れるか
- depends_on: L-36 | scouts: D1-11

## L-55 [P3/high/kabulab-cf/schema] swing_daily_ohlcv（全行の 68%・18 MB）を廃止し個別銘柄の日足は R2 daily/{code}.json を Worker BUCKET から読む
- action: L-47 の後に。financial-math price-cache.getOhlcvSeries を BUCKET.get('daily/'+code+'.json') に、emh low-vol の 50 銘柄も同経路。writeStockSnapshot の OHLCV upsert と Phase 4 を削除、表を退避→DROP。P6（②日足 writer 移管）の処理順・ガード（K3）はこの変更で先取りしない。
- evidence: 336,169 行。読み手は capm/bs の 1 銘柄（92）、emh low-vol 50 銘柄（4,509）、日次 cron だけ。R2 daily/ は 10 年・4,445 銘柄を既に配信。ただし R2 daily の更新は vwap-ingest の月水金で日次ではない。
- loc_delta: −250 | cost: D1 storage −18 MB、rows_written −3,700〜7,400/run、往復 −3,700/run。R2 Class B +数十/日
- prod_migration: True | user_decision: capm/bs/low-vol の日足が最大 2 営業日古くなることを受け入れるか、vwap-ingest を毎日にするか（Actions 無料だが ≈1h/回 増）
- depends_on: L-47 | scouts: D1-02

## L-56 [P2/medium/kabulab-cf/perf] D1 REST 書込の往復数を減らす: 日次 sync の銘柄ごと 7〜8 文を表ごとの multi-row upsert（または Worker db.batch ルート）に、monthly rebuild の逐次 3,230 往復も同様に
- action: 案A: 銘柄 snapshot を N 件バッファし表ごとに VALUES 複数行 upsert（bind 100 内: rsi 8 行/文、financials 8、indicators 3、screening 14、annual 33）。案B: 認証付き POST /api/ingest/snapshot を足し binding の db.batch() で 1 往復・同一リージョン。monthly.ts:136-200 も 8〜16 行/文に。D1 REST /query の複数文+params 可否を先に確認。
- evidence: monthly 実測 3,230 往復/670.9 秒 = 208 ms/往復（D1 は APAC、runner は US）。日次 3,715×7.5 ≈ 27.9k 往復 ≈ Phase 3（1,725 秒）の 2/3（推定）。drizzle-orm 0.45.2 sqlite-proxy は batchCallback 対応だが d1-http-client.ts:38 は未使用。
- loc_delta: +80/−30（案A） | cost: D1 REST 呼び出し −80〜85%、日次 run −12〜18 分、月次 −9 分（推定）。rows_read/written 不変。$±0
- prod_migration: False | user_decision: 案A（multi-row）/ 案B（Worker 側に書込経路を増やす）の選択
- depends_on: L-52 | scouts: D1-20, KCF-PERF-01, KCF-PERF-05

## L-57 [P1/low/kabulab-cf/workflow] stock-sync の失敗率 52% を下げる: 一過性失敗の回収上限 100 を時間予算制にし、vwap 取込で Yahoo 404 を 3 回リトライしない
- action: daily.ts:191 MAX_RECOVERY_TARGETS → 「残り時間 ≤30 分まで」の予算ループ、2 パス目は Retry-After を尊重。失敗判定（0 件 / ≤1%）は別途決める。scripts/vwap/lib/r2.ts:76-88 retry() で 404 を即 throw に。
- evidence: schedule 21 run 中 11 失敗、いずれも 94〜99.5% 成功で isDailySyncIncomplete が exit 1。09-02 は 227 件、09-03 は 43 件を上限超過で未回収。timeout 90 分に対し実績 24〜32 分。vwap run 34599729252: errors 27（全て 404）を daily/intra で 3 回リトライ。
- loc_delta: +30/−10 | cost: Issue 誤報減、Yahoo −160 req/run（vwap）、worker 待ち −2.7 分/run。$±0
- prod_migration: False | user_decision: 『1 件でも失敗なら run 失敗』を維持するか、閾値付き警告に変えるか
- depends_on:  | scouts: KCF-WF-04, KCF-VWAP-06

## L-58 [P3/high/kabulab-cf/cost] VWAP 取込の母集団を stocks.json（4,445 件・凍結・非普通株 717）から変える／日足二重取得の統合／5 分足の月別シャード化 —— 外部読者と R2 契約に衝突するため設計判断が先
- action: KC-19（母集団を core_stocks active∧equity に）、KCF-VWAP-07（intra/{code}/{YYYY-MM}.json）、KCF-VWAP-08（stock-sync と vwap の日足 chart 二重取得を 1 経路に）はいずれも不変条件 G1/G3/G4 と衝突する: daily/ の母集団は全種別（日経225 連動 ETF 1 本 を別リポジトリ（ブレイク検証）が読む）、intra/{code}.json は別リポジトリ（動画制作用） data/intraday.py が直読。実施するなら (1) stocks.json の再生成経路（core_stocks 全種別 + JPX 一覧）を先に設計、(2) intra は既存キーを残したまま新シャードを追加し外部読者を移行後に旧キーを止める、(3) 日足統合は P6 の処理順・ガード（K3）として設計書側で決める。今回の PR 単位には入れない。
- evidence: stocks.json 内訳: 普通株以外 717（16.1%）を月水金に Yahoo へ問い合わせ R2 に書く。intra 1 銘柄 496 KB を 5 日分足すために毎回 GET→PUT（≈2.1 GB/run）。月水金は同じ銘柄の日足 chart を 2 回叩く。外部読者: 別リポジトリ（ブレイク検証） README が当該 ETF を参照、別リポジトリ（動画制作用） intraday.py:1,58,71 が intra/{code}.json を直読（本統合で grep 確認）。
- loc_delta: 0（今回は不実施）/ 実施時 +220 | cost: 実施すれば Yahoo −4,300 req/週、R2 転送 −90%、vwap run −25〜40 分。ただし外部読者を壊す
- prod_migration: True | user_decision: VWAP 画面・R2 daily/ から ETF・REIT・PRO を外してよいか（外部読者 2 リポの改修を含む）。『全期間』表示 365 日を維持するか
- depends_on: L-42（build_stocks.py 削除後に生成経路を作り直す） | scouts: KC-19, KCF-VWAP-06, KCF-VWAP-07, KCF-VWAP-08, INV G1/G3/G4

## L-59 [P2/medium/kabulab-cf/perf] zod classic → zod/mini へ移行し Worker バンドルの 1/3（≈480 KB raw / 55 KB gz）を落とす
- action: 20 ファイルの validators を zod/mini の関数形に書き換え、error-handler の ZodError 判定を z.core.$ZodError に。@hono/zod-validator 0.7.6 が mini を受けるか（standard-schema 経由）を 1 ルートで先に確認。
- evidence: wrangler dry-run: 1528 KiB / gz 312 KiB、sourcemap 配分で zod 532,013 B（34.3%）、うち locales 248,851 B（全言語同梱）。z.locales / toJSONSchema 使用 0。プローブ: classic 310,547 B vs mini 15,310 B。
- loc_delta: ±0〜+50 | cost: バンドル −約 480 KB raw / −55 KB gz（推定）。無料プランの $ には影響なし
- prod_migration: False | user_decision: 20 ファイルのバリデータ書き換えの価値を認めるか
- depends_on:  | scouts: KC-11

## L-60 [P2/medium/kabulab-cf/duplication] Yahoo 取込プロキシ 2 系統（/api/ingest/yahoo + /vwap-analysis/api/ingest-fetch）と Yahoo クライアント 2 実装を 1 系統に寄せ、6 サービスの error-handler を src/shared に 1 本化、core-schema の re-export シム 3 本を撤去
- action: scripts/vwap/* を src/shared/yahoo/client.ts の fetchChart 系に寄せ、services/vwap-analysis/app.ts:36 の ingest-fetch と lib/yahoo.ts (148) を削除（YAHOO_PROXY_BASE は同じ値、パスだけ切替）。src/shared/error-handler.ts に createErrorHandler(serviceName) を置き 5 ファイルを削除。src/cron/{daily,universe,monthly}.ts の import を src/shared/db/core-schema.js に向け services/{rsi,swing,fm}/src/db/core-schema.ts のシムを削除。
- evidence: wrangler.toml:9-11 が両ルートを列挙、cookie 取得と 429 判定を lib/yahoo.ts:8-56 と client.ts:98-106,358 が別実装。error-handler は接頭辞のみ差（diff）。シムは `export * from` だけ。
- loc_delta: −260 | cost: Worker ルート −1 本。$±0
- prod_migration: False | user_decision: 
- depends_on: L-41 | scouts: KC-13, KC-15, KC-06(docs lane)

## L-61 [P2/high/kabulab-cf/duplication] 7 ページで重複する layout CSS（33 KB / 142 ルール）と直書き FONT_LINKS を design.ts に統合し、Google Fonts の Noto Sans JP（CSS 474 KB / @font-face 496）を外す
- action: header/nav/footer/table/card の共通ルールを design.ts の SHARED_LAYOUT_CSS に移し、services/{financial-math,rsi-screening,swing-trading}/src/views/layout.ts と otakara app.ts（CSS 23.8 KB）はサービス固有分だけに。FONT_LINKS から Noto Sans JP を外し --font-body をシステムスタックに（Space Grotesk / JetBrains Mono の要否も同時判断）。rsi の screening-view.test.ts / stock-detail-view.test.ts の markup 検査を更新。
- evidence: レンダ後 HTML の <style> 107 KB のうち 33 KB が 2 ページ以上で同一、`.top-header{` を 7 ファイルが各自定義、design.ts を使うのは ir-catalog・yuho のみ。fonts.googleapis.com css2 応答 474,048 B（gz 122 KB）、Noto 除外で 15,304 B。
- loc_delta: −500〜−1,000 | cost: HTML −3〜10 KB/ページ、外部 CSS −459 KB(gz −107 KB)/ページ、バンドル −100〜200 KB（推定）。$±0
- prod_migration: False | user_decision: 見た目の微差と和文フォント変更を許容するか
- depends_on:  | scouts: KC-14, KCF-WEB-10, KCF-WEB-11

## L-62 [P3/low/kabulab-cf/perf] SSR 一覧ページに Cache-Control を付け、静的アセットを public/_headers で長期キャッシュに、vwap フロントの lightweight-charts を unpkg から public/ 同梱に
- action: 一覧/ホームに public, max-age=300、詳細に 60。public/_headers で /vwap-analysis/data/* と app.js に max-age=86400。lightweight-charts.standalone.production.js を public/vwap-analysis/vendor/ に置き相対参照。
- evidence: SSR HTML に Cache-Control 無し（実測ヘッダ）、静的は max-age=0 must-revalidate（stocks.json 253 KB を毎回再検証）。unpkg から 163,551 B を毎回取得（第三者 CDN が単一障害点）。*.workers.dev では Cache API 無効（未確認）。
- loc_delta: +16（+164 KB 静的ファイル） | cost: 再訪の Worker 呼び出しと D1 読取が減る（量は未測定）。$±0
- prod_migration: False | user_decision: personal-only 値を含むページに public キャッシュを付けてよいか（現状公開面は EDINET 由来のみの前提）
- depends_on:  | scouts: KCF-WEB-12, KCF-WEB-13

## L-63 [P1/low/kabulab-cf/workflow] CI/テスト衛生: vitest の 2 回実行を 1 回に、テストの migration 適用を番号付き SQL に限定、lint 98 warnings をゼロに
- action: ci.yml:40,72 を `vitest run --reporter=default --reporter=json --outputFile=` 1 回にし skip 報告は JSON を読む。drizzle/d1/*.sql を readdirSync で全適用するテスト（services/yuho-quant/src/tests/active-equity-universe.test.ts:41-46 ほか）の filter を /^\d{4}_.*\.sql$/ にしヘルパ 1 本化。no-console 79 は logger ヘルパ経由で対象外ディレクトリ限定、no-explicit-any 19 は型付け、warning をエラー扱いに。
- evidence: run 34783158408: test 19 秒 + skip 報告 18 秒（job 62 秒の 30%）。ローカル vitest は 9 ファイル/59 テストが git-ignore の _cutover-*.sql（0012 の後に流れる）で赤（CI は緑）。lint 0 errors / 98 warnings。
- loc_delta: −3 / +1 / ±40 | cost: CI −18 秒/run。$±0
- prod_migration: False | user_decision: test:coverage（@vitest/coverage-v8）を残すか
- depends_on:  | scouts: KC-20, KCF-CI-09, KCF-TEST-16, KCF-LINT-17, KC-21

## L-64 [P3/medium/kabulab-cf/comment-noise] SECTOR_DAILY_PUBLIC_KEY_SINCE と読み側の日付ガードを、swing_sector_daily の 2026-09-14 未満の行（JPX キー）を DELETE した上で撤去
- action: swing_sector_daily の date < '2026-09-14' を DELETE（現在の最新日付は未確認）→ public-columns.ts:112-157 の定数と 45 行の根拠、swing pages.ts:45-70 の日付比較、sector-ranking-key-switch.test.ts の日付 it を撤去（PUBLISH_JPX_DERIVED_COLUMNS 自体は残す）。
- evidence: cron は当日分を delete→insert するだけで過去日を残す（daily.ts:704 付近）。定数は「2026-09-14 以降の行だけ出す」ための一時ガード。
- loc_delta: −140 | cost: D1 サイズ微減
- prod_migration: True | user_decision: swing_sector_daily の 2026-09-14 未満の行を削除してよいか
- depends_on:  | scouts: KC-02(docs lane), INV C1

## L-65 [P1/low/kabulab-cf/stale-doc] ADR-0001 を 5 行要約にして削除し、README / overview / portal / deploy / scripts README / drizzle README / new-project-template の経緯・誤記・重複を直す
- action: ADR (304) → overview 冒頭 5 行。README: 運用ステータス／残タスク節（L118-170）削除、ディレクトリ構成を overview に一本化、vwap「平日 08:00」→月水金、「GitHub Actions 3 本」→4、「全9サービス」→7、「private 2,000 min」削除、「exclude 5 ファイル/型エラー 33 件」「db:push:finmath」削除。overview: 件数（17/7986→書かない）、DB 木に p_momentum・yuho_overseas_facts を追加、削除表の表・2026-04 経緯削除。000-portal を 60 行に。deploy-cloudflare の PR #1・2,000 min 記述削除、Secrets 表はここだけに。drizzle/d1/README を 35 行に（0011/0012 は適用済み、<ローカルの退避先> パス削除）。new-project-template の旧 PG・P5-b・絶対パス・§10 削除。scripts/README の migrate/・Neon 前置き削除。wrangler.toml:6-13 / stock-sync.yml:16 / vwap-ingest.yml:13,61 の「無料プラン」「private 2,000 分」「日次 40〜50 分」を実態（Paid $5、PUBLIC）に。
- evidence: ADR ステータス「完了」だが §3.3 の正規化計画（macro_market_context・VIEW 化）は未実施のまま残る。vwap-ingest.yml:29 は `0 8 * * 1,3,5`。本番実測 yutai_genres 16 / yutai_benefits 8,295、p_momentum 有り。tsconfig exclude は空。TARGET-ARCHITECTURE §9.1: Workers Paid 確定（本統合では whoami 権限不足で未再確認）。
- loc_delta: 約 −900 | cost: $±0
- prod_migration: False | user_decision: ADR を履歴として残す方針か（git 履歴には残る）
- depends_on: L-39 | scouts: KD-01, KD-04, KD-05, KD-13, KD-14, KD-15, KD-16, KD-17, KCF-DOC-14, D1-23, KC-17

## L-66 [P1/low/kabulab-cf/stale-doc] docs/00N（rsi/otakara/swing/financial-math/yuho/ir-catalog）の関数名誤り・PR 記録・未追随機能を直し、ci-typecheck-blind-spots.md を 30 行に、rsi docs/ と final-quality-report.md を削除
- action: 002: L200-294 の PR 記録節（テストが固定）と経緯 2 節を削除、「統一 cron at root」（存在しない）・scoreAllStocks（不在）・母集団 1,616→書かない。003: syncMarketContext（不在、実体 fetchMarketContextDraft/persistMarketContext）を訂正、2026-04 経緯削除。004: finmath キャッシュ記述を現行に、保持 100/90 日の矛盾を 90 に。005: 海外売上（yuho_overseas_facts・/overseas・backfill:overseas）を追記、backfill 無効化/Phase 3 記述を現行に。006/ir CLAUDE.md: Neon 期・旧フラット DB 経緯削除、ツリー修正。001: 経緯と層別実測を削り残課題 5 行。blind-spots.md 235→30（lint 死角と確認手順のみ）。services/rsi-screening/docs/ (276) と services/otakara-yutai/docs/final-quality-report.md (85) を削除。
- evidence: grep: /api/cron/sync-monthly は root に 0 件、syncMarketContext 0 件。yuho_overseas_facts は本番に存在するが docs/005（最終更新 2026-06-23）に無い。blind-spots.md「残っている除外 1 ファイル」は tsconfig exclude 空で虚偽。rsi docs は overview §DB 設計と同内容。
- loc_delta: 約 −1,000 | cost: $±0
- prod_migration: False | user_decision: docs/001 の残課題（annual_financials 15,963 行の再構築と連結売上の供給源）を文書に残すか Issue へ移すか
- depends_on:  | scouts: KD-02, KD-03, KD-06, KD-07, KD-08, KD-09, KD-10, KD-11, KD-12, KC-17

## L-67 [P2/medium/kabulab-cf/duplication] サービス別 CLAUDE.md / README.md / docs/00N の三重化を 1 本に寄せ、誤記 6 箇所を訂正。CLAUDE.md ルール4/5 と AGENTS.md の .claude/agents 参照を現行運用に合わせる
- action: 共通節（技術スタック・デザイン・Git 規約・コマンド）は root CLAUDE.md / overview へのリンクに置換し、各サービス文書は固有ルール・スキーマ・ルートだけに。誤記: rsi「優良株 = 営業利益率が過去 3 年で上昇基調」→ TTM ≥ 5%（blue-chip.ts:35）、「RSI履歴」表（削除済み）、swing「統合テストはローカル D1 で実データ」（存在しない）、「カバレッジ 80%」→ vitest.config 70、ツリーの不在ファイル。CLAUDE.md ルール4（.claude/agents 不在）・ルール5（push 必須 vs PR 運用）・ルール7 末尾の経緯注、AGENTS.md L10-11,32-35 を書き直す。
- evidence: wc: CLAUDE.md 6 本 789 行、README 4 本 424 行、docs/00N 7 本 1,359 行、md 総行数 5,374。AGENTS.md:28 は「サービス別 AGENTS.md は置かない」と二重管理回避を宣言。ls .claude/agents → No such file。#30/#31 は PR マージ運用。
- loc_delta: 約 −1,100〜−1,500 | cost: $±0
- prod_migration: False | user_decision: サービス別 README.md を CLAUDE.md に統合してよいか。ルール4（エージェント精査）とルール5（push 必須）をどう書き直すか。new-project-template.md を残すか
- depends_on: L-65 | scouts: KD-18, KD-19, KC-18

## L-68 [P1/low/kabulab-cf/comment-noise] コード内の経緯コメント（決定記録・実測・採らなかった案・PR 番号）約 1,000 行を現在形の規則に圧縮し、実装と食い違うコメント 6 箇所と死 URL の User-Agent を訂正
- action: active-equity.ts（−90: 決定日・実測・承認待ちを削り述語と WHERE/ON 規則を残す）、daily.ts（−100: adj 経緯・P5-b・実測表。「JOIN せず id 集合を先に引く」3 行は残す）、otakara app.ts（−60）、universe.ts（−70: 分母を equity に絞る理由は 10 行に）、core-schema.ts（−45: annual_financials の欠陥 2 点は残す）、public-columns.ts（−80）、projection-schema.ts（−60: 恒久化しない 3 条件は残す、存在しない docs/TARGET-ARCHITECTURE.md 参照を stockStock パスに）、price-cache/fm pages（−95）、swing pages（−50）、jpx/yahoo/blue-chip（−110）、workflows・wrangler・drizzle.d1.config・tsconfig・eslint（−110: 「これまで 14 workflow に if: failure() も 0 行」同文 3 箇所等）、テスト内経緯（−90）。誤り訂正: monthly.ts:1-10「Workers Cron / POST /admin/sync-monthly」→ Node・GitHub Actions、yuho-edinet.ts:8「Phase 3」「Neon 日次」、rsi index.ts:14-16 / swing api.ts:54-57 / swing index.ts:15 / otakara app.ts:156-157「root app /api/cron/sync-*」（存在しない）、dataset.ts「Vercel 300s」「Postgres が正本」。UA `kabulab-ir-catalog/1.0 (+https://kabulab.vercel.app)` 3 箇所（tdnet/client.ts:66, pages.ts:138, dataset.ts:452）を workers.dev/GitHub URL に。
- evidence: git 追跡 316 ファイルにコメント 9,721 行、経緯パターン一致 582 行、上位 20 で削減 ≈840 行。grep -i 'vercel|neon|api/cron/sync' 45 行。root に /api/cron ルートは 0 件（auth.test.ts の URL 文字列のみ）。
- loc_delta: 約 −1,100 | cost: $±0（UA 訂正で TDnet/Notion に死 URL を名乗らなくなる）
- prod_migration: False | user_decision: active-equity.ts の「承認待ち」2 点（instrument_type を述語に使う承認、公開面に ETF/REIT を出さない）に回答があれば記述を消せる
- depends_on: L-64（public-columns の定数撤去と同時なら二度手間が減る） | scouts: KC-16, KC-01/03/04/05/07/08/09/10/11/12/13(docs lane), INV-C4

# INVARIANTS
# 壊してはいけない不変条件（全レーンの must_keep を統合。テストが固定しているものは削除せず「規則 + テスト名」に縮める）

## A. 言語横断の契約
- A1 tests/fixtures/contracts/stock-code-vectors.json は両リポで同一バイト列（11,892 B）。両 ci.yml cross-repo-contract が相手 main を checkout して diff。移動・改名も失敗。
- A2 d1-license-map.json は stockStock のみ（kabulab-cf 不在、pending-peer 猶予中）。生成元は schema.MIXED_LICENSE_COLUMNS(16 列)/governance.TABLE_LICENSE(29 表)/WRITER_CLAIMS(18)。test_governance.TestSharedContract が等号照合。
- A3 銘柄コード正準形 ^[0-9]{3}[0-9A-Z]$ の実装は contracts/stock_code.py と kabulab-cf stock-code.ts の 2 箇所のみ。既知例外 worker routes.ts isValidCode はテスト固定。5 文字→4 文字は末尾 0 のときだけ（margin_code_to_key、TestFiveCharCodeToKey、kabulab margin.test.ts）。
- A4 kabulab-cf CI: pnpm db:generate:d1 後に drizzle/d1 が clean。drizzle-kit push を D1 に使わない。0010 は本番へ流さない。本番に d1_migrations 表は無い（適用は手動）。
- A5 test_fixture_policy.ALLOWED 以外の fixtures 追跡禁止（JPX/JSF/TDnet/Yahoo は gitignore、EDINET は追跡可）。
- A6 src の DESIGN.md §番号参照 528 箇所（§3-1 89 / §5.2 69 …）は触らず、文書側で ID を維持する。

## B. ライセンス境界（宣言 = stockStock）
- B1 MIXED_LICENSE_COLUMNS は allowlist。sector(JPX personal-only) と sector33(EDINET commercial-ok) は別出所で統合禁止。id/is_active/is_yutai/created_at/updated_at は意図的に未宣言。
- B2 TABLE_LICENSE は本番 29 表を全登録（OBSERVED_TABLE_COUNT=29）。未知の表 = warning、宣言にあって本番に無い表 = failure（Issue）。表を足す/消すときは TABLE_LICENSE / RETIRED_TABLES / CHILD_TABLES / datasets.py / d1-license-map.json（両リポ）/ tests PROD_TABLE_NAMES を同じ PR 群で動かし、stockStock 側を先に main へ。
- B3 WRITER_CLAIMS 18 件。column_group 語彙 all/base/enrich は改名不可。core_stocks/base = kabulab-cf は動かさない。照合は投入→読み直し→照合の順。CLAIM_MISMATCH_IS_FAILURE=True。
- B4 jobs/license_map.py が seed_reference_tables の唯一の入口。孤児 column_license は削除、index_symbols の孤児は削除しない。apply_schema を毎日呼ばない。
- B5 LicenseTag 3 値、inherit() は厳しい側、_STRICTNESS の順序は financials.py の SQL でも再現。
- B6 jss_financials PK=(code, fiscal_period_end, disclosure_type, consolidated)、consolidated NOT NULL('不明' 番兵)、値列 COALESCE、disclosed_at ガード、FINANCIALS_PK はリテラル二重持ち（test_cloud_schema）。
- B7 cloud_store/r2.py に delete_object を実装しない。mutable JSON は check_no_regression。

## C. ライセンス境界（実効防御 = kabulab-cf）
- C1 public-columns.ts: PUBLISH_JPX_DERIVED_COLUMNS=false は 1 箇所のみ、publicMarketColumn=NULL、publicSectorColumn=sector33、sector へフォールバックしない。
- C2 core-stocks-license-boundary.test.ts の 5 検査はパス・識別子ベース（PUBLIC_SURFACE 12 ファイル、`*[Ss]tocks` 接尾辞）。公開面ファイルの移動/改名は PUBLIC_SURFACE と source-scan.ts の範囲定数を同時更新。
- C3 activeEquityCondition() は WHERE/ON のみ・select しない、instrument_type の書き手は universe.ts だけ、修飾参照は universe.ts と active-equity.ts だけ。取込は disclosureIngestCondition() (X-05 で改名)。
- C4 yutai_benefits.description は公開面 app.ts で `name="description"` と `g.description` 以外に出さない。src/routes・src/views は存在してはならない。stockStock 側は RESTRICTED_COLUMNS と yutai.EXPORT_COLUMNS。
- C5 業種集計は分母・分子 activeEquityCondition、キー sector33、カバレッジ <90% で書かない、当日分のみ delete→insert。
- C6 notion-archive は ir-catalog 公開ページの fetchPageFileUrl が使う（消すと PDF リンクが死ぬ）。
- C7 公開 GET / 計算 POST は D1 に 1 文も書かない。/emh?type=momentum は p_momentum だけを読む。p_momentum に列を足さない。

## D. 母集団ガード
- D1 assertUniverseCoverage (a)(b)(c)(d1)(d2) と定数 4 つ（universe.test.ts 30 本、test_universe_guards 45 本）。(c) の分母は equity、部分充填は縮退。
- D2 instrument_type 語彙 6 値の正本は instrument-type.ts、equity は isListedEquity と完全一致。
- D3 planInstrumentTypeUpdates は行を増やさない・対象外化行は書かない・全計画後に書く。UPSERT_CHUNK=16。
- D4 JPX 取得は .xlsx（.xls は 404）。月次 rebuild は active∧equity∧is_yutai。

## E. 鮮度監視・運用層
- E1 DATASET_SOURCES キー == SLO_BY_DATASET ∪ NOT_REFRESHED。1 データセット 1 文（MAX/MAX/COUNT AS n、test_ops_slo:267 が "COUNT(" を要求）、UNION 禁止。
- E2 鮮度はデータ基準日で測る。updated_at は実表の epoch であって記録時刻ではない。core_stocks の鮮度 writer は kabulab-cf universe.ts、master_sync は updated_at を進めない。
- E3 ops_check.yml: step id {probe, judge, drift, license}、Issue タイトル完全一致 jq、閉じる条件は 4 outcome AND、cron 30 14 * * *。ops_check.py は何も書かない（runner の jss_job_runs 1 行を除く）。
- E4 G-core-5: FK の無い jss_financials（SOFT）と p_momentum の孤児検査は残す。c.stock_id IS NOT NULL の除外。
- E5 E7 列ドリフト: core_stocks 21 列・索引 3 本を両方向突合。PROTECTED_COLUMNS 8 列は stockStock が SET しない。
- E6 cron 文字列は TestWorkflowCrons.EXPECTED（現在 7 件）で固定。新 cron を足さない。margin_weekly は切替日まで無効。

## F. D1 書込規律
- F1 jss_* は CREATE IF NOT EXISTS、②日足・⑧XBRL ファクト・需給日次の表は持たない。
- F2 バインド 100/文、compound SELECT 5 項、rows_read は SELECT 列に依存しない、JOIN 外側で桁が変わる。新クエリは meta.rows_read を実測。
- F3 書込順序 R2→D1。assertDailySchema で未適用 migration を取得前に検出。
- F4 sector33 充填は差分だけ UPDATE・updated_at を SET しない（test_core_stocks_sector33 30 本、AST で build_column_update 呼び出し 0）。

## G. R2 契約と外部読者
- G1 vwap-data: daily/{code}.json（bars[].date,o,h,l,c,v,adj / splits）、intra/{code}.json（bars[].ts…）、margin/{date}.json（rows はちょうど 5 キー）、weeks.json（素の配列）は「1 バイトも変えない」。/api/daily・/api/intra は素通し、/api/margin は行スプレッド → 運用メタをオブジェクトに入れない。
- G2 margin/ は削除しない、weeks.json を空にしない。r2Delete は呼び出し元 0（stockStock は delete 非実装）。
- G3 daily/ の母集団は public/vwap-analysis/data/stocks.json（4,445 件・全種別・2026-06-18 凍結）であり core_stocks ではない。日経225 連動 ETF 1 本 の更新継続が外部読者（別リポジトリ（ブレイク検証））の前提。
- G4 別リポジトリ（動画制作用） は daily/intra/margin を R2 直読（intraday.py は intra/{code}.json、margin.py は実キー名 ^margin/(\d{4}-\d{2}-\d{2})\.json$ から週を列挙し全キーで DataFrame 化）、D1 REST で core_stocks/core_stock_financials/annual/yuho_*/ir_disclosures/rsi_percentile を読む（列追加は耐える、改名・削除は壊れる）。
- G5 jp-stock-supply: supply/{code}.json（schema=1、writer 必須、既存点を落とさない）と backup/yutai/。公開 Worker は bind しない。jp-stock-raw: raw/{source}/{datatype}/{yyyy}/{date}/{scope}/{doc_id}/{sha16}.{ext}。

## H. 公開 Worker / MCP
- H1 jss-api-public は D1 + jp-stock-raw のみ bind、GET/HEAD 以外 405、personal-only は null 化。private は JSS_API_KEYS 未設定で 503。
- H2 MCP jp_supply_latest/jp_supply_series/jp_dataset_freshness/jp_xbrl_elements/jp_raw_file は稼働中（本セッションで応答）。ツール名・封筒を変えない。
- H3 kabulab-cf の /api/ingest/yahoo は CRON_SECRET + ホスト allowlist、Node 同期は YAHOO_PROXY_BASE 必須。Yahoo 429 サーキットブレーカと stockStartGate。

## I. Notion
- I1 DB_REGISTRY 7 キー、dry-run は書かない、⑤原本アップロード必須、冪等キー。RECOMMENDED_VIEWS が参照するプロパティ名（ROE%・開示種別・33業種・開示日時・ライセンスタグ）は改名しない。
- I2 upsert.py の「最古を正・作成直後 1 回だけ再検索・自分のページだけ archive」(#13)・「開示日時が新しい既存行は上書きしない」(#14)、④日付窓マップの JST 境界、query 10,000 件打切り検知。
- I3 Notion ① の 3 プロパティ（現行履歴DB ID 等）はコードを消しても本番 DB から消さない（放置で無害）。

## J. 恒久制約
- J1 新 cron/workflow 禁止、Workers Paid 機能・Workers Cron 不使用、両リポ PUBLIC のまま、失敗通知は GitHub Issue 1 本。
- J2 JPX・Yahoo・日証金 = personal-only、みんかぶ = no-store、TDnet = factual-cite、EDINET = commercial-ok。公開面の業種は EDINET 33 業種。
- J3 ダミー禁止・フォールバック禁止（?? default / catch{return default} は規約違反）。
- J4 P4b/P5/P6 の前提（fin_parity の閾値、writeCoreFinancials フラグ、P6 の処理順と「対象コード ⊇ R2 既存 daily/ キー」検査、P2 の 2026-09-28 切替）はリファクタで先取りしない。