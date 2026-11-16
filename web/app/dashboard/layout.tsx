import type { ReactNode } from 'react';
import StudioShell, { AdminOnly } from '@/components/StudioShell';

/** The cost dashboard sits in the studio shell, for admins only (the page itself is unchanged). */
export default function DashboardLayout({ children }: { children: ReactNode }) {
  return (
    <StudioShell>
      <AdminOnly>{children}</AdminOnly>
    </StudioShell>
  );
}
