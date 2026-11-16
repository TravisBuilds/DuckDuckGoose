/**
 * What the creator does next on the episode page, worked out from the run state
 * (GET /api/episodes/{id}, with the shot states of GET .../shot-states and the files of GET .../files
 * when the page has them), the story outline (GET .../story-shape) and the cast (GET .../cast).
 * Pure: the Next step card, the stepper and the studio home's status chips render what this returns.
 * The cast step is advice only: the workflow never waits for it, and the shot plan can be approved
 * without it.
 */

export const STEPS = ['story', 'cast', 'shot_plan', 'stills', 'final_cut', 'download'];

export const STEP_LABELS = {
  story: 'Story',
  cast: 'Cast',
  shot_plan: 'Shot plan',
  stills: 'Scene pictures',
  final_cut: 'Final cut',
  download: 'Download',
};

/** Stages before the shot plan: the story is still being written or waiting for approval. */
const STORY_STAGES = new Set(['info_gathering', 'readback', 'script']);

/** Delivery files that are a video someone can watch, in the order the finish card prefers them. */
export const VIDEO_FILES = ['phone', 'master_subs', 'master_nosubs'];

function upper(value) {
  return value ? String(value).trim().toUpperCase() : null;
}

function passed(state, gate) {
  return (state?.passed_gates ?? []).some((g) => upper(g) === gate);
}

const APPROVAL_KEYS = { G1_SHOT_PLAN: 'g1_shot_plan', G2_SCENE_STILLS: 'g2_scene_stills', G_FINAL_CUT: 'g_final_cut' };

/**
 * The gate waiting for a decision, or null. The run keeps current_gate set after a gate passes (G2
 * stays current while the clips are made), so a passed or approved gate is not open. The workflow
 * withdraws an approval that stops counting (a blocking final-cut QC), which reopens the gate.
 */
export function openGate(state) {
  const gate = upper(state?.current_gate);
  if (!gate || passed(state, gate)) return null;
  const key = APPROVAL_KEYS[gate];
  if (key && state?.approvals?.[key]) return null;
  return gate;
}

/** The story was approved, or the run has moved on past it (story shaping off, or approved earlier). */
export function storyDone(state) {
  if (!state) return false;
  if (state.approvals?.story_shape || passed(state, 'STORY_SHAPE')) return true;
  if (state.current_gate || (state.passed_gates ?? []).length > 0) return true;
  return Boolean(state.stage) && !STORY_STAGES.has(state.stage);
}

/** The Story panel is read-only: approved, moved past, or the run has ended (edits are refused then). */
export function storyLocked(state) {
  return storyDone(state) || Boolean(state?.run?.ended);
}

/**
 * Every shot the run state names, sorted (S01, S02, ...). The run records a shot's outcome only once
 * its clip is done, so at the scene pictures the shot states (GET .../shot-states) are the full list.
 */
export function shotIds(state) {
  const failed = (state?.failed_shots ?? []).map((f) => (typeof f === 'string' ? f : f?.shot_id)).filter(Boolean);
  const ids = new Set([
    ...Object.keys(state?.shot_workflow_ids ?? {}),
    ...Object.keys(state?.shot_outcomes ?? {}),
    ...Object.keys(state?.shot_decisions ?? {}),
    ...failed,
  ]);
  return [...ids].sort();
}

const READY = new Set(['done', 'completed']);

/**
 * The scene pictures at G2: how many wait for the creator's decision (ready or failed, no decision
 * yet) and how many are still being made. Uses the shot states when the page has them, else the
 * run state's shot count.
 */
export function stillsToReview(state, shots) {
  const decisions = state?.shot_decisions ?? {};
  if (Array.isArray(shots) && shots.length > 0) {
    const open = shots.filter((s) => !decisions[s.shot_id]);
    const making = open.filter((s) => !s.pre_approved && !READY.has(s.still_status) && s.still_status !== 'failed');
    return { review: open.length - making.length, making: making.length, total: shots.length };
  }
  const total = Math.max(state?.shots_count ?? 0, shotIds(state).length);
  const decided = Object.keys(decisions).length;
  return { review: Math.max(total - decided, 0), making: 0, total };
}

