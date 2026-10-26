import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, api } from './client';

function respond(status: number, body?: unknown) {
  const fetchMock = vi.fn<typeof fetch>(() =>
    Promise.resolve(
      new Response(body === undefined ? null : JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json' },
      }),
    ),
  );
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('api client', () => {
  it('turns an error body into ApiError with code and details', async () => {
    respond(409, {
      error: { code: 'novel_exists', message: 'exists', details: { novel_id: 'NV1' } },
    });
    const err = await api.createNovel({ toc_url: 'https://x/y' }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({
      status: 409,
      code: 'novel_exists',
      message: 'exists',
      details: { novel_id: 'NV1' },
    });
  });

  it('falls back to a generic ApiError when the body is not the error shape', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(new Response('<html>', { status: 502 }))),
    );
    await expect(api.listNovels()).rejects.toMatchObject({ status: 502, code: 'http_error' });
  });

  it('sends JSON bodies to /api/v1 paths', async () => {
    const fetchMock = respond(201, { id: 'NV1' });
    await api.createNovel({ toc_url: 'https://x/y', followed: false });
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/novels',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ toc_url: 'https://x/y', followed: false }),
      }),
    );
  });

  it('resolves a 204 to undefined', async () => {
    respond(204);
    await expect(api.deleteNovel('NV1')).resolves.toBeUndefined();
  });

  it('builds the chapters query string', async () => {
    const fetchMock = respond(200, { items: [], total: 0 });
    await api.listChapters('NV1', { offset: 1000, limit: 500, state: 'error' });
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      '/api/v1/novels/NV1/chapters?offset=1000&limit=500&state=error',
    );
  });

  it('percent-encodes path ids', async () => {
    const fetchMock = respond(200, {});
    await api.getNovel('a/b');
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/novels/a%2Fb');
  });

  describe('getChapterContent', () => {
    it('GETs the chapter content and returns the parsed body', async () => {
      const body = { chapter: { id: 'CH1' }, markdown: 'Hi', fetched_at: 'x', previous_id: null };
      const fetchMock = respond(200, body);
      await expect(api.getChapterContent('NV1', 'CH1')).resolves.toEqual(body);
      expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/novels/NV1/chapters/CH1/content');
    });

    it('throws not_found with the chapter_content entity when nothing is stored', async () => {
      respond(404, {
        error: { code: 'not_found', message: 'no content', details: { entity: 'chapter_content' } },
      });
      await expect(api.getChapterContent('NV1', 'CH1')).rejects.toMatchObject({
        status: 404,
        code: 'not_found',
        details: { entity: 'chapter_content' },
      });
    });
  });
});
