import Link from 'next/link';

export type NextTone = 'needs' | 'working' | 'ok' | 'go';

const TONE: Record<NextTone, string> = {
  needs: 'bg-mustard-soft',
  working: 'bg-aqua-soft',
  ok: 'bg-green-soft',
  go: 'bg-blue-soft',
};

/** "Next: …" at the foot of a card: yellow when it needs you, cyan while the studio works, lime when ready. */
export default function NextStepPill({ text, tone = 'needs', href, testId, className = '' }: {
  text: string; tone?: NextTone; href?: string | null; testId?: string; className?: string;
}) {
  const cls = `relative z-10 flex min-h-11 items-center rounded-full px-4 py-1.5 text-[13.5px] font-extrabold text-ink ${TONE[tone]} ${className}`;
  const body = <span className="min-w-0 py-1">{text}</span>;
  return href ? (
    <Link href={href} className={`${cls} transition-transform hover:-translate-y-0.5 motion-reduce:transform-none`} data-testid={testId}
      data-tone={tone}>
      {body}
    </Link>
  ) : (
    <p className={cls} data-testid={testId} data-tone={tone}>{body}</p>
  );
}
