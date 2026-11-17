import type { Badge } from '@/lib/consistency.mjs';
import { chip, tones } from '@/lib/ui';

/** One consistency badge (lib/consistency.mjs): locked, look changed since lock, older look, not locked yet, photo set. */
export function ConsistencyBadge({ badge, testId }: { badge: Badge; testId?: string }) {
  return (
    <span className={`${chip} ${tones[badge.tone]}`} data-testid={testId} data-badge={badge.key}>
      {badge.label}
    </span>
  );
}
