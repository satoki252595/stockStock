/**
 * 公開面の列マスクが列単位ライセンス地図から離れないようにする。
 *
 * `worker/` は Python パッケージと別ビルドなので
 * `cloud_store/schema.py` の `MIXED_LICENSE_COLUMNS` を import できない。
 * そのため正本の地図は共有契約ファイル
 * `tests/fixtures/contracts/d1-license-map.json` の `column_license` を
 * ここで読み、`RESTRICTED_COLUMNS` と等号で突き合わせて固定する
 * (`stock-code-contract.test.ts` と同手法)。
 *
 * これが無いと、地図だけが更新されて公開面のマスクが古いまま
 * （personal-only の列が出る）/ 逆に commercial-ok の列を伏せたまま、
 * 気づかれずに残る。
 */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { PERSONAL_ONLY, RESTRICTED_COLUMNS } from "../src/shared/license";

const MAP_PATH = new URL(
  "../../tests/fixtures/contracts/d1-license-map.json",
  import.meta.url
);
const contract = JSON.parse(readFileSync(MAP_PATH, "utf8")) as {
  column_license: Record<string, Record<string, string>>;
};

describe("RESTRICTED_COLUMNS は列単位ライセンス地図と一致する", () => {
  it("core_stocks の personal-only 集合が地図と一致する", () => {
    const columns = contract.column_license.core_stocks;
    expect(columns, "地図に core_stocks が無い").toBeDefined();
    const masked = Object.entries(columns)
      .filter(([, tag]) => tag === PERSONAL_ONLY)
      .map(([column]) => column)
      .sort();
    expect([...RESTRICTED_COLUMNS.core_stocks].sort()).toEqual(masked);
  });
});
