'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { PriceBlock } from '@/components/playful';
import {
  CHANGE_EXAMPLES, CHANGE_MAX, aboutLine, anyMaking, changeBlocker, changesLine, framesCount, framesSummary, latestRound,
  makeLabel, optionName, pickedOption, priceRows, priceStatus, priceTotal, referencesLine, roundNote, roundTitle,
  shotBlocker, shotStatus,
} from '@/lib/frames.mjs';
import type { FrameEstimate, FrameOption, FrameRound, FrameShot, FrameTone, FramesView } from '@/lib/frames.mjs';
import { API_URL, ApiError, apiFetch, postJson } from '@/lib/studio-api';
import { btnGo, btnPrimary, btnSecondary, card, input, link } from '@/lib/ui';

/**
 * First frames (docs/CONCEPT-FRAMES.md): before filming, each shot can start from a picture the owner
 * picked. The studio writes the picture from the shot's story moment, words, characters, place, props
 * and their reference photos, makes a few options (the price first; free stand-ins in a test run), and
 * a change in plain words ("make it night") reaches every later try. The pick becomes the shot's start
 * frame in its shot list. GET/POST /api/episodes/{id}/concept-frames...; the API stays the authority:
 * a refused step shows its reason.
 */

type Message = { tone: 'ok' | 'bad'; text: string };
type Pending = { estimate: FrameEstimate; change: string; keepChanges: boolean };

const POLL_MS = 3000;
const hint = 'text-[12.5px] text-ink-soft';
const TONE_TEXT: Record<FrameTone, string> = {
  ok: 'text-green-ink', todo: 'text-mustard-ink', run: 'text-aqua-ink', bad: 'text-coral-ink', wait: 'text-ink-soft',
};
const TONE_EDGE: Record<FrameTone, string> = {
  ok: 'border-l-green', todo: 'border-l-mustard', run: 'border-l-aqua', bad: 'border-l-coral', wait: 'border-l-line',
};

function problemText(e: unknown): string {
  return e instanceof ApiError ? e.message : 'The studio could not be reached. Try again in a moment.';
}

function ratio(aspect: string): string {
  const [w, h] = aspect.split(':').map(Number);
  return w > 0 && h > 0 ? `${w} / ${h}` : '9 / 16';
}

function wide(aspect: string): boolean {
  const [w, h] = aspect.split(':').map(Number);
  return w > h;
}