/**
 * Names of the characters in the shot plan whose look is not locked yet, in the order they first
 * appear. A shot's pick uses the character's usual version unless it names one.
 */
export function charactersToLock(cast) {
  const characters = cast?.characters ?? [];
  if (characters.length === 0) return [];
  const bySlug = new Map(characters.map((c) => [c.slug, c]));
  const names = new Map();
  for (const shot of cast?.shots?.shots ?? []) {
    for (const entry of shot.cast ?? []) {
      const slug = typeof entry === 'string' ? entry : entry?.character;
      const character = bySlug.get(slug);
      if (!character || names.has(slug)) continue;
      const number = (typeof entry === 'object' && entry?.version) || character.default_version;
      const version = (character.versions ?? []).find((v) => v.version === number);
      if (!version?.locked) names.set(slug, character.name || slug);
    }
  }
  return [...names.values()];
}

/** Characters picked for at least one shot. */
function plannedCast(cast) {
  return (cast?.shots?.shots ?? []).some((shot) => (shot.cast ?? []).length > 0);
}

/** "Mara", "Mara and Theo", "Mara, Theo and 2 more". */
export function listNames(names) {
  if (names.length <= 1) return names[0] ?? '';
  if (names.length === 2) return `${names[0]} and ${names[1]}`;
  if (names.length === 3) return `${names[0]}, ${names[1]} and ${names[2]}`;
  return `${names[0]}, ${names[1]} and ${names.length - 2} more`;
}

/**
 * The finish card: is there a video to watch, and which file plays first. For a completed run, and for
 * a run whose history expired (run.outcome "expired") or could not be loaded (no run): the delivered
 * files are on disk whatever happened to the run, so they are shown either way.
 * mine: the viewer made it ("Your video"), else "This video".
 */
export function finishLine(run, files, mine = true) {
  const ready = VIDEO_FILES.filter((type) => files?.[type]?.available);
  if (!run || run.outcome === 'expired') {
    return {
      ready: ready.length > 0,
      primary: ready[0] ?? null,
      title: run ? "This run's history expired" : "This run's history could not be loaded",
      text: ready.length > 0
        ? 'The studio no longer knows how the run ended, but the video it delivered is here to watch and download.'
        : 'The studio no longer knows how the run ended, and no video file is left for it.',
    };
  }
  if (ready.length > 0) {
    return {
      ready: true,
      primary: ready[0],
      title: mine ? 'Your video is ready' : 'This video is ready',
      text: 'Watch it here and download the files you need.',
    };
  }
  if (run?.dry_run) {
    return {
      ready: false,
      primary: null,
      title: mine ? 'Your preview is finished' : 'This preview is finished',
      text: 'A preview run goes through every step without paying for pictures or clips, so it has no video file to download.',
    };
  }
  return {
    ready: false,
    primary: null,
    title: mine ? 'Your video is finished' : 'This video is finished',
    text: 'Its files are not available right now. An admin can check the delivery under Production details.',
  };
}

/**
 * Who reads the Next step. mine: they made the video, or it has no owner ("your video"). canAct: they
 * may take its steps (whoever made it, an admin, or any member while it has no owner). ownerName:
 * who made it. Without a viewer the card speaks to the person who made the video.
 */
const MAKER = { mine: true, canAct: true, ownerName: null };

/** A step only the owner (or an admin) can take, as someone else sees it: who it waits on. */
function waitingOn(who, step, task, text, href, extra = {}) {
  return { step, title: `Waiting on ${who} to ${task}`, text, href, waiting: true, waitingOn: who, ...extra };
}

/**
 * The Next step card: { step, title, text, href, count?, advisory?, also?, waiting?, waitingOn? }.
 * step is one of story | cast | shot_plan | stills | final_cut | done | expired | failed | working.
 * expired: the run's history is gone and nothing saved shows how it ended (or no run state could be
 * read while delivered files exist); the finish card shows what files there are.
 * more: { files (GET .../files), shots (GET .../shot-states shots) } when the page has them, and
 * viewer ({ mine, canAct, ownerName }) when the reader may not be the person who made the video.
 * waiting: the step is someone else's to take (waitingOn names them).
 */
