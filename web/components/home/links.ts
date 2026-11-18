/**
 * Where the home page's links go. All of them open the studio: middleware lets a signed-in visitor
 * straight in and sends anyone else to /login?from=%2Fstudio, which returns them to the studio once
 * they sign in.
 *
 * TODO(early-access): there is no public sign-up or waitlist yet (accounts come from admin invites,
 * see app/set-password), so "Get early access" leads to the sign-in page for now. Point
 * EARLY_ACCESS_HREF at the request-access page once one exists.
 */
export const STUDIO_HREF = '/studio';
export const EARLY_ACCESS_HREF = STUDIO_HREF;
