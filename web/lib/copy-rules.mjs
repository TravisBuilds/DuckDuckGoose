/**
 * The owner's copy rules, as data. The home page keeps to all of them (lib/home-copy.test.mjs,
 * tests/home-page-e2e.spec.ts); sign-in, the password page, the studio and the shared header and
 * footer keep to the banned words and draw no icons (lib/app-copy.test.mjs, tests/app-pages-e2e.spec.ts),
 * and may talk about prices.
 *
 * Words are matched case-insensitively, as whole words, with their other forms (plurals, -ing, -ed),
 * so "Creators", "unlocking" and "AI powered" are caught too.
 */

/**
 * Emoji, arrows, check marks, crosses, stars, play triangles and other symbols that would stand in for
 * icons, and the private-use characters icon fonts draw with.
 */
export const ICON_CHARACTERS = /[\p{Emoji_Presentation}\u2190-\u21FF\u2300-\u23FF\u25A0-\u27BF\u27F0-\u27FF\u2900-\u297F\u2B00-\u2BFF\uE000-\uF8FF\uFE0F]/u;

/** Words that never appear in the copy. */
export const BANNED_WORDS = [
  { word: 'creators', pattern: /\bcreators?\b/i },
  { word: 'IP', pattern: /\bI\.?P\b/i },
  { word: 'harness', pattern: /\bharness(?:es|ed|ing)?\b/i },
  { word: 'co-pilot', pattern: /\bco[\s\u2010-\u2015-]?pilot(?:s|ing|ed)?\b/i },
  { word: 'content', pattern: /\bcontents?\b/i },
  { word: 'unlock', pattern: /\bunlock(?:s|ed|ing|able)?\b/i },
  { word: 'leverage', pattern: /\bleverag(?:e|es|ed|ing)\b/i },
  { word: 'platform', pattern: /\bplatforms?\b/i },
  { word: 'synergy', pattern: /\bsynerg(?:y|ies|istic)\b/i },
  { word: 'AI-powered', pattern: /\bAI[\s\u2010-\u2015-]*powered\b/i },
  { word: 'revolutionize', pattern: /\brevolution(?:i[sz](?:e|es|ed|ing)|ary)\b/i },
];

/** Sections the home page leaves out: pricing, investors, founders and "contact us". */
export const OFF_LIMITS = [
  { topic: 'pricing', pattern: /\bpric(?:e|es|ed|ing)\b|[$€£]\s?\d|\bper month\b|\bfree trial\b|\bsubscri(?:be|ption)s?\b/i },
  { topic: 'investors', pattern: /\binvest(?:or|ors|ment|ing)\b|\bfunding\b|\bbacked by\b/i },
  { topic: 'founders', pattern: /\bfounders?\b|\bfounded\b/i },
  { topic: 'contact us', pattern: /\bcontact\b|\bget in touch\b|\breach out\b/i },
];

/** Every banned word a text uses: [{ rule, match }] (empty when it uses none). */
export function bannedWords(text) {
  const problems = [];
  for (const { word, pattern } of BANNED_WORDS) {
    const hit = String(text).match(pattern);
    if (hit) problems.push({ rule: `banned word "${word}"`, match: hit[0] });
  }
  return problems;
}

/** Every rule a text breaks: [{ rule, match }] (empty when the text is clean). */
export function copyProblems(text) {
  const problems = bannedWords(text);
  for (const { topic, pattern } of OFF_LIMITS) {
    const hit = String(text).match(pattern);
    if (hit) problems.push({ rule: `no ${topic} section`, match: hit[0] });
  }
  return problems;
}