export function nextStep(state, story = null, cast = null, more = {}) {
  const { files = null, shots = null, viewer = null } = more ?? {};
  const { mine, canAct, ownerName } = { ...MAKER, ...(viewer ?? {}) };
  const who = ownerName || 'the owner';
  if (!state) {
    if (VIDEO_FILES.some((type) => files?.[type]?.available)) {
      const finish = finishLine(null, files, mine);
      return { step: 'expired', title: finish.title, text: finish.text, href: '#finished' };
    }
    return {
      step: 'working',
      title: "This video hasn't started yet",
      text: 'Its first step shows up here once it starts.',
      href: null,
    };
  }
  const run = state.run ?? null;
  if (run?.ended) {
    if (run.outcome === 'completed' || run.outcome === 'expired') {
      const finish = finishLine(run, files, mine);
      return { step: run.outcome === 'completed' ? 'done' : 'expired', title: finish.title, text: finish.text, href: '#finished' };
    }
    let text = 'See what went wrong.';
    if (run.can_start_again) {
      text = canAct ? 'See what went wrong, then start it again.' : `See what went wrong. Only ${who} or an admin can start it again.`;
    }
    return { step: 'failed', title: 'This video stopped before it was finished', text, href: '#run-ended' };
  }

  const gate = openGate(state);
  const hasStory = Boolean(state.story_shape || story?.story_shape);
  if (!gate && hasStory && !storyDone(state) && !story?.approved) {
    if (!canAct) {
      return waitingOn(who, 'story', 'approve the story',
        `You can read it below. Only ${who} or an admin can change or approve it.`, '#story');
    }
    return {
      step: 'story',
      title: mine ? 'Read and approve your story' : 'Read and approve the story',
      text: 'Change anything that is not right, then approve it to start planning the shots.',
      href: '#story',
    };
  }

  if (gate === 'G1_SHOT_PLAN') {
    if (!canAct) {
      return waitingOn(who, 'shot_plan', 'approve the shot plan',
        `You can look over the shots below. Only ${who} or an admin can approve them.`, '#shot-plan');
    }
    const names = charactersToLock(cast);
    if (names.length > 0) {
      return {
        step: 'cast',
        title: `Lock the look of ${listNames(names)}`,
        text: 'Optional, but a locked look keeps each face the same in every scene. You can approve the shot plan without it.',
        href: '#cast',
        advisory: true,
        also: { text: 'Go to the shot plan', href: '#shot-plan' },
      };
    }
    return {
      step: 'shot_plan',
      title: 'Approve the shot plan',
      text: 'Look over the shots, then approve them to start making the scene pictures.',
      href: '#shot-plan',
    };
  }

  if (gate === 'G2_SCENE_STILLS') {
    const { review, making, total } = stillsToReview(state, shots);
    if (review > 0 && !canAct) {
      return waitingOn(who, 'stills', `review ${review} scene picture${review === 1 ? '' : 's'}`,
        `You can see them below. Only ${who} or an admin can approve them or ask for a redo.`, '#scene-pictures',
        { count: review });
    }
    if (review > 0) {
      return {
        step: 'stills',
        title: `Review ${review} scene picture${review === 1 ? '' : 's'}`,
        text: making > 0
          ? `${making} more ${making === 1 ? 'is' : 'are'} still being made. Approve each picture or ask for a redo.`
          : 'Approve each picture or ask for a redo, then approve them all to start making the clips.',
        href: '#scene-pictures',
        count: review,
      };
    }
    if (making > 0) {
      return {
        step: 'stills',
        title: mine ? 'Making your scene pictures' : 'Making the scene pictures',
        text: `${total - making} of ${total} ready. They show up below as they finish.`,
        href: '#scene-pictures',
        count: 0,
      };
    }
    if (!canAct) {
      return waitingOn(who, 'stills', 'approve all scene pictures',
        `Every picture has a decision. Only ${who} or an admin can approve them all.`, '#scene-pictures', { count: 0 });
    }
    return {
      step: 'stills',
      title: 'Approve all scene pictures',
      text: 'Every picture has your decision. Approve them all to start making the clips.',
      href: '#scene-pictures',
      count: 0,
    };
  }

  if (gate === 'G_FINAL_CUT') {
    if (!canAct) {
      return waitingOn(who, 'final_cut', 'approve the final cut',
        `You can watch it below. Only ${who} or an admin can approve it or send it back.`, '#final-cut');
    }
    return {
      step: 'final_cut',
      title: 'Watch and approve the final cut',
      text: 'Play the video, then approve it to finish, or send it back with notes.',
      href: '#final-cut',
    };
  }

  if (gate) {
    return {
      step: 'working',
      title: 'Waiting for a review',
      text: 'An admin can see the details under Production details.',
      href: '#production-details',
    };
  }

  return {
    step: 'working',
    title: mine ? 'Working on your video' : 'Working on this video',
    text: `Nothing to do right now. ${mine ? 'Your' : 'The'} next step shows up here when it's ready.`,
    href: null,
  };
}

