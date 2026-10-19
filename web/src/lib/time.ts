const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

function toDate(value: Date | string): Date {
  return typeof value === 'string' ? new Date(value) : value;
}

function amount(ms: number): string {
  if (ms < HOUR) return `${Math.floor(ms / MINUTE)} min`;
  if (ms < DAY) return `${Math.floor(ms / HOUR)} h`;
  return `${Math.floor(ms / DAY)} d`;
}

/** "12 min ago", "2 h ago", "3 d ago"; "in 4 h" for the future; "just now" under a minute. */
export function relative(date: Date | string, now: Date = new Date()): string {
  const diff = now.getTime() - toDate(date).getTime();
  if (Number.isNaN(diff)) return 'unknown';
  if (Math.abs(diff) < MINUTE) return 'just now';
  return diff > 0 ? `${amount(diff)} ago` : `in ${amount(-diff)}`;
}

/** The local calendar date as YYYY-MM-DD. */
export function dateOnly(date: Date | string): string {
  const d = toDate(date);
  if (Number.isNaN(d.getTime())) return '—';
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return `${String(d.getFullYear())}-${mm}-${dd}`;
}
