import type { components } from './schema';

type Schemas = components['schemas'];
export type Host = Schemas['Host'];
export type Novel = Schemas['Novel'];
export type Chapter = Schemas['Chapter'];
export type ChapterState = Schemas['ChapterState'];
export type ChapterPage = Schemas['ChapterPage'];
export type ChapterSelector = Schemas['ChapterSelector'];
export type ChapterPatchResult = Schemas['ChapterPatchResult'];
// Fields with a server default are required in the generated type; requests may omit them.
export type NovelCreate = Omit<Schemas['NovelCreate'], 'followed'> & { followed?: boolean };
export type NovelPatch = Schemas['NovelPatch'];
export type Export = Schemas['Export'];
export type ExportCreate = Omit<Schemas['ExportCreate'], 'format' | 'skip_missing'> &
  Partial<Pick<Schemas['ExportCreate'], 'format' | 'skip_missing'>>;
export type Settings = Schemas['SettingsOut'];
export type Subscription = Schemas['Subscription'];
export type WebpushSubscriptionCreate = Schemas['WebpushSubscriptionCreate'];

const BASE = '/api/v1';

/** An error response (`{"error": {code, message, details?}}`) from the API. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown> | undefined;

  constructor(status: number, code: string, message: string, details?: Record<string, unknown>) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

async function toApiError(response: Response): Promise<ApiError> {
  try {
    const body = (await response.json()) as Partial<Schemas['ErrorBody']> | null;
    const error = body?.error;
    if (error && typeof error.code === 'string' && typeof error.message === 'string') {
      return new ApiError(response.status, error.code, error.message, error.details ?? undefined);
    }
  } catch {
    // not JSON (for example a proxy error page): fall through to the generic error
  }
  return new ApiError(response.status, 'http_error', `Request failed (${String(response.status)})`);
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method };
  if (body !== undefined) {
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(body);
  }
  const response = await fetch(`${BASE}${path}`, init);
  if (!response.ok) throw await toApiError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

const id = encodeURIComponent;

export interface ChapterQuery {
  offset?: number;
  limit?: number;
  state?: ChapterState;
}

export const api = {
  getSettings: () => request<Settings>('GET', '/settings'),
  listHosts: () => request<Host[]>('GET', '/hosts'),
  listNovels: () => request<Novel[]>('GET', '/novels'),
  createNovel: (body: NovelCreate) => request<Novel>('POST', '/novels', body),
  getNovel: (novelId: string) => request<Novel>('GET', `/novels/${id(novelId)}`),
  patchNovel: (novelId: string, body: NovelPatch) =>
    request<Novel>('PATCH', `/novels/${id(novelId)}`, body),
  deleteNovel: (novelId: string) => request<undefined>('DELETE', `/novels/${id(novelId)}`),
  syncNovel: (novelId: string) => request<Novel>('POST', `/novels/${id(novelId)}/sync`),
  listChapters: (novelId: string, query: ChapterQuery = {}) => {
    const params = new URLSearchParams();
    if (query.offset !== undefined) params.set('offset', String(query.offset));
    if (query.limit !== undefined) params.set('limit', String(query.limit));
    if (query.state !== undefined) params.set('state', query.state);
    const qs = params.size > 0 ? `?${params.toString()}` : '';
    return request<ChapterPage>('GET', `/novels/${id(novelId)}/chapters${qs}`);
  },
  patchChapters: (novelId: string, state: ChapterState, chapters: ChapterSelector) =>
    request<ChapterPatchResult>('PATCH', `/novels/${id(novelId)}/chapters`, { state, chapters }),
  listExports: (novelId: string) => request<Export[]>('GET', `/novels/${id(novelId)}/exports`),
  createExport: (novelId: string, body: ExportCreate) =>
    request<Export>('POST', `/novels/${id(novelId)}/exports`, body),
  deleteExport: (novelId: string, exportId: string) =>
    request<undefined>('DELETE', `/novels/${id(novelId)}/exports/${id(exportId)}`),
  createWebpushSubscription: (body: WebpushSubscriptionCreate) =>
    request<Subscription>('POST', '/webpush/subscriptions', body),
  listSubscriptions: () => request<Subscription[]>('GET', '/subscriptions'),
  deleteSubscription: (subscriptionId: string) =>
    request<undefined>('DELETE', `/subscriptions/${id(subscriptionId)}`),
};
