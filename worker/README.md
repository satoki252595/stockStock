# jss-api — 配信 Worker

正本（R2 + D1）を REST と MCP で読むための Cloudflare Worker。**読み取り専用**
（唯一の例外は内部面の鮮度 upsert）。設計は [docs/CF-CANONICAL-DESIGN.md](../docs/CF-CANONICAL-DESIGN.md)。

## Worker は2本

| Worker | 用途 | bind | 認証 |
|---|---|---|---|
| `jss-api-public` | 公開 REST。`commercial-ok` 全量と `factual-cite` のメタのみ | `DB` / `RAW` | 無認証 |
| `jss-api-private` | 内部 REST + **MCP**。`personal-only` を含む全量 | `DB` / `RAW` / `SUPPLY` | APIキー必須 |

**公開面に `jp-stock-supply` と `vwap-data` を bind していないのは意図的**で、
コードにバグがあっても personal-only のオブジェクトへ物理的に到達できないようにする
第0層の防御。`test/bindings.test.ts` が設定ファイルごと固定している。

`kabulab-cf`（7サービスの SSR 配信）にも `kabuMCP`（Stripe/Cookie 認可を持つ課金
Worker）にも相乗りしない。D1 は1個のまま両方が読むだけで、データは複製しない。

## 防御の層

| 層 | 内容 |
|---|---|
| L0 | 公開 Worker が personal-only の R2 を bind しない（物理的到達不能） |
| L1 | 公開面は GET / HEAD 以外を 405 で拒否（書込面が存在しない） |
| L2 | 行の `license_tag` で判定。**未知のタグは出さない側に倒す** |
| L3 | 列単位の伏字（`core_stocks` の JPX 由来列、`yutai_benefits` の掲載文） |
| L4 | 内部面は `JSS_API_KEYS` 未設定なら 503 で **fail-closed**（素通しにしない） |

## 開発

```bash
nix develop -c bash -c 'cd worker && pnpm install'
nix develop -c bash -c 'cd worker && pnpm typecheck && pnpm test'
```

## デプロイ

```bash
nix develop -c bash -c 'cd worker && pnpm deploy:public'
nix develop -c bash -c 'cd worker && wrangler secret put JSS_API_KEYS -c wrangler.private.jsonc'
nix develop -c bash -c 'cd worker && pnpm deploy:private'
```

`JSS_API_KEYS` はカンマ区切りで複数指定できる。**入れるまで内部面は全リクエストを
503 で拒否する**ので、鍵を入れ忘れたまま personal-only が漏れることはない。

## MCP

内部面の `POST /mcp`（Streamable HTTP・ステートレス）。ツールは `jp_*` 名前空間で、
kabuMCP の `edinet_*` とは分けてある（クライアントに両方登録できるので統合は不要）。

| ツール | 内容 |
|---|---|
| `jp_supply_latest` | 需給の最新断面 |
| `jp_supply_series` | 1銘柄の需給時系列 |
| `jp_dataset_freshness` | 各データセットの鮮度 |
| `jp_xbrl_elements` | XBRL 勘定科目の語彙（前方一致） |
| `jp_raw_file` | ⑤原本のメタを SHA256 で引く |
