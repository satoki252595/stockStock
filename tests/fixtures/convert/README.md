# tests/fixtures/convert — 変換レイヤー（バンドルE）実レスポンスフィクスチャ

すべて**実エンドポイントから取得した実レスポンスの保存物**である（捏造禁止 DESIGN.md §3-6）。
再取得は `uv run --no-sync python scripts/capture_convert_fixtures.py`。

| ファイル | 取得元 | 取得日 | ライセンス上の扱い |
|---|---|---|---|
| `yanoshin_tdnet_recent.json` | `https://webapi.yanoshin.jp/webapi/tdnet/list/recent.json?limit=5`（やのしんTDnet WEB-API・公開） | 2026-06-10 | 開示メタデータ。テスト用内部保管のみ（個人運営APIのため依存しない設計 §2.1） |
| `tdnet_disclosure_sample.pdf` | 上記一覧の `document_url`（`www.release.tdnet.info`）。実際の出所は `tdnet_disclosure_sample.source.txt` を参照 | 2026-06-10 | **factual-cite** (§2.1)。開示資料の著作権は開示会社に帰属。**内部保管・テスト検証のみ**。再配布・転載はしない |
| `data_j.xls` | `https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls`（JPX 上場銘柄一覧） | 2026-06-10 | **personal-only** (§2.1)。JPX サイト利用規約により商用目的のデータ収集・二次利用は不可。**私的検証用フィクスチャ**であり、公開・再配布・商用利用・商用トラックへの混入を禁止する |

注意:

- 各ファイルは取得時のバイト列を無加工で保存している（値不変 §5.2 の検証に使うため）
- フィクスチャが存在しない環境では、該当テストは fail ではなく skip する
  （`tests/conftest.py` の `fixture_path()`）
