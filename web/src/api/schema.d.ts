/**
 * HAND-WRITTEN from spec sections 5.2-5.8 until the Python API exists (Tasks 14-18).
 * A follow-up replaces this file with the output of `npm run gen:api` and enables `check:api`.
 * Shape follows openapi-typescript output (paths / components / operations).
 */

export interface paths {
  '/api/v1/settings': { get: operations['get_settings'] };
  '/api/v1/hosts': { get: operations['list_hosts'] };
  '/api/v1/novels': { get: operations['list_novels']; post: operations['create_novel'] };
  '/api/v1/novels/{novel_id}': {
    get: operations['get_novel'];
    patch: operations['patch_novel'];
    delete: operations['delete_novel'];
  };
  '/api/v1/novels/{novel_id}/sync': { post: operations['sync_novel'] };
  '/api/v1/novels/{novel_id}/chapters': {
    get: operations['list_chapters'];
    patch: operations['patch_chapters'];
  };
  '/api/v1/novels/{novel_id}/exports': {
    get: operations['list_exports'];
    post: operations['create_export'];
  };
  '/api/v1/novels/{novel_id}/exports/{export_id}': { delete: operations['delete_export'] };
  '/api/v1/webpush/subscriptions': { post: operations['create_webpush_subscription'] };
  '/api/v1/subscriptions': { get: operations['list_subscriptions'] };
  '/api/v1/subscriptions/{subscription_id}': { delete: operations['delete_subscription'] };
  '/api/v1/events': { get: operations['stream_events'] };
}

export interface components {
  schemas: {
    Host: { host: string };
    Novel: {
      id: string;
      title: string | null;
      host: string;
      toc_url: string;
      import_error: string | null;
      importing: boolean;
      cover_url: string | null;
      followed: boolean;
      syncing: boolean;
      refresh_interval: number;
      next_refresh_at: string;
      last_refreshed_at: string | null;
      total_pages: number;
      downloaded_pages: number;
      pending_pages: number;
      error_pages: number;
      new_pages: number;
      created_at: string;
      updated_at: string;
    };
    ChapterState: 'not_requested' | 'pending' | 'downloading' | 'downloaded' | 'error';
    Chapter: {
      id: string;
      position: number;
      url: string;
      title: string | null;
      state: components['schemas']['ChapterState'];
      error: string | null;
      error_at: string | null;
      has_images: boolean;
      released_at: string | null;
      queued_at: string | null;
      updated_at: string;
    };
    ChapterPage: { items: components['schemas']['Chapter'][]; total: number };
    ChapterSelector: number[] | { start: number; end: number };
    ChapterPatch: {
      state: components['schemas']['ChapterState'];
      chapters: components['schemas']['ChapterSelector'];
    };
    ChapterPatchResult: {
      updated: number[];
      skipped: { position: number; state: components['schemas']['ChapterState'] }[];
    };
    NovelCreate: { toc_url: string; followed?: boolean };
    NovelPatch: { followed?: boolean; seen?: true; title?: string };
    ExportState: 'waiting' | 'building' | 'ready' | 'failed';
    Export: {
      id: string;
      novel_id: string;
      filename: string;
      chapter_from: number | null;
      chapter_to: number | null;
      skip_missing: boolean;
      format: 'epub';
      mime: string;
      state: components['schemas']['ExportState'];
      progress: { downloaded: number; total: number };
      blocked: { errors: number[]; not_queued: number[] };
      error: string | null;
      waited: boolean;
      size_bytes: number | null;
      url: string | null;
      created_at: string;
      ready_at: string | null;
    };
    ExportCreate: {
      chapters: { start: number; end: number };
      format?: 'epub';
      skip_missing?: boolean;
    };
    Settings: {
      notifications: {
        channels: {
          webpush: { enabled: boolean; vapid_public_key: string | null };
        } & { [channel: string]: { enabled: boolean } };
      };
      static_base_url: string;
      refresh_min_interval: number;
      refresh_max_interval: number;
    };
    Subscription: { id: string; channel: string; label: string | null; created_at: string };
    WebpushSubscriptionCreate: { endpoint: string; keys: { p256dh: string; auth: string } };
    ErrorBody: {
      error: { code: string; message: string; details?: { [key: string]: unknown } };
    };
  };
}

