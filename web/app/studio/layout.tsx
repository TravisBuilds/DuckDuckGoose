import type { ReactNode } from 'react';
import StudioShell from '@/components/StudioShell';

export default function StudioLayout({ children }: { children: ReactNode }) {
  return <StudioShell>{children}</StudioShell>;
}
