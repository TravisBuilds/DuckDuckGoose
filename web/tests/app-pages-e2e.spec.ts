import { readFileSync } from 'node:fs';
import path from 'node:path';
import type { Locator, Page } from '@playwright/test';
import { test, expect } from './e2e-cleanup';

/**
 * Sign-in, the password page, the studio and the cost dashboard as rendered, against the real dry-run
 * backend (playwright.e2e.config.ts), at 1440 and 390 px: no icons of any kind (no svg, no symbol
 * characters, nothing drawn with CSS content), none of the words the owner bans in the title, the
 * description, the text or the labels, and the DuckDuckGoose wordmark set in Montserrat. On a phone the
 * studio's tab bar names every tab in words, the raised button says "New", and every one keeps its
 * accessible name.
 */

const MEMBER = { email: 'e2e-member@test.local', password: 'e2e-member-password' };
const ADMIN = { email: 'e2e@test.local', password: 'e2e-test-password' };
const SIZES = [[1440, 900], [390, 844]] as const;

// lib/copy-rules.mjs is an ES module with no imports; the e2e job's Node 20 cannot require one from
// this compiled spec, so its source is evaluated here.
const { bannedWords, ICON_CHARACTERS } = new Function(
  `${readFileSync(path.join(__dirname, '..', 'lib', 'copy-rules.mjs'), 'utf8').replace(/^export /gm, '')}\nreturn { bannedWords, ICON_CHARACTERS };`,
)() as { bannedWords: (text: string) => { rule: string; match: string }[]; ICON_CHARACTERS: RegExp };

async function signIn(page: Page, who: { email: string; password: string }) {
  await page.goto('/login');
  await page.locator('#email').fill(who.email);
  await page.locator('#password').fill(who.password);
  await page.getByRole('button', { name: 'Sign In', exact: true }).click();
  await expect(page).toHaveURL(/\/studio$/);
}

/** No svg, no svg pictures, nothing drawn with CSS content, no symbol characters and no banned words. */
async function expectTypeOnly(page: Page, where: string) {
  const seen = await page.evaluate(() => {
    const labels: string[] = [];
    for (const element of document.querySelectorAll('[aria-label], [title], [placeholder], [alt]')) {
      for (const name of ['aria-label', 'title', 'placeholder', 'alt']) {
        const value = element.getAttribute(name);
        if (value) labels.push(value);
      }
    }
    const drawn: string[] = [];
    for (const element of document.querySelectorAll('body *')) {
      for (const pseudo of ['::before', '::after']) {
        const content = getComputedStyle(element, pseudo).content;
        if (!['none', 'normal', '""', "''"].includes(content)) drawn.push(`<${element.tagName.toLowerCase()}>${pseudo} ${content}`);
      }
    }
    return {
      words: [
        document.title,
        document.querySelector('meta[name="description"]')?.getAttribute('content') ?? '',
        document.body.innerText,
        ...labels,
      ].join('\n'),
      svgs: document.querySelectorAll('svg').length,
      svgPictures: [...document.images].map((img) => img.currentSrc || img.src).filter((src) => /\.svg(?:$|\?)/i.test(src)),
      drawn,
    };
  });
  expect(seen.svgs, `${where} draws an svg`).toBe(0);
  expect(seen.svgPictures, `${where} shows an svg picture`).toEqual([]);
  expect(seen.drawn, `${where} draws with CSS content`).toEqual([]);
  expect(seen.words.match(new RegExp(ICON_CHARACTERS.source, 'gu')) ?? [], `${where} shows symbol characters`).toEqual([]);
  expect(bannedWords(seen.words), `${where} uses a banned word`).toEqual([]);
}

/** Every word in the tab bar sits inside its tab, with a clear gap before the next one. */
async function expectRoomyTabs(bar: Locator, where: string) {
  const tabs = await bar.locator('a, button').evaluateAll((all) => all.map((tab) => {
    const word = [...tab.querySelectorAll('span')].find((span) => !span.classList.contains('sr-only') && span.textContent?.trim());
    const box = tab.getBoundingClientRect();
    const text = (word ?? tab).getBoundingClientRect();
    return { name: word?.textContent?.trim() ?? '', tab: [box.left, box.right], word: [text.left, text.right] };
  }));
  for (const [i, { name, tab, word }] of tabs.entries()) {
    expect(word[0] >= tab[0] - 0.5 && word[1] <= tab[1] + 0.5, `${where}: "${name}" spills out of its tab`).toBe(true);
    if (i > 0) {
      expect(word[0] - tabs[i - 1].word[1], `${where}: "${tabs[i - 1].name}" and "${name}" run together`).toBeGreaterThanOrEqual(8);
    }
  }
}

/** The wordmark: "DuckDuckGoose" in Montserrat, "Goose" in azure, with no picture beside it. */
async function expectWordmark(wordmark: Locator) {
  await expect(wordmark).toBeVisible();
  await expect(wordmark).toHaveText('DuckDuckGoose');
  await expect(wordmark).toHaveCSS('font-family', /Montserrat/);
  await expect(wordmark.getByText('Goose', { exact: true })).toHaveCSS('color', 'rgb(10, 108, 192)');
  await expect(wordmark.locator('img, svg')).toHaveCount(0);
}

