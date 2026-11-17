import Link from 'next/link';

export type NextTone = 'needs' | 'working' | 'ok' | 'go';

const TONE: Record<NextTone, string> = {
  needs: 'bg-mustard-soft',
  working: 'bg-aqua-soft',
  ok: 'bg-green-soft',
  go: 'bg-blue-soft',
};

/** "Next: …" at the foot of a card: mustard when it needs you, aqua while the studio works, green when ready. */
export default function NextStepPill({ text, tone = 'needs', href, testId, className = '' }: {
  text: string; tone?: NextTone; href?: string | null; testId?: string; className?: string;
}) {
  const cls = `relative z-10 flex min-h-11 items-center justify-between gap-3 rounded-input px-3.5 py-2.5 text-[13.5px] font-extrabold text-ink ${TONE[tone]} ${className}`;
  const body = (
    <>
      <span className="min-w-0">{text}</span>
      <span aria-hidden="true">→</span>
    </>
  );
  return href ? (
    <Link href={href} className={`${cls} transition-transform hover:-translate-y-0.5 motion-reduce:transform-none`} data-testid={testId}
      data-tone={tone}>
      {body}
    </Link>
  ) : (
    <p className={cls} data-testid={testId} data-tone={tone}>{body}</p>
  );
}
