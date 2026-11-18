import { readFileSync } from 'node:fs';
import path from 'node:path';
import type { Page } from '@playwright/test';
import { test, expect } from './e2e-cleanup';

/**
 * The public home page against the real dry-run backend (playwright.e2e.config.ts). / is the same
 * static page for everyone, with its own header and footer. A signed-out visitor's "Get early access"
 * leads to sign-in and then into the studio; a signed-in member gets from the home page into the
 * studio in one click, without the sign-in form, and logging out lands back on the home page. At 1440,
 * 1024 and 390 px the page fits the screen, keeps its cards and photos off the text, has one h1,
 * describes every photo, shows the focus ring and says none of the words the copy rules ban.
 */

const MEMBER = { email: 'e2e-member@test.local', password: 'e2e-member-password' };
const SECTIONS = ["It's already happening", 'We start with video', 'Be the star of your own story', 'From video to everything'];

// lib/copy-rules.mjs is an ES module with no imports; the e2e job's Node 20 cannot require one from
// this compiled spec, so its source is evaluated here.
const { copyProblems } = new Function(
  `${readFileSync(path.join(__dirname, '..', 'lib', 'copy-rules.mjs'), 'utf8').replace(/^export /gm, '')}\nreturn { copyProblems };`,
)() as { copyProblems: (text: string) => { rule: string; match: string }[] };

async function signIn(page: Page, who: { email: string; password: string }) {
  await page.locator('#email').fill(who.email);
  await page.locator('#password').fill(who.password);
  await page.getByRole('button', { name: 'Sign In', exact: true }).first().click();
}