export default function ConceptFrames({ episodeId, refreshKey, onCount }: {
  episodeId: string;
  refreshKey: string;
  onCount?: (count: { picked: number; shots: number }) => void;
}) {
  const base = `/api/episodes/${encodeURIComponent(episodeId)}/concept-frames`;
  const [view, setView] = useState<FramesView | null>(null);
  const [load, setLoad] = useState<'loading' | 'ok' | 'hidden' | 'error'>('loading');
  const [loadProblem, setLoadProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [pending, setPending] = useState<Record<string, Pending>>({});
  const [messages, setMessages] = useState<Record<string, Message>>({});

  const reload = useCallback(async () => {
    try {
      const next = await apiFetch<FramesView>(base);
      setView(next);
      setLoad('ok');
      setLoadProblem(null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        setLoad('hidden');
      } else {
        setLoad((was) => (was === 'ok' ? was : 'error'));
        setLoadProblem(problemText(e));
      }
    }
  }, [base]);

  useEffect(() => {
    reload();
  }, [reload, refreshKey]);

  const making = anyMaking(view);
  const [polls, setPolls] = useState(0);
  useEffect(() => {
    if (!making) return undefined;
    const timer = setTimeout(async () => {
      await reload();
      setPolls((n) => n + 1);
    }, POLL_MS);
    return () => clearTimeout(timer);
  }, [making, polls, reload]);

  // the wizard opens the page on this section: it scrolls here once the shots have loaded
  const arrived = useRef(false);
  useEffect(() => {
    if (load !== 'ok' || arrived.current) return;
    arrived.current = true;
    if (window.location.hash === '#first-frames') document.getElementById('first-frames')?.scrollIntoView({ block: 'start' });
  }, [load]);

  const { picked, shots: shotCount } = framesCount(view);
  useEffect(() => {
    onCount?.({ picked, shots: shotCount });
  }, [onCount, picked, shotCount]);

  const say = (shotId: string, message: Message | null) => setMessages((all) => {
    const next = { ...all };
    if (message) next[shotId] = message;
    else delete next[shotId];
    return next;
  });
  const putShot = (shot: FrameShot) => setView((was) => (was ? {
    ...was, shots: was.shots.map((s) => (s.shot_id === shot.shot_id ? shot : s)),
  } : was));
  const run = async <T,>(shotId: string, fn: () => Promise<T>, done: (result: T) => string | null): Promise<T | null> => {
    setBusy(shotId);
    say(shotId, null);
    try {
      const result = await fn();
      const text = done(result);
      if (text) say(shotId, { tone: 'ok', text });
      return result;
    } catch (e) {
      say(shotId, { tone: 'bad', text: problemText(e) });
      await reload();
      return null;
    } finally {
      setBusy(null);
    }
  };

  const priceIt = (shot: FrameShot, change: string, keepChanges: boolean) => run(shot.shot_id,
    () => postJson<FrameEstimate>(`${base}/${shot.shot_id}/estimate`, { change, keep_changes: keepChanges }),
    (estimate) => {
      setPending((all) => ({ ...all, [shot.shot_id]: { estimate, change, keepChanges } }));
      return null;
    });
  const make = (shot: FrameShot) => {
    const plan = pending[shot.shot_id];
    if (!plan) return null;
    return run(shot.shot_id, () => postJson<{ round: FrameRound; shot: FrameShot }>(`${base}/${shot.shot_id}`, {
      change: plan.change, keep_changes: plan.keepChanges, approved_total_usd_micros: plan.estimate.total_usd_micros,
    }), (made) => {
      putShot(made.shot);
      setPending((all) => {
        const next = { ...all };
        delete next[shot.shot_id];
        return next;
      });
      return made.round.status === 'making'
        ? 'The studio is making the options now. They show up here as they are made.'
        : `${made.round.made === 1 ? '1 option' : `${made.round.made} options`} ready. Pick the one this shot starts from.`;
    });
  };
  const cancel = (shotId: string) => setPending((all) => {
    const next = { ...all };
    delete next[shotId];
    return next;
  });
  const pick = (shot: FrameShot, option: FrameOption) => run(shot.shot_id,
    () => postJson<{ shot: FrameShot }>(`${base}/${shot.shot_id}/pick`, { option_id: option.option_id }),
    (done) => {
      putShot(done.shot);
      return `${optionName(option)} is the start frame of shot ${shot.shot_id}.`;
    });
  const unpick = (shot: FrameShot) => run(shot.shot_id,
    () => postJson<{ shot: FrameShot }>(`${base}/${shot.shot_id}/unpick`, {}),
    (done) => {
      putShot(done.shot);
      return `Shot ${shot.shot_id} gets its first frame while filming again.`;
    });

  if (load === 'hidden' || (load === 'ok' && (!view || view.shots.length === 0))) return null;
  if (load === 'loading') {
    return (
      <section id="first-frames" className={`${card} scroll-mt-24`} aria-labelledby="first-frames-title">
        <h2 id="first-frames-title" className="text-xl font-semibold text-gray-900">First frames</h2>
        <p role="status" className={`${hint} mt-1`}>Loading the shots…</p>
      </section>
    );
  }
  if (load === 'error' || !view) {
    return (
      <section id="first-frames" className={`${card} scroll-mt-24`} aria-labelledby="first-frames-title">
        <h2 id="first-frames-title" className="text-xl font-semibold text-gray-900">First frames</h2>
        <p role="alert" className="mt-2 text-[13.5px] font-bold text-coral-ink">
          The first frames could not be loaded: {loadProblem}
        </p>
        <button type="button" className={`${btnSecondary} pf-btn-sm mt-3`} onClick={reload}>Try again</button>
      </section>
    );
  }
  // while a run films, the section only shows the frames it starts from
  if (!view.can_change && view.phase !== 'open' && picked === 0) return null;

  return (
    <section id="first-frames" className={`${card} scroll-mt-24`} aria-labelledby="first-frames-title"
      data-testid="first-frames">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="first-frames-title" className="text-xl font-semibold text-gray-900">First frames</h2>
        <p className="text-[14px] font-extrabold" data-testid="first-frames-summary" aria-live="polite">
          {framesSummary(view)}
        </p>
      </div>
      <div className="mt-2 h-2.5 overflow-hidden rounded-full bg-blue-soft" role="progressbar"
        aria-label="Shots with a start frame" aria-valuemin={0} aria-valuemax={shotCount} aria-valuenow={picked}>
        <div className="h-full rounded-full bg-green transition-[width]"
          style={{ width: `${shotCount ? Math.round((100 * picked) / shotCount) : 0}%` }} />
      </div>
      <p className="mt-3 max-w-[70ch] text-[14px]">
        Pick how each shot starts. The studio turns the shot&apos;s moment, characters, place and props into a picture,
        makes {view.options_per_round} options and you pick one: filming starts from it. Not quite right? Say what to
        change in plain words and try again.
      </p>
      <p className={`${hint} mt-1`}>
        Optional: a shot without one gets its first frame while filming.
        {view.dry_run ? ' This is a test run, so the options are free stand-in pictures.' : ''}
      </p>
      {view.words_hidden && (
        <p className={`${hint} mt-1`} data-testid="first-frames-words-hidden">
          The shots&apos; words are only shown to the people working on this video.
        </p>
      )}
      {!view.can_change && (
        <p id="first-frames-locked" className="pf-off-why" data-testid="first-frames-locked">{shotBlocker(view, null)}</p>
      )}
      <ol className="mt-4 space-y-4" data-testid="first-frames-shots">
        {view.shots.map((shot) => (
          <ShotFrames key={shot.shot_id} shot={shot} view={view} busy={busy === shot.shot_id} anyBusy={busy !== null}
            pending={pending[shot.shot_id] ?? null} message={messages[shot.shot_id] ?? null}
            onPrice={(change, keep) => priceIt(shot, change, keep)} onMake={() => make(shot)}
            onCancel={() => cancel(shot.shot_id)} onPick={(option) => pick(shot, option)} onUnpick={() => unpick(shot)} />
        ))}
      </ol>
    </section>
  );
}

function ShotFrames({ shot, view, busy, anyBusy, pending, message, onPrice, onMake, onCancel, onPick, onUnpick }: {
  shot: FrameShot;
  view: FramesView;
  busy: boolean;
  anyBusy: boolean;
  pending: Pending | null;
  message: Message | null;
  onPrice: (change: string, keepChanges: boolean) => void;
  onMake: () => void;
  onCancel: () => void;
  onPick: (option: FrameOption) => void;
  onUnpick: () => void;
}) {
  const [change, setChange] = useState('');
  const status = shotStatus(shot);
  const blocker = shotBlocker(view, shot);
  const off = Boolean(blocker) || anyBusy;
  const latest = latestRound(shot);
  const earlier = shot.rounds.slice(1);
  const kept = changesLine(shot.changes);
  const changeOff = changeBlocker(change);
  const id = shot.shot_id;
  const lockProblem = shot.problems.some((p) => /lock the look/i.test(p));
  // the words that say why the buttons are off: the section's (not theirs, a run going), the shot's
  // problem, or the line under the buttons
  const blockerIsProblem = Boolean(blocker) && shot.problems.includes(blocker as string);
  const whyId = !blocker ? undefined
    : !view.can_change ? 'first-frames-locked' : blockerIsProblem ? `frames-problem-${id}` : `frames-why-${id}`;

  const tryChange = () => {
    onPrice(change.trim(), true);
    setChange('');
  };

  return (
    <li data-testid={`frames-shot-${id}`} data-tone={status.tone}
      className={`rounded-inner border-l-[6px] bg-white p-4 shadow-card ${TONE_EDGE[status.tone]}`}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="text-[18px]">Shot {id}</h3>
          <p className={`text-[13.5px] font-extrabold ${TONE_TEXT[status.tone]}`} data-testid={`frames-status-${id}`}>
            {status.text}
          </p>
        </div>
        {shot.pick?.thumb_url && (
          <a href={`${API_URL}${shot.pick.image_url}`} target="_blank" rel="noreferrer" title="Open the start frame"
            className="block w-16 flex-none overflow-hidden rounded-[12px] shadow-[0_0_0_3px_var(--green)]"
            data-testid={`frames-picked-${id}`}>
            <img src={`${API_URL}${shot.pick.thumb_url}`} alt={`Shot ${id}: the start frame you picked`}
              className="w-full bg-mist object-cover" style={{ aspectRatio: ratio(shot.aspect_ratio) }} />
          </a>
        )}
      </div>

      <p className="mt-2 break-words text-[14px] font-extrabold" data-testid={`frames-in-it-${id}`}>{aboutLine(shot)}</p>
      {shot.beat && (
        <p className="mt-1 break-words text-[13.5px]" data-testid={`frames-beat-${id}`}>
          <span className="font-bold">The moment: </span>{shot.beat}
        </p>
      )}
      <p className="mt-1 break-words text-[13px] text-ink-soft" data-testid={`frames-refs-${id}`}>
        {referencesLine(shot.references, shot.no_photos)}.
      </p>
      {shot.problems.map((problem, i) => (
        <p key={problem} id={i === 0 ? `frames-problem-${id}` : undefined} data-testid={`frames-problem-${id}`}
          className="mt-2 break-words text-[13px] font-bold text-coral-ink">
          {problem}{lockProblem && <> <a href="#cast" className={link}>Go to Cast</a></>}
        </p>
      ))}
      {shot.notes.map((note) => <p key={note} className={`${hint} mt-1 break-words`}>{note}</p>)}

      {shot.pick && (
        <div className="mt-3 flex flex-wrap items-center gap-2 rounded-inner bg-green-soft p-3" data-testid={`frames-pick-${id}`}>
          <p className="min-w-0 flex-1 text-[13.5px] font-bold text-green-ink">
            {shot.pick.source === 'shot_list'
              ? 'This shot starts from a picture set in its shot list.'
              : shot.pick.stale
                ? 'Filming starts from the frame you picked, made before the shot last changed. Make new options to match it.'
                : 'Filming starts from the frame you picked.'}
          </p>
          {shot.pick.source === 'here' && (
            <button type="button" className={`${btnSecondary} pf-btn-sm`} disabled={off} onClick={onUnpick}
              aria-describedby={whyId} data-testid={`frames-unpick-${id}`}>
              Stop using it
            </button>
          )}
        </div>
      )}

      {pending ? (
        <PriceBlock className="mt-3" testId={`frames-price-${id}`} total={priceTotal(pending.estimate)}
          status={priceStatus(pending.estimate)} rows={priceRows(pending.estimate)}>
          {(pending.change || !pending.keepChanges) && (
            <p className="mt-2 break-words text-[12.5px]">
              {pending.change ? `With your change: ${pending.change}.` : 'Starting over from the shot as it is.'}
            </p>
          )}
          {pending.estimate.notes.map((note) => <p key={note} className={`${hint} mt-1 break-words`}>{note}</p>)}
          <div className="mt-3 flex flex-wrap gap-2">
            <button type="button" className={`${btnGo} pf-btn-sm`} disabled={off} onClick={onMake}
              aria-describedby={whyId} data-testid={`frames-make-${id}`}>
              {busy ? 'Starting…' : makeLabel(pending.estimate)}
            </button>
            <button type="button" className={`${btnSecondary} pf-btn-sm`} disabled={busy} onClick={onCancel}
              data-testid={`frames-cancel-${id}`}>
              Not now
            </button>
          </div>
          <p className={`${hint} mt-2`}>
            {pending.estimate.dry_run ? 'Nothing is charged in a test run.'
              : 'The studio checks the price and your balance again before each picture.'}
          </p>
        </PriceBlock>
      ) : !latest ? (
        <div className="mt-3">
          <button type="button" className={`${btnPrimary} pf-btn-sm`} disabled={off} onClick={() => onPrice('', true)}
            aria-describedby={whyId} data-testid={`frames-start-${id}`}>
            {busy ? 'Checking the price…' : `See ${view.options_per_round} options`}
          </button>
        </div>
      ) : null}
      {blocker && view.can_change && !blockerIsProblem && (
        <p id={`frames-why-${id}`} className="pf-off-why" data-testid={`frames-why-${id}`}>{blocker}</p>
      )}

      {message && (
        <p role={message.tone === 'bad' ? 'alert' : 'status'} data-testid={`frames-message-${id}`}
          className={`mt-2 break-words text-[13px] font-bold ${message.tone === 'bad' ? 'text-coral-ink' : 'text-green-ink'}`}>
          {message.text}
        </p>
      )}

      {latest && (
        <FrameRoundGrid round={latest} shot={shot} off={off} busy={busy} whyId={whyId} onPick={onPick} newest />
      )}

      {latest && !pending && (
        <div className="mt-4 rounded-inner bg-blue-soft p-3" data-testid={`frames-change-${id}`}>
          <label htmlFor={`frames-change-text-${id}`} className="text-[14px] font-extrabold">
            Not quite right? Say what to change
          </label>
          <input id={`frames-change-text-${id}`} value={change} maxLength={CHANGE_MAX + 50}
            onChange={(e) => setChange(e.target.value)} placeholder="e.g. make it night, closer on her face"
            className={`${input} mt-1`} data-testid={`frames-change-input-${id}`} />
          <div className="mt-2 flex flex-wrap gap-1.5" aria-label="Ideas">
            {CHANGE_EXAMPLES.map((example) => (
              <button key={example} type="button" className="pf-btn pf-btn-quiet pf-btn-sm !min-h-8 !px-3 !text-[12px]"
                disabled={off} onClick={() => setChange(example)}>
                {example}
              </button>
            ))}
          </div>
          <div className="mt-3 flex flex-wrap gap-2">
            <button type="button" className={`${btnPrimary} pf-btn-sm`} disabled={off || Boolean(changeOff)} onClick={tryChange}
              aria-describedby={whyId ?? (changeOff ? `frames-change-why-${id}` : undefined)}
              data-testid={`frames-try-change-${id}`}>
              Try again with this change
            </button>
            <button type="button" className={`${btnSecondary} pf-btn-sm`} disabled={off} onClick={() => onPrice('', true)}
              aria-describedby={whyId} data-testid={`frames-try-again-${id}`}>
              Try again
            </button>
          </div>
          {changeOff && !off && (
            <p id={`frames-change-why-${id}`} className="pf-off-why" data-testid={`frames-change-why-${id}`}>{changeOff}</p>
          )}
          {kept && (
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <p className="min-w-0 flex-1 break-words text-[12.5px] font-bold" data-testid={`frames-changes-${id}`}>{kept}</p>
              <button type="button" className={`${btnSecondary} pf-btn-sm`} disabled={off} onClick={() => onPrice('', false)}
                aria-describedby={whyId} data-testid={`frames-start-over-${id}`}>
                Start over without them
              </button>
            </div>
          )}
        </div>
      )}

      {earlier.length > 0 && (
        <details className="mt-4" data-testid={`frames-earlier-${id}`}>
          <summary className={`cursor-pointer text-[13px] font-extrabold text-blue-ink ${link}`}>
            Earlier tries ({earlier.length})
          </summary>
          {earlier.map((round) => (
            <FrameRoundGrid key={round.set_id} round={round} shot={shot} off={off} busy={busy} whyId={whyId} onPick={onPick} />
          ))}
        </details>
      )}
    </li>
  );
}

function FrameRoundGrid({ round, shot, off, busy, whyId, onPick, newest = false }: {
  round: FrameRound; shot: FrameShot; off: boolean; busy: boolean; whyId?: string; onPick: (option: FrameOption) => void;
  newest?: boolean;
}) {
  const note = roundNote(round);
  const picked = pickedOption(shot);
  const columns = wide(shot.aspect_ratio) ? 'grid-cols-2 sm:grid-cols-3' : 'grid-cols-3 sm:grid-cols-4';
  return (
    <div className="mt-4" data-testid={`frames-round-${shot.shot_id}-${round.round}`} data-status={round.status}>
      <p className="text-[13.5px] font-extrabold">{newest ? `Latest: ${roundTitle(round)}` : roundTitle(round)}</p>
      {!round.current && (
        <p className="text-[12.5px] font-bold text-mustard-ink">Made before the shot last changed, so it cannot be picked.</p>
      )}
      {note && (
        <p role={round.status === 'making' ? 'status' : undefined}
          className={`text-[12.5px] font-bold ${round.status === 'making' ? 'text-aqua-ink' : 'text-coral-ink'}`}>
          {note}
        </p>
      )}
      <ul className={`mt-2 grid gap-2 ${columns}`}>
        {round.options.map((option) => {
          const chosen = picked === option.option_id;
          return (
            <li key={option.option_id} data-testid={`frames-option-${option.option_id}`} data-picked={chosen ? 'true' : 'false'}
              className={`min-w-0 overflow-hidden rounded-[16px] bg-white ${chosen ? 'shadow-[0_0_0_4px_var(--green)]' : 'shadow-card'}`}>
              {option.thumb_url && option.image_url ? (
                <a href={`${API_URL}${option.image_url}`} target="_blank" rel="noreferrer" className="block"
                  title="Open the full picture">
                  <img src={`${API_URL}${option.thumb_url}`} alt={`Shot ${shot.shot_id}, ${optionName(option)}`} loading="lazy"
                    className="w-full bg-mist object-cover" style={{ aspectRatio: ratio(shot.aspect_ratio) }} />
                </a>
              ) : (
                <div className={`flex w-full items-center justify-center p-2 text-center text-[12px] font-bold ${option.status === 'queued' || option.status === 'running' ? 'bg-aqua-soft text-aqua-ink' : 'bg-mist text-ink-soft'}`}
                  style={{ aspectRatio: ratio(shot.aspect_ratio) }}>
                  {option.status === 'queued' ? 'Waiting its turn' : option.status === 'running' ? 'Being made'
                    : option.status === 'pending_reconcile' ? 'Still with the provider' : 'Not made'}
                </div>
              )}
              <div className="flex flex-col gap-1 p-1.5">
                <span className="px-1 text-[12px] font-extrabold">{chosen ? 'Start frame' : optionName(option)}</span>
                {option.image_url && !chosen && round.current && (
                  <button type="button" className={`${btnGo} pf-btn-sm w-full !min-h-9 !px-2 !text-[12.5px]`} disabled={off}
                    aria-describedby={whyId} onClick={() => onPick(option)} data-testid={`frames-use-${option.option_id}`}>
                    {busy ? 'Saving…' : 'Use this one'}
                  </button>
                )}
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
