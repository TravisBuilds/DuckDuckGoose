export declare const SESSION_KEY_CONTEXT: string;
export declare function timingSafeEqual(a: string, b: string): boolean;
export declare function sessionKeyFromEnv(env: Record<string, string | undefined>): Promise<Uint8Array | null>;
export declare function signSessionToken(expiresAt: number | string, nonce: string, key: Uint8Array): Promise<string>;
export declare function verifySessionToken(
  token: string | undefined,
  key: Uint8Array | null,
  nowSeconds?: number
): Promise<{ ok: true } | { ok: false; reason: 'no-key' | 'malformed' | 'bad-signature' | 'expired' }>;
