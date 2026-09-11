/** 全エンドポイント共通のレスポンス封筒。 */

export interface Envelope<T> {
  data: T;
  meta: {
    /** 次ページのカーソル。無ければ null。 */
    cursor: string | null;
    /** この応答に含まれる行のライセンスタグ（重複なし）。 */
    licenses: string[];
    /** 出典表記。personal-only は公開面に出ないのでここにも現れない。 */
    attribution: string[];
  };
}

export const ATTRIBUTION: Record<string, string> = {
  EDINET: "出典: EDINET（金融庁）。本データは EDINET 公表情報を編集・加工して作成。",
  TDnet: "出典: TDnet（東京証券取引所 適時開示情報閲覧サービス）。原文は各社開示資料。",
  JPX: "出典: 日本取引所グループ（JPX）統計情報。私的利用に限定し再配布しない。",
  日証金: "出典: 日本証券金融（JSF）貸借取引情報。私的利用に限定し第三者へ提供しない。",
};

export function envelope<T>(
  data: T,
  options: { cursor?: string | null; sources?: Iterable<string>; licenses?: Iterable<string> } = {},
): Envelope<T> {
  const sources = [...new Set(options.sources ?? [])].filter(Boolean);
  return {
    data,
    meta: {
      cursor: options.cursor ?? null,
      licenses: [...new Set(options.licenses ?? [])].filter(Boolean).sort(),
      attribution: sources.map((s) => ATTRIBUTION[s]).filter((s): s is string => Boolean(s)),
    },
  };
}

export function errorBody(message: string, code: string) {
  return { error: { code, message } };
}
