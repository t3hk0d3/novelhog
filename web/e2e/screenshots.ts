import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

/** Where the PNGs go (the script runs from `web/`). */
const OUT = '../docs/assets/screenshots';
const HOST = 'https://gutenberg.org';

interface DemoNovel {
  slug: string;
  followed: boolean;
  /** Chapters 1..downloaded are downloaded; the rest stay "not requested". */
  downloaded: number;
}

/** The demo library, in the order it is added (see `tests/fixtures/demo_backend.py`). */
const NOVELS: DemoNovel[] = [
  { slug: 'sherlock-holmes', followed: true, downloaded: 2 },
  { slug: 'moby-dick', followed: false, downloaded: 3 },
  { slug: 'wizard-of-oz', followed: true, downloaded: 5 },
  { slug: 'pride-and-prejudice', followed: true, downloaded: 8 },
  { slug: 'alice-in-wonderland', followed: true, downloaded: 8 },
];
/** The novel the novel-page and reader shots show. */
const FEATURED = 'alice-in-wonderland';
const NOT_DOWNLOADED_POSITION = 10;
/** The desktop library ends here: two rows of cards and a margin. */
const LIBRARY_DESKTOP_HEIGHT = 640;

interface Created {
  id: string;
}
interface Chapter {
  id: string;
  position: number;
  state: string;
}
interface ExportSummary {
  state: string;
}

async function clearLibrary(request: APIRequestContext) {
  const response = await request.get('/api/v1/novels');
  expect(response.ok()).toBe(true);
  for (const novel of (await response.json()) as Created[]) {
    expect((await request.delete(`/api/v1/novels/${novel.id}`)).ok()).toBe(true);
  }
}

async function chaptersOf(request: APIRequestContext, novelId: string): Promise<Chapter[]> {
  const response = await request.get(`/api/v1/novels/${novelId}/chapters?limit=500`);
  expect(response.ok()).toBe(true);
  return ((await response.json()) as { items: Chapter[] }).items;
}

/** Adds every demo novel and downloads its first chapters; returns the ids by slug. */
async function seed(request: APIRequestContext): Promise<Record<string, string>> {
  await clearLibrary(request);
  const ids: Record<string, string> = {};
  for (const novel of NOVELS) {
    const created = await request.post('/api/v1/novels', {
      data: { toc_url: `${HOST}/${novel.slug}`, followed: novel.followed },
    });
    expect(created.ok()).toBe(true);
    const id = ((await created.json()) as Created).id;
    ids[novel.slug] = id;
    await expect.poll(async () => (await chaptersOf(request, id)).length).toBeGreaterThan(0);
    const patch = await request.patch(`/api/v1/novels/${id}/chapters`, {
      data: { state: 'pending', chapters: { start: 1, end: novel.downloaded } },
    });
    expect(patch.ok()).toBe(true);
    await expect
      .poll(async () => {
        const chapters = await chaptersOf(request, id);
        return chapters.filter((c) => c.state === 'downloaded').length;
      })
      .toBe(novel.downloaded);
  }
  return ids;
}

/** Waits for fonts and every image on the page, so a shot never catches one half-loaded. */
async function settle(page: Page) {
  await page.evaluate(() => document.fonts.ready);
  await expect
    .poll(() =>
      page.evaluate(() =>
        [...document.images].every((img) => img.complete && img.naturalWidth > 0),
      ),
    )
    .toBe(true);
}

test('documentation screenshots', async ({ page, request }, testInfo) => {
  const [device = 'desktop', scheme = 'light'] = testInfo.project.name.split('-');
  const light = scheme === 'light';
  /** Captures the viewport, or only its top `height` pixels (for a page that ends early). */
  const shot = async (name: string, height?: number) => {
    await settle(page);
    const suffix = light ? '' : '-dark';
    const viewport = page.viewportSize();
    await page.screenshot({
      path: `${OUT}/${name}-${device}${suffix}.png`,
      animations: 'disabled',
      caret: 'hide',
      clip:
        height !== undefined && viewport
          ? { x: 0, y: 0, width: viewport.width, height }
          : undefined,
    });
  };

  const ids = await seed(request);
  const featured = ids[FEATURED];
  if (featured === undefined) throw new Error('the featured novel was not added');
  const chapters = await chaptersOf(request, featured);
  const first = chapters.find((c) => c.position === 1);
  const notDownloaded = chapters.find((c) => c.position === NOT_DOWNLOADED_POSITION);
  if (!first || !notDownloaded) throw new Error('the featured novel lacks the expected chapters');

  const exported = await request.post(`/api/v1/novels/${featured}/exports`, {
    data: { chapters: { start: 1, end: 8 }, format: 'epub', skip_missing: false },
  });
  expect(exported.ok()).toBe(true);
  await expect
    .poll(async () => {
      const list = (await (
        await request.get(`/api/v1/novels/${featured}/exports`)
      ).json()) as ExportSummary[];
      return list.every((e) => e.state === 'ready');
    })
    .toBe(true);

  await test.step('library', async () => {
    await page.goto('/');
    await expect(page.getByRole('article')).toHaveCount(NOVELS.length);
    // Two rows of cards fill a phone; on a desktop the page ends well above the fold.
    await shot('library', device === 'desktop' ? LIBRARY_DESKTOP_HEIGHT : undefined);
  });

  await test.step('novel page with chapters and exports', async () => {
    await page.goto(`/novels/${featured}`);
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    await page.getByText('Click to open').click();
    await expect(page.getByText('Click to open')).toBeHidden();
    await shot('novel');
  });

  await test.step('reader', async () => {
    await page.goto(`/novels/${featured}/chapters/${first.id}`);
    await expect(page.getByTestId('reader-surface')).toBeVisible();
    await shot('reader');
  });

  if (!light) return;

  const openSettings = async () => {
    await page.getByRole('button', { name: 'Reading settings' }).click();
    return page.getByRole('dialog', { name: 'Reading settings' });
  };
  const closeSettings = async () => {
    await page.keyboard.press('Escape');
    await expect(page.getByRole('dialog', { name: 'Reading settings' })).toBeHidden();
  };

  await test.step('reading settings', async () => {
    await openSettings();
    await shot('reader-settings');
    await closeSettings();
  });

  await test.step('themes and fonts', async () => {
    for (const [theme, font] of [
      ['Sepia', 'Old style'],
      ['Night', 'Serif'],
    ] as const) {
      const dialog = await openSettings();
      await dialog.getByRole('button', { name: theme, exact: true }).click();
      await dialog.getByRole('button', { name: font, exact: true }).click();
      await closeSettings();
      await shot(`reader-${theme.toLowerCase()}`);
    }
  });

  await test.step('a chapter that is not downloaded', async () => {
    await page.goto(`/novels/${featured}/chapters/${notDownloaded.id}`);
    await expect(page.getByText("This chapter isn't downloaded yet.")).toBeVisible();
    await shot('reader-not-downloaded');
  });
});