/** Scrolls the whole page so every lazy photo loads, then waits for them. */
async function loadEveryPhoto(page: Page) {
  await page.evaluate(async () => {
    for (let y = 0; y < document.body.scrollHeight; y += 400) {
      window.scrollTo(0, y);
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    window.scrollTo(0, 0);
  });
  await expect.poll(() => page.evaluate(() => [...document.images].every((img) => img.complete && img.naturalWidth > 0))).toBe(true);
}

test('a signed-out visitor gets the home page, and "Get early access" leads to sign-in, then into the studio', async ({ page }) => {
  const errors: string[] = [];
  page.on('console', (message) => { if (message.type() === 'error') errors.push(message.text()); });
  page.on('pageerror', (error) => errors.push(error.message));

  const response = await page.goto('/');
  expect(response?.status()).toBe(200);
  await expect(page).toHaveURL('/');
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Your life, back');
  await expect(page.getByRole('heading', { level: 2 })).toHaveText(SECTIONS);

  // the home page's own header and footer, not the shared ones
  await expect(page.getByRole('banner')).toHaveCount(1);
  await expect(page.getByRole('contentinfo')).toHaveCount(1);
  await expect(page.getByRole('link', { name: 'Archetypes' })).toHaveCount(0);
  await expect(page.getByTestId('footer-coming-soon')).toBeVisible();
  await expect(page.getByTestId('footer-coming-soon')).not.toContainText('Contact');

  // one call to action, and every way in goes through the studio's sign-in
  const ctas = page.getByRole('link', { name: 'Get early access' });
  await expect(ctas).toHaveCount(3);
  for (const cta of await ctas.all()) await expect(cta).toHaveAttribute('href', '/studio');
  for (const signInLink of await page.getByRole('link', { name: 'Sign in' }).all()) {
    await expect(signInLink).toHaveAttribute('href', '/studio');
  }
  await loadEveryPhoto(page);
  expect(errors, 'the home page logs no errors').toEqual([]);

  await page.getByRole('main').getByRole('link', { name: 'Get early access' }).first().click();
  await expect(page).toHaveURL('/login?from=%2Fstudio');
  await signIn(page, MEMBER);
  await expect(page).toHaveURL(/\/studio$/);
  await expect(page.getByTestId('me-role')).toHaveText('Member');
});

test('a signed-in member gets the same home page and reaches the studio in one click; logging out lands on the home page', async ({ page }) => {
  await page.goto('/login');
  await signIn(page, MEMBER);
  await expect(page).toHaveURL(/\/studio$/);

  await page.goto('/');
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Your life, back');
  await expect(page.getByTestId('footer-coming-soon')).toBeVisible();

  await page.getByRole('banner').getByRole('link', { name: 'Sign in' }).click();
  await expect(page).toHaveURL(/\/studio$/);
  await expect(page.getByTestId('me-role')).toHaveText('Member');
  await expect(page.locator('#password')).toHaveCount(0);

  await page.goBack();
  await expect(page).toHaveURL('/');
  await page.getByRole('main').getByRole('link', { name: 'Get early access' }).first().click();
  await expect(page).toHaveURL(/\/studio$/);
  await expect(page.getByTestId('me-role')).toHaveText('Member');

  await page.getByTestId('studio-menu-button').click();
  await page.getByRole('button', { name: 'Log out' }).click();
  await expect(page).toHaveURL('/');
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Your life, back');
  await page.goto('/studio');
  await expect(page).toHaveURL('/login?from=%2Fstudio');
});

for (const [width, height] of [[1440, 900], [1024, 768], [390, 844]]) {
  test(`at ${width} px the home page fits the screen, covers none of its text, describes every photo, shows the focus ring and keeps to the copy rules`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    await page.goto('/');
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    await loadEveryPhoto(page);

    const layout = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
      h1s: document.querySelectorAll('h1').length,
      photos: [...document.images].map((img) => ({ alt: img.alt, src: img.currentSrc })),
    }));
    expect(layout.scrollWidth, 'nothing sticks out sideways').toBeLessThanOrEqual(layout.innerWidth);
    expect(layout.h1s).toBe(1);
    expect(layout.photos).toHaveLength(5);
    for (const photo of layout.photos) {
      expect(photo.alt.split(' ').length, `${photo.src} has alt text`).toBeGreaterThanOrEqual(6);
      expect(photo.src, 'Chromium gets the AVIF').toMatch(/\/home\/[a-z]+-\d+\.avif$/);
    }

    // the cards overlap the photos on purpose, never the words: whatever is drawn at the start, middle
    // and end of every line of text is that text. (window.scrollTo, not scrollIntoView, which would
    // also move where the next Tab starts from.)
    const covered = await page.evaluate(() => {
      const problems = new Set<string>();
      for (const element of document.querySelectorAll('main :is(h1, h2, h3, p, a), footer :is(p, a, span)')) {
        const box = element.getBoundingClientRect();
        window.scrollTo({ top: window.scrollY + box.top + box.height / 2 - window.innerHeight / 2, behavior: 'instant' });
        const range = document.createRange();
        range.selectNodeContents(element);
        for (const line of range.getClientRects()) {
          if (line.width < 4 || line.height < 4) continue;
          for (const x of [line.left + 2, line.left + line.width / 2, line.right - 2]) {
            const top = document.elementFromPoint(x, line.top + line.height / 2);
            if (top && !element.contains(top) && !top.contains(element)) {
              problems.add(`"${element.textContent?.trim().slice(0, 40)}" is under <${top.tagName.toLowerCase()} class="${top.getAttribute('class') ?? ''}">`);
            }
          }
        }
      }
      window.scrollTo({ top: 0, behavior: 'instant' });
      return [...problems];
    });
    expect(covered, 'nothing is drawn over the text').toEqual([]);

    const words = await page.evaluate(() => [
      document.title,
      document.querySelector('meta[name="description"]')?.getAttribute('content') ?? '',
      document.body.innerText,
      ...[...document.images].map((img) => img.alt),
    ].join('\n'));
    expect(copyProblems(words)).toEqual([]);

    // the skip link comes first and moves focus to the page; the next stop shows the focus ring
    await page.keyboard.press('Tab');
    const skip = page.getByRole('link', { name: 'Skip to main section' });
    await expect(skip).toBeFocused();
    await expect(skip).toBeInViewport();
    await page.keyboard.press('Enter');
    await expect(page.getByRole('main')).toBeFocused();
    await page.keyboard.press('Tab');
    const heroCta = page.getByRole('main').getByRole('link', { name: 'Get early access' }).first();
    await expect(heroCta).toBeFocused();
    await expect(heroCta).toHaveCSS('outline-style', 'solid');
    await expect(heroCta).toHaveCSS('outline-width', '3px');

    // on a phone the header keeps "Sign in" and leaves the call to action to the page
    const headerCta = page.getByRole('banner').getByRole('link', { name: 'Get early access' });
    if (width < 640) await expect(headerCta).toBeHidden();
    else await expect(headerCta).toBeVisible();
    await expect(page.getByRole('banner').getByRole('link', { name: 'Sign in' })).toBeVisible();
  });
}