type Json<T> = { content: { 'application/json': T } };
type NovelIdParam = { path: { novel_id: string } };

export interface operations {
  get_settings: { responses: { 200: Json<components['schemas']['Settings']> } };
  list_hosts: { responses: { 200: Json<components['schemas']['Host'][]> } };
  list_novels: { responses: { 200: Json<components['schemas']['Novel'][]> } };
  create_novel: {
    requestBody: Json<components['schemas']['NovelCreate']>;
    responses: {
      201: Json<components['schemas']['Novel']>;
      409: Json<components['schemas']['ErrorBody']>; // novel_exists (details.novel_id)
      422: Json<components['schemas']['ErrorBody']>; // invalid_url, unsupported_host (details.hosts)
      503: Json<components['schemas']['ErrorBody']>; // hosts_not_published
    };
  };
  get_novel: {
    parameters: NovelIdParam;
    responses: { 200: Json<components['schemas']['Novel']>; 404: Json<components['schemas']['ErrorBody']> };
  };
  patch_novel: {
    parameters: NovelIdParam;
    requestBody: Json<components['schemas']['NovelPatch']>;
    responses: {
      200: Json<components['schemas']['Novel']>;
      404: Json<components['schemas']['ErrorBody']>;
      422: Json<components['schemas']['ErrorBody']>; // invalid_title
    };
  };
  delete_novel: {
    parameters: NovelIdParam;
    responses: { 204: { content?: never }; 404: Json<components['schemas']['ErrorBody']> };
  };
  sync_novel: {
    parameters: NovelIdParam;
    responses: { 202: Json<components['schemas']['Novel']>; 404: Json<components['schemas']['ErrorBody']> };
  };
  list_chapters: {
    parameters: {
      path: { novel_id: string };
      query?: { offset?: number; limit?: number; state?: components['schemas']['ChapterState'] };
    };
    responses: { 200: Json<components['schemas']['ChapterPage']>; 404: Json<components['schemas']['ErrorBody']> };
  };
  patch_chapters: {
    parameters: NovelIdParam;
    requestBody: Json<components['schemas']['ChapterPatch']>;
    responses: {
      200: Json<components['schemas']['ChapterPatchResult']>;
      404: Json<components['schemas']['ErrorBody']>;
      422: Json<components['schemas']['ErrorBody']>; // validation_error, invalid_target_state, invalid_selector
    };
  };
  list_exports: {
    parameters: NovelIdParam;
    responses: { 200: Json<components['schemas']['Export'][]>; 404: Json<components['schemas']['ErrorBody']> };
  };
  create_export: {
    parameters: NovelIdParam;
    requestBody: Json<components['schemas']['ExportCreate']>;
    responses: {
      200: Json<components['schemas']['Export']>; // identical active export returned
      201: Json<components['schemas']['Export']>;
      404: Json<components['schemas']['ErrorBody']>;
      422: Json<components['schemas']['ErrorBody']>; // invalid_selector, unsupported_format, nothing_to_export
    };
  };
  delete_export: {
    parameters: { path: { novel_id: string; export_id: string } };
    responses: {
      204: { content?: never };
      404: Json<components['schemas']['ErrorBody']>;
      409: Json<components['schemas']['ErrorBody']>; // export_building
    };
  };
  create_webpush_subscription: {
    requestBody: Json<components['schemas']['WebpushSubscriptionCreate']>;
    responses: {
      200: Json<components['schemas']['Subscription']>; // existing endpoint replaced
      201: Json<components['schemas']['Subscription']>;
      422: Json<components['schemas']['ErrorBody']>; // validation_error, invalid_endpoint
      503: Json<components['schemas']['ErrorBody']>; // channel_disabled
    };
  };
  list_subscriptions: { responses: { 200: Json<components['schemas']['Subscription'][]> } };
  delete_subscription: {
    parameters: { path: { subscription_id: string } };
    responses: { 204: { content?: never }; 404: Json<components['schemas']['ErrorBody']> };
  };
  stream_events: {
    parameters: { query?: { novel_id?: string } };
    responses: { 200: { content: { 'text/event-stream': string } } };
  };
}
