import type { ChapterState } from '../api/client';
import { StatusIcon, TAB_COLOR_VAR } from './StatusIcon';

// Leading-edge chevron point, 14 px deep, mirrored for right-to-left.
const CHEVRON =
  '[clip-path:polygon(14px_0,100%_0,100%_100%,14px_100%,0_50%)] rtl:[clip-path:polygon(0_0,calc(100%-14px)_0,100%_50%,calc(100%-14px)_100%,0_100%)]';

/** Full-row-height status block (spec section 10); the chevron point shows in normal mode only. */
export function StatusTab({ state, chevron = true }: { state: ChapterState; chevron?: boolean }) {
  return (
    <span
      data-testid="status-tab"
      data-chevron={chevron}
      className={`flex w-12 shrink-0 items-center justify-center self-stretch ${chevron ? CHEVRON : ''}`}
      style={{ backgroundColor: TAB_COLOR_VAR[state] }}
    >
      <StatusIcon state={state} />
    </span>
  );
}
