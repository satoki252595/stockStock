/**
 * ライセンス判定。公開面に出してよいものを**列レベル**で決める。
 *
 * 行の license_tag 1列では足りない。core_stocks は EDINETコードリスト由来
 * (commercial-ok) と JPX data_j.xls 由来 (personal-only) を1行に混在させており、
 * 行単位で許可すると market/sector33 まで公開面に出てしまう。
 */

export const COMMERCIAL_OK = "commercial-ok";
export const FACTUAL_CITE = "factual-cite";
export const PERSONAL_ONLY = "personal-only";

/** 公開面に「全量」出してよいタグ。 */
export function isPublishableInFull(tag: string | null | undefined): boolean {
  return tag === COMMERCIAL_OK;
}

/** 公開面に「メタデータと原典リンクだけ」出してよいタグ。 */
export function isPublishableAsMetadata(tag: string | null | undefined): boolean {
  return tag === COMMERCIAL_OK || tag === FACTUAL_CITE;
}

/**
 * 公開面に出してはいけない行か。
 * **未知のタグは出さない側に倒す**（新しいソースを足したときに既定で漏れない）。
 */
export function isRestricted(tag: string | null | undefined): boolean {
  return !isPublishableAsMetadata(tag);
}

/**
 * 公開面で伏せる列。値を null に潰し、キー自体は残す
 * （消すとクライアントが「その列が存在しない」と誤解するため）。
 */
export const RESTRICTED_COLUMNS: Record<string, readonly string[]> = {
  // JPX data_j.xls 由来。EDINETコードリスト由来の列とは出自が違う。
  core_stocks: ["market", "sector", "sector33", "sector17", "instrument_type"],
  // みんかぶ掲載文。規約上、取得も公開も不可（TDnet 由来へ移行済み）。
  yutai_benefits: ["description", "short_summary", "estimated_value"],
};

export function redactColumns<T extends Record<string, unknown>>(
  table: string,
  row: T,
): T {
  const restricted = RESTRICTED_COLUMNS[table];
  if (!restricted) return row;
  const out = { ...row } as Record<string, unknown>;
  for (const column of restricted) {
    if (column in out) out[column] = null;
  }
  return out as T;
}
