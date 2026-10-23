import { expect, test, type Page } from '@playwright/test';

const TITLE = 'The Fake Chronicle';
// The fake backend's chapter 2 always fails (tests/fixtures/fake_backend.py).
const FAILURE = 'chapter 2 is unreadable';

interface NovelSummary {
  id: string;
}

/** Row of chapter `n` in the chapter table (normal or select mode). */
const chapter = (page: Page, n: number) =>
  page.locator('[data-chapter-id]').filter({ hasText: new RegExp(`^${String(n)}`) });

test.beforeEach(async ({ request }) => {
  // One backend serves every project: start each test from an empty library.
  const response = await request.get('/api/v1/novels');
  expect(response.ok()).toBe(true);
  for (const novel of (await response.json()) as NovelSummary[]) {
    expect((await request.delete(`/api/v1/novels/${novel.id}`)).ok()).toBe(true);
  }
});

test('main flow: add, download, details, export, delete', async ({ page }, testInfo) => {
  const scheme = testInfo.project.use.colorScheme ?? 'light';
  await page.goto('/');
  expect(await page.evaluate(() => window.matchMedia('(prefers-color-scheme: dark)').matches)).toBe(
    scheme === 'dark',
  );
  await expect(page.getByText('Your library is empty.')).toBeVisible();

  await test.step('add a novel; the card shows Importing… then the imported title', async () => {
    await page.getByRole('banner').getByRole('button', { name: 'Add novel' }).click();
    const dialog = page.getByRole('dialog', { name: 'Add novel' });
    await dialog
      .getByLabel('Table of contents URL')
      .fill(`https://fake.test/${testInfo.project.name}`);
    await dialog.getByRole('button', { name: 'Add', exact: true }).click();

    const card = page.getByRole('article');
    await expect(card.getByText('Importing…')).toBeVisible();
    await expect(card.getByRole('link', { name: TITLE })).toBeVisible();
    await expect(card.getByText('Importing…')).toBeHidden();
    await expect(card.getByText('0 / 5 downloaded')).toBeVisible();
    await expect
      .poll(() => card.locator('img').evaluate((img: HTMLImageElement) => img.naturalWidth))
      .toBeGreaterThan(0); // the cover is served from /static
    await card.getByRole('link', { name: TITLE }).click();
    await expect(page.getByRole('heading', { level: 1, name: TITLE })).toBeVisible();
  });

  await test.step('select a range and Download; states go queued, then downloaded', async () => {
    await page.getByRole('button', { name: 'Select', exact: true }).click();
    await chapter(page, 1).click();
    await chapter(page, 3).click({ modifiers: ['Shift'] });
    await expect(page.getByText('3 selected').first()).toBeVisible();
    await page
      .getByRole('region', { name: 'Selection actions' })
      .getByRole('button', { name: 'Download' })
      .click();
    await expect(page.getByText('Queued 3 · skipped 0')).toBeVisible();

    await expect(chapter(page, 3)).toHaveAccessibleName(/Pending/);
    await expect(chapter(page, 1)).toHaveAccessibleName(/Downloaded/);
    await expect(chapter(page, 3)).toHaveAccessibleName(/Downloaded/);
    await expect(chapter(page, 2)).toHaveAccessibleName(/Error/);
  });

  await test.step('open chapter details and see the failing chapter error', async () => {
    await chapter(page, 2).click();
    const details = page.getByRole('dialog', { name: 'Chapter 2 of 5' });
    await expect(details.getByRole('group', { name: 'Error details' })).toContainText(FAILURE);
    await details.getByRole('button', { name: 'Close' }).click();
    await expect(details).toBeHidden();
  });

  await test.step('export with missing chapters waits, then is ready and downloads', async () => {
    await page.getByRole('button', { name: 'More', exact: true }).click();
    await page.getByRole('button', { name: 'Export EPUB…' }).click();
    const dialog = page.getByRole('dialog', { name: 'Export' });
    await dialog.getByLabel('From').fill('3');
    await dialog.getByLabel('To').fill('5');
    await expect(dialog.getByText('2 chapters will be downloaded first')).toBeVisible();
    await dialog.getByRole('button', { name: 'Download & export' }).click();

    const exports = page.getByRole('region', { name: 'Exports' });
    await expect(exports.getByText('Chapters 3–5')).toBeVisible();
    const link = exports.getByRole('link', { name: 'Download' });
    await expect(link).toBeVisible();
    const [download] = await Promise.all([page.waitForEvent('download'), link.click()]);
    expect(download.suggestedFilename()).toMatch(/\.epub$/);
    const stream = await download.createReadStream();
    const head: Buffer[] = [];
    for await (const part of stream) head.push(part as Buffer);
    expect(Buffer.concat(head).subarray(0, 2).toString()).toBe('PK'); // an EPUB is a zip
  });

  await test.step('delete the novel; the app returns to an empty library', async () => {
    await page.getByRole('button', { name: 'More', exact: true }).click();
    await page.getByRole('button', { name: 'Delete novel…' }).click();
    const dialog = page.getByRole('dialog', { name: 'Delete novel' });
    await dialog.getByRole('button', { name: 'Delete', exact: true }).click();
    await expect(page).toHaveURL('/');
    await expect(page.getByText('Your library is empty.')).toBeVisible();
  });
});
