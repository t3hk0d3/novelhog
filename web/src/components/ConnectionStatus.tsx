import { useQuery } from '@tanstack/react-query';
import { api, ApiError } from '../api/client';
import { useLiveStatus } from '../hooks/useLiveEvents';
import { errorMessage } from '../lib/errors';

export const HOST_NOT_ALLOWED = 'This host name is not allowed; set NOVELHOG_ALLOWED_HOSTS';
export const STREAM_BLOCKED =
  'Live updates unavailable; check that your proxy does not buffer /api/v1/events';

/** Why the stream is stalled, from a probe of the API without the `ready` gate. */
function cause(error: unknown): string {
  if (error instanceof ApiError && error.code === 'host_not_allowed') return HOST_NOT_ALLOWED;
  return error ? errorMessage(error) : STREAM_BLOCKED;
}

/**
 * Shown on a page while its data waits for the stream's `ready`: "Connecting…", or once the
 * stream has stalled, an error naming the cause. Nothing once the stream is ready.
 */
export function ConnectionStatus() {
  const status = useLiveStatus();
  const probe = useQuery({
    queryKey: ['live-probe'],
    queryFn: api.getSettings,
    enabled: status === 'stalled',
    retry: false,
  });

  if (status === 'ready') return null;
  if (status === 'connecting' || probe.isPending) {
    return <p className="text-ink-muted">Connecting…</p>;
  }
  return (
    <p role="alert" className="text-danger">
      {cause(probe.error)}
    </p>
  );
}
