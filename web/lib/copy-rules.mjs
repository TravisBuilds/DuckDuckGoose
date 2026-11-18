/**
 * The owner's copy rules for public marketing pages, as data. lib/home-copy.test.mjs holds the home
 * page's copy to them, and tests/home-page-e2e.spec.ts checks the rendered page as well.
 *
 * Words are matched case-insensitively, as whole words, with their other forms (plurals, -ing, -ed),
 * so "Creators", "unlocking" and "AI powered" are caught too.
 */

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

/** Every rule a text breaks: [{ rule, match }] (empty when the text is clean). */
export function copyProblems(text) {
  const problems = [];
  for (const { word, pattern } of BANNED_WORDS) {
    const hit = String(text).match(pattern);
    if (hit) problems.push({ rule: `banned word "${word}"`, match: hit[0] });
  }
  for (const { topic, pattern } of OFF_LIMITS) {
    const hit = String(text).match(pattern);
    if (hit) problems.push({ rule: `no ${topic} section`, match: hit[0] });
  }
  return problems;
}
