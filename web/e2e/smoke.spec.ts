import { expect, test, type Locator, type Page } from '@playwright/test';

const TITLE = 'The Fake Chronicle';
// The fake backend's chapter 2 always fails (tests/fixtures/fake_backend.py).
const FAILURE = 'chapter 2 is unreadable';

interface NovelSummary {
  id: string;
}

/** Row of chapter `n` in the chapter table (normal or select mode). */
const chapter = (page: Page, n: number) =>
  page.locator('[data-chapter-id]').filter({ hasText: new RegExp(`^${String(n)}`) });

/** Holds the primary button down on `row` until the page shows `selected` (a long press), then releases. */
async function longPress(page: Page, row: Locator, selected: string) {
  const box = await row.boundingBox();
  if (!box) throw new Error('row is not visible');
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await expect(page.getByText(selected).first()).toBeVisible();
  await page.mouse.up();
}

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
  // The theme really applies: the page background is light or dark, not just the media query.
  const background = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  const [red = 0, green = 0, blue = 0] = (background.match(/\d+/g) ?? []).map(Number);
  const luminance = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255;
  if (scheme === 'dark') expect(luminance).toBeLessThan(0.3);
  else expect(luminance).toBeGreaterThan(0.7);

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
    if (testInfo.project.use.isMobile) {
      // Touch: press and hold a row to enter select mode, hold another to extend the range.
      await longPress(page, chapter(page, 1), '1 selected');
      await longPress(page, chapter(page, 3), '3 selected');
    } else {
      await page.getByRole('button', { name: 'Select', exact: true }).click();
      await chapter(page, 1).click();
      await chapter(page, 3).click({ modifiers: ['Shift'] });
    }
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

  await test.step('read a chapter: preview, Read, step through the chapters, back', async () => {
    await chapter(page, 1).click();
    const details = page.getByRole('dialog', { name: 'Chapter 1 of 5' });
    await details.getByRole('button', { name: 'Preview' }).click();
    const preview = page.getByRole('dialog').last();
    await expect(preview.getByRole('region', { name: 'Chapter content' })).toContainText(
      'Text of chapter 1.',
    );
    await preview.getByRole('button', { name: 'Back' }).click();
    await details.getByRole('link', { name: 'Read' }).click();
    await expect(page).toHaveURL(/\/novels\/[^/]+\/chapters\/[^/]+$/);
    await expect(page.getByRole('progressbar', { name: 'Reading position' })).toHaveAttribute(
      'aria-valuetext',
      'Chapter 1 of 5',
    );
    await expect(page.getByText('Text of chapter 1.')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Previous chapter' })).toBeDisabled();

    await page.getByRole('button', { name: 'Next chapter' }).click();
    await expect(page.getByRole('progressbar', { name: 'Reading position' })).toHaveAttribute(
      'aria-valuetext',
      'Chapter 2 of 5',
    );
    await expect(page.getByText("This chapter isn't downloaded yet.")).toBeVisible();
    await page.getByRole('button', { name: 'Next chapter' }).click();
    await expect(page.getByText('Text of chapter 3.')).toBeVisible();

    if (testInfo.project.use.isMobile) {
      // A reload is a direct visit: the chapter list loads after the page, and swipes must still work.
      await page.reload();
      await expect(page.getByText('Text of chapter 3.')).toBeVisible();
      // A swipe to the right with the finger goes back one chapter (touch events from the page).
      await page.getByTestId('reader-surface').evaluate(async (surface) => {
        const touch = (x: number, y: number) =>
          new Touch({ identifier: 1, target: surface, clientX: x, clientY: y });
        const fire = (type: string, x: number, y: number, ended = false) => {
          const points = ended ? [] : [touch(x, y)];
          surface.dispatchEvent(
            new TouchEvent(type, {
              bubbles: true,
              cancelable: true,
              touches: points,
              changedTouches: [touch(x, y)],
            }),
          );
        };
        fire('touchstart', 50, 300);
        for (const x of [130, 220, 310, 390]) {
          fire('touchmove', x, 302);
          await new Promise((resolve) => setTimeout(resolve, 16));
        }
        fire('touchend', 390, 302, true);
      });
      await expect(page.getByRole('progressbar', { name: 'Reading position' })).toHaveAttribute(
        'aria-valuetext',
        'Chapter 2 of 5',
      );
      await expect(page.getByRole('navigation', { name: 'Chapters' })).toBeInViewport();
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - window.innerWidth,
      );
      expect(overflow).toBeLessThanOrEqual(0);
    }

    await page.getByRole('link', { name: 'Back to novel' }).click();
    await expect(page.getByRole('heading', { level: 1, name: TITLE })).toBeVisible();
  });

  await test.step('export with missing chapters waits, then is ready and downloads', async () => {
    await page.getByRole('button', { name: 'Export…' }).click();
    const dialog = page.getByRole('dialog', { name: 'Export' });
    await dialog.getByLabel('From').fill('3');
    await dialog.getByLabel('To').fill('5');
    await expect(dialog.getByText('2 chapters will be downloaded first')).toBeVisible();
    await dialog.getByRole('button', { name: 'Download & export' }).click();

    const exports = page.getByRole('region', { name: 'Exports' });
    await expect(exports.getByText('Chapters 3–5')).toBeVisible();
    // It waits for the missing chapters (3 is downloaded, 4 and 5 are not) and is not ready yet.
    await expect(exports.getByText('1 / 3 downloaded')).toBeVisible();
    await expect(exports.getByRole('link', { name: 'Download' })).toBeHidden();
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
