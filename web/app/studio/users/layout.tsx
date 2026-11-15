import type { ReactNode } from 'react';
import { AdminOnly } from '@/components/StudioShell';

export default function MembersLayout({ children }: { children: ReactNode }) {
  return <AdminOnly>{children}</AdminOnly>;
}
