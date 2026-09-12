/**
 * 公開 API の銘柄コード検証が銘柄コード契約から離れないようにする。
 *
 * `worker/` は Python パッケージと別ビルドなので `contracts/stock_code.py` を
 * import できない。そのため正準パターンは共有テストベクタ
 * `tests/fixtures/contracts/stock-code-vectors.json` の `canonical_regex` を
 * ここで読み、`isValidCode` の正規表現リテラルと突き合わせて固定する
 * (docs/CONTRACTS.md 不変条件9 の「既知例外」)。
 *
 * これが無いと、公開面だけが緩いまま (旧実装は `A130` / `ABCD` を通していた)
 * 気づかれずに残る。
 */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { isValidCode } from "../src/shared/routes";

const VECTORS_PATH = new URL(
  "../../tests/fixtures/contracts/stock-code-vectors.json",
  import.meta.url
);
const fixture = JSON.parse(readFileSync(VECTORS_PATH, "utf8")) as {
  canonical_regex: string;
  vectors: { id: string; input: string | null; parse: string | null }[];
};

describe("isValidCode は銘柄コード契約の正準形を使う", () => {
  it("正規表現が共有テストベクタの canonical_regex と一致する", () => {
    // 実装の正規表現リテラルを直接読む (別の定数を挟むと固定が効かない)。
    const src = readFileSync(
      new URL("../src/shared/routes.ts", import.meta.url),
      "utf8"
    );
    const m = src.match(/return \/(\^[^/]+\$)\/\.test\(code\)/);
    expect(m, "isValidCode の正規表現リテラルを見つけられない").not.toBeNull();
    expect(m?.[1]).toBe(fixture.canonical_regex);
  });

  // 正規化はしない面なので、「入力がそのまま正準形のベクタ」だけを対象にする。
  // (小文字・全角・BOM 付きは公開 API では 400 のままが正しい)
  it("入力がそのまま正準形のベクタを受理し、それ以外を拒否する", () => {
    for (const v of fixture.vectors) {
      if (typeof v.input !== "string") continue;
      if (v.parse !== null && v.parse === v.input) {
        expect(isValidCode(v.input), `${v.id} は受理されるべき`).toBe(true);
      }
      if (v.parse === null && /^[0-9A-Z]*$/.test(v.input)) {
        expect(isValidCode(v.input), `${v.id} は拒否されるべき`).toBe(false);
      }
    }
  });

  it("1桁目英字は拒否する (JPX 付番体系に無い)", () => {
    expect(isValidCode("A130")).toBe(false);
    expect(isValidCode("ABCD")).toBe(false);
    expect(isValidCode("130A")).toBe(true);
    expect(isValidCode("7203")).toBe(true);
  });
});