for (const [width, height] of SIZES) {
  test(`at ${width} px sign-in and the password page show the wordmark in type, no icons and no banned words`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    await page.goto('/login');
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('Studio Console');
    await expectWordmark(page.getByText('DuckDuckGoose', { exact: true }));
    await expectTypeOnly(page, '/login');
    await page.getByRole('button', { name: 'Sign in with admin secret' }).click();
    await expect(page.getByRole('button', { name: 'Back to email sign in', exact: true })).toBeVisible();
    await expectTypeOnly(page, '/login (admin secret)');

    await page.goto('/set-password');
    await expect(page.getByRole('heading', { name: 'Invalid Link' })).toBeVisible();
    await expectTypeOnly(page, '/set-password without a token');
    await page.goto('/set-password?token=e2e-not-a-real-token');
    await expect(page.getByRole('heading', { name: 'Set Your Password' })).toBeVisible();
    await expectTypeOnly(page, '/set-password');
  });

  test(`at ${width} px a member's studio is type, colour and shape only, with no banned words`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    await signIn(page, MEMBER);
    const pages: [string, Locator][] = [
      ['/studio', page.locator('[data-testid="universes-section"], [data-testid="first-run"]').first()],
      ['/studio/episodes', page.getByTestId('episodes-page')],
      ['/studio/create', page.getByRole('heading', { name: 'Make Your Own Video' })],
      ['/studio/universes/new', page.getByTestId('new-universe-page')],
      ['/dashboard', page.getByTestId('admins-only')],
    ];
    for (const [url, ready] of pages) {
      await page.goto(url);
      await expect(ready).toBeVisible();
      await expect(page.getByText(/^Loading/)).toHaveCount(0);
      const home = page.getByTestId('studio-home');
      await expect(home).toHaveAccessibleName('DuckDuckGoose Studio home');
      await expectWordmark(home.getByText('DuckDuckGoose', { exact: true }));
      await expectTypeOnly(page, url);
    }
  });

  test(`at ${width} px an admin's studio, cost dashboard and members page are type, colour and shape only`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    await signIn(page, ADMIN);
    const pages: [string, Locator][] = [
      ['/studio', page.locator('[data-testid="universes-section"], [data-testid="first-run"]').first()],
      ['/dashboard', page.getByRole('heading', { name: 'Admin Dashboard' })],
      ['/studio/users', page.getByRole('heading', { level: 1, name: 'Members' })],
    ];
    for (const [url, ready] of pages) {
      await page.goto(url);
      await expect(ready).toBeVisible();
      await expect(page.getByText(/^Loading/)).toHaveCount(0);
      await expectTypeOnly(page, url);
    }
  });
}

test('on a phone the tab bar names every tab in words and keeps every accessible name', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  for (const [who, labels, costs] of [[MEMBER, ['Universes', 'Episodes', 'New', 'You'], false], [ADMIN, ['Universes', 'Episodes', 'New', 'Costs', 'You'], true]] as const) {
    await page.context().clearCookies();
    await signIn(page, who);
    const bar = page.getByRole('navigation', { name: 'Studio' });
    await expect(bar).toBeVisible();
    if (costs) await expect(bar.getByRole('link', { name: 'Costs' })).toBeVisible();
    const tabs = bar.locator('a, button');
    const words = await tabs.evaluateAll((all) => all.map((tab) => {
      const seen = tab.cloneNode(true) as HTMLElement;
      seen.querySelectorAll('.sr-only').forEach((hidden) => hidden.remove());
      return seen.textContent?.trim();
    }));
    expect(words, 'the words on the tabs').toEqual(labels);
    for (const [i, tab] of (await tabs.all()).entries()) {
      await expect(tab.getByText(labels[i], { exact: true })).toBeVisible();
      await expect(tab).toHaveAccessibleName(labels[i] === 'New' ? 'Make something new' : labels[i]);
      expect((await tab.boundingBox())!.height, `${labels[i]} is a big enough target`).toBeGreaterThanOrEqual(44);
    }
    await expectRoomyTabs(bar, `the ${costs ? 'admin' : 'member'} tab bar at 390 px`);
    await expect(bar.getByRole('link', { name: 'Universes', exact: true })).toHaveAttribute('aria-current', 'page');
    await expect(bar.getByRole('link', { name: 'Episodes', exact: true })).not.toHaveAttribute('aria-current', 'page');
    await expect(bar.getByRole('button', { name: 'You', exact: true })).toHaveAttribute('aria-expanded', 'false');
    const make = page.getByTestId('studio-bottom-bar-new');
    await make.click();
    await expect(page.locator('#studio-make-menu').getByRole('link', { name: /New Universe/ })).toBeVisible();
    await expectTypeOnly(page, '/studio with the make menu open');

    await bar.getByRole('link', { name: 'Episodes', exact: true }).click();
    await expect(page).toHaveURL(/\/studio\/episodes$/);
    await expect(bar.getByRole('link', { name: 'Episodes', exact: true })).toHaveAttribute('aria-current', 'page');
    await bar.getByRole('button', { name: 'You', exact: true }).click();
    await expect(bar.getByRole('button', { name: 'You', exact: true })).toHaveAttribute('aria-expanded', 'true');
    await expect(page.getByRole('button', { name: 'Log out' })).toBeVisible();
    await expectTypeOnly(page, '/studio/episodes with the menu open');
  }

  // the admin's five slots on a small phone
  await page.setViewportSize({ width: 320, height: 640 });
  await page.goto('/studio');
  const bar = page.getByRole('navigation', { name: 'Studio' });
  await expect(bar.getByRole('link', { name: 'Costs' })).toBeVisible();
  await expectRoomyTabs(bar, 'the admin tab bar at 320 px');
  expect(await page.evaluate(() => document.documentElement.scrollWidth), 'the studio scrolls sideways at 320 px').toBeLessThanOrEqual(320);
});