/**
 * The stepper: Story, Cast, Shot plan, Scene pictures, Final cut, Download, each
 * done | current | todo | skipped | stopped. "skipped" is a step the video went past without it
 * (no story outline, or no cast to lock). Null when the run's history expired: nothing shows which
 * steps it took.
 */
export function stepperSteps(state, story, cast, next) {
  if (next?.step === 'expired' || state?.run?.outcome === 'expired') return null;
  const approvals = state?.approvals ?? {};
  const ended = Boolean(state?.run?.ended);
  const completed = ended && state?.run?.outcome === 'completed';
  const g1 = Boolean(approvals.g1_shot_plan) || passed(state, 'G1_SHOT_PLAN') || completed;
  const hasStory = Boolean(state?.story_shape || story?.story_shape);
  const status = {
    story: storyDone(state) || story?.approved ? (hasStory ? 'done' : 'skipped') : 'todo',
    cast: 'todo',
    shot_plan: g1 ? 'done' : 'todo',
    stills: approvals.g2_scene_stills || passed(state, 'G2_SCENE_STILLS') || completed ? 'done' : 'todo',
    final_cut: approvals.g_final_cut || passed(state, 'G_FINAL_CUT') || completed ? 'done' : 'todo',
    download: completed ? 'current' : 'todo',
  };
  if (!cast || !plannedCast(cast)) status.cast = 'skipped';
  else if (charactersToLock(cast).length === 0) status.cast = 'done';
  else status.cast = g1 ? 'skipped' : 'todo';

  const firstTodo = STEPS.find((key) => status[key] === 'todo');
  if (next?.step === 'failed') {
    if (firstTodo) status[firstTodo] = 'stopped';
  } else if (next && next.step !== 'done') {
    const key = STEPS.includes(next.step) && status[next.step] === 'todo' ? next.step : firstTodo;
    if (key) status[key] = 'current';
  }
  return STEPS.map((key) => ({ key, label: STEP_LABELS[key], status: status[key] }));
}

/** The status chip on a studio home card. */
export function statusChip(next) {
  if (next?.waiting) return { label: `Waiting on ${next.waitingOn}`, tone: 'wait' };
  switch (next?.step) {
    case 'story':
      return { label: 'Story to approve', tone: 'todo' };
    case 'cast':
    case 'shot_plan':
      return { label: 'Shot plan to approve', tone: 'todo' };
    case 'stills':
      return { label: 'Scene pictures to review', tone: 'todo' };
    case 'final_cut':
      return { label: 'Final cut to approve', tone: 'todo' };
    case 'done':
      return { label: 'Finished', tone: 'ok' };
    case 'expired':
      return { label: 'History expired', tone: 'warn' };
    case 'failed':
      return { label: 'Stopped', tone: 'bad' };
    default:
      return { label: 'In progress', tone: 'wait' };
  }
}

/**
 * The studio home card's chip from GET /api/episodes/{id}. Without a run state (a 404: the API knows
 * no run, saved state or file of the episode) it has not started; a run Temporal has purged comes back
 * as the API's archived state and reads like any other run, e.g. "Finished".
 */
export function cardStatus(state, story = null, viewer = null) {
  if (!state) return { label: 'Not started', tone: 'wait' };
  return statusChip(nextStep(state, story, null, { viewer }));
}
