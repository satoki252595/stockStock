/** Worker のバインディング。公開面は SUPPLY を持たない（型でも表現する）。 */
export interface PublicEnv {
  DB: D1Database;
  RAW: R2Bucket;
  SURFACE: "public";
}

export interface PrivateEnv {
  DB: D1Database;
  RAW: R2Bucket;
  SUPPLY: R2Bucket;
  SURFACE: "private";
  /** カンマ区切りの APIキー。未設定なら fail-closed で全拒否する。 */
  JSS_API_KEYS?: string;
}

export type AnyEnv = PublicEnv | PrivateEnv;
