/** A message fit for a toast or an inline error, whatever was thrown. */
export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Something went wrong';
}
