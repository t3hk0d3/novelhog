import { expect, test, type Page } from '@playwright/test';

// The fake backend's chapter 5 carries two images and a very long unbroken word
// (tests/fixtures/fake_backend.py): a wide one (2400 px, stored at 1600) and a small sigil.
const IMAGE_CHAPTER = 5;
const WIDE = 'A wide map';
const SIGIL = 'A tiny sigil';

interface NovelSummary {
  id: string;
}

test.beforeEach(async ({ request }) => {
  const response = await request.get('/api/v1/novels');
  expect(response.ok()).toBe(true);
  for (const novel of (await response.json()) as NovelSummary[]) {
    expect((await request.delete(`/api/v1/novels/${novel.id}`)).ok()).toBe(true);
  }
});

/** Adds the fake novel and downloads its image chapter through the API; returns the ids. */
async function novelWithImageChapter(
  request: Parameters<Parameters<typeof test>[2]>[0]['request'],
  name: string,
) {
  const created = await request.post('/api/v1/novels', {
    data: { toc_url: `https://fake.test/${name}-images` },
  });
  expect(created.ok()).toBe(true);
  const { id } = (await created.json()) as NovelSummary;
  await expect
    .poll(
      async () =>
        ((await (await request.get(`/api/v1/novels/${id}`)).json()) as { total_pages: number })
          .total_pages,
    )
    .toBe(5);
  const queued = await request.patch(`/api/v1/novels/${id}/chapters`, {
    data: { state: 'pending', chapters: { start: IMAGE_CHAPTER, end: IMAGE_CHAPTER } },
  });
  expect(queued.ok()).toBe(true);
  let chapterId = '';
  await expect
    .poll(async () => {
      const list = await request.get(
        `/api/v1/novels/${id}/chapters?offset=${String(IMAGE_CHAPTER - 1)}&limit=1`,
      );
      const item = ((await list.json()) as { items: { id: string; state: string }[] }).items[0];
      chapterId = item?.id ?? '';
      return item?.state;
    })
    .toBe('downloaded');
  return { novelId: id, chapterId };
}

/** Widths of an image as drawn and as stored, after it has loaded. */
async function widths(container: ReturnType<Page['locator']>, alt: string) {
  const image = container.getByRole('img', { name: alt });
  await image.scrollIntoViewIfNeeded();
  await expect
    .poll(() => image.evaluate((img: HTMLImageElement) => img.naturalWidth))
    .toBeGreaterThan(0);
  return image.evaluate((img: HTMLImageElement) => ({
    drawn: img.getBoundingClientRect().width,
    natural: img.naturalWidth,
    column: img.closest('article, section')?.clientWidth ?? 0,
  }));
}

const overflow = (page: Page) =>
  page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);

test('the reader draws chapter images inside the column and does not scroll sideways', async ({
  page,
  request,
}, testInfo) => {
  const { novelId, chapterId } = await novelWithImageChapter(request, testInfo.project.name);
  await page.goto(`/novels/${novelId}/chapters/${chapterId}`);
  const article = page.getByRole('article');
  await expect(article.getByText('Before the map.')).toBeVisible();

  const wide = await widths(article, WIDE);
  expect(wide.natural).toBeGreaterThan(wide.column); // stored larger than the column…
  expect(wide.drawn).toBeLessThanOrEqual(wide.column + 1); // …and scaled down to fit it

  const sigil = await widths(article, SIGIL);
  expect(sigil.drawn).toBe(sigil.natural); // a small image is not blown up

  expect(await overflow(page)).toBeLessThanOrEqual(0);
  const longWord = article.getByText(/^A very long word/);
  expect(await longWord.evaluate((p) => p.scrollWidth - p.clientWidth)).toBeLessThanOrEqual(0); // the long unbroken word wrapped
});

test('the preview draws the same images inside its own box', async ({
  page,
  request,
}, testInfo) => {
  const { novelId } = await novelWithImageChapter(request, testInfo.project.name);
  await page.goto(`/novels/${novelId}`);
  await page
    .locator('[data-chapter-id]')
    .filter({ hasText: new RegExp(`^${String(IMAGE_CHAPTER)}`) })
    .click();
  await page.getByRole('button', { name: 'Preview' }).click();
  const preview = page.getByRole('region', { name: 'Chapter content' });
  await expect(preview.getByText('Before the map.')).toBeVisible();

  const wide = await widths(preview, WIDE);
  expect(wide.drawn).toBeLessThanOrEqual(wide.column + 1);
  expect(await preview.evaluate((box) => box.scrollWidth - box.clientWidth)).toBeLessThanOrEqual(0);
});

test('the reader reopens a chapter where it was left', async ({ page, request }, testInfo) => {
  const { novelId, chapterId } = await novelWithImageChapter(request, testInfo.project.name);
  // A short window so even this short chapter has something to scroll.
  await page.setViewportSize({ width: 390, height: 360 });
  await page.goto(`/novels/${novelId}/chapters/${chapterId}`);
  const article = page.getByRole('article');
  await expect(article.getByText('After the images.')).toBeAttached();
  await widths(article, WIDE);
  const room = await page.evaluate(
    () => document.documentElement.scrollHeight - window.innerHeight,
  );
  expect(room).toBeGreaterThan(100);

  await page.evaluate((y) => {
    window.scrollTo(0, y);
  }, room / 2);
  await expect
    .poll(() => page.evaluate(() => localStorage.getItem('novelhog.reader.positions')))
    .toContain(chapterId);

  await page.reload();
  await expect(page.getByRole('article').getByText('After the images.')).toBeAttached();
  await expect
    .poll(() => page.evaluate(() => window.scrollY), { timeout: 5000 })
    .toBeGreaterThan(room / 2 - 40);
  expect(await page.evaluate(() => window.scrollY)).toBeLessThan(room / 2 + 40);
});
