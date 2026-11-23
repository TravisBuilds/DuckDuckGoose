/**
 * Plain-language names for the workflow's ids, for the people making videos: review steps (gate ids),
 * pipeline stages, QC verdicts, shot statuses and delivery files. The raw ids appear only under
 * "Production details" on the episode page. No imports: lib/labels.test.mjs loads this file on its own.
 */

export type LabelTone = 'ok' | 'todo' | 'wait' | 'warn' | 'bad';

export const GATE_LABELS: Record<string, string> = {
  STORY_SHAPE: 'Story',
  G1_SHOT_PLAN: 'Shot plan',
  G2_SCENE_STILLS: 'Scene pictures',
  G_FINAL_CUT: 'Final cut',
};

/** "Shot plan" for G1_SHOT_PLAN or g1_shot_plan; unknown review steps get a generic name, never the id. */
export function gateLabel(gate: string | null | undefined): string {
  if (!gate) return 'Nothing to review';
  return GATE_LABELS[gate.trim().toUpperCase()] ?? 'A review step';
}

export const STAGE_LABELS: Record<string, string> = {
  info_gathering: 'Getting started',
  readback: 'Reading your idea back',
  script: 'Writing the story',
  character_locks: 'Setting up the cast',
  storyboard: 'Sketching the storyboard',
  shot_planning: 'Planning the shots',
  still_generation: 'Making the scene pictures',
  still_review: 'Checking the scene pictures',
  clip_generation: 'Turning pictures into clips',
  clip_qc: 'Checking the clips',
  mute_edit: 'Editing the clips together',
  picture_lock: 'Locking the edit',
  audio: 'Adding voice and music',
  final_mix: 'Mixing the sound',
  delivery: 'Preparing the files',
  post_approval: 'Finishing up',
  complete: 'Finished',
};

export function stageLabel(stage: string | null | undefined): string {
  if (!stage) return 'Getting started';
  return STAGE_LABELS[stage.trim().toLowerCase()] ?? 'Working on it';
}

export const QC_LABELS: Record<string, string> = {
  PASS: 'Looks good',
  WATCH: 'Looks fine, worth a quick look',
  NEEDS_REVIEW: 'Needs a person to check it',
  ESCALATE: 'Needs an admin to check it',
  FAIL: 'Needs a fix',
  DRY_RUN: 'Not checked (preview run)',
  NO_CLIPS: 'No clips to check',
  NO_PICTURE: 'No picture to check',
  RESTORED: 'Restored from an earlier run',
};

export function qcLabel(verdict: string | null | undefined): string {
  if (!verdict) return 'Not checked yet';
  return QC_LABELS[verdict.trim().toUpperCase()] ?? 'Checked';
}

export const SHOT_STATUS_LABELS: Record<string, string> = {
  pending: 'Being made',
  running: 'Being made',
  done: 'Ready to review',
  completed: 'Ready to review',
  failed: "Couldn't be made",
  skipped: 'Skipped',
  approved: 'Approved',
  rejected: 'Redo requested',
  qc_failed: 'Flagged by the picture check',
};

export function shotStatusLabel(status: string | null | undefined): string {
  if (!status) return 'Waiting';
  return SHOT_STATUS_LABELS[status.trim().toLowerCase()] ?? 'Waiting';
}

/**
 * The chip on a scene picture: your decision first, then where the picture is. allApproved: the scene
 * pictures step has passed, so a finished picture without a decision of its own went ahead with the rest.
 * flagged: the picture waits for your decision before the pictures can be approved (the picture check
 * failed it), so it never reads as approved with the rest.
 */
export function stillChip(shot: {
  stillStatus?: string | null;
  decision?: string | null;
  retrying?: boolean;
  held?: boolean;
  allApproved?: boolean;
  flagged?: boolean;
}): { label: string; tone: LabelTone } {
  if (shot.held) return { label: 'Held for an admin to check', tone: 'warn' };
  if (shot.retrying) return { label: 'Being made again', tone: 'wait' };
  if (shot.flagged) return { label: 'Flagged by the picture check', tone: 'warn' };
  if (shot.decision === 'approved') return { label: 'Approved', tone: 'ok' };
  if (shot.decision === 'rejected') return { label: 'Redo requested', tone: 'warn' };
  const status = (shot.stillStatus ?? '').toLowerCase();
  if (status === 'failed') return { label: "Couldn't be made", tone: 'bad' };
  if (status === 'done' || status === 'completed') {
    return shot.allApproved ? { label: 'Approved', tone: 'ok' } : { label: 'Ready to review', tone: 'todo' };
  }
  if (status === 'skipped') return { label: 'Skipped', tone: 'wait' };
  return { label: 'Being made', tone: 'wait' };
}

export const FILE_LABELS: Record<string, string> = {
  phone: 'Phone version (9:16)',
  master_subs: 'Full video with subtitles',
  master_nosubs: 'Full video without subtitles',
  subtitles_srt: 'Subtitles file (.srt)',
  mute_edit: 'Picture edit without sound',
};

export function fileLabel(type: string): string {
  return FILE_LABELS[type] ?? type;
}

export function roleLabel(role: string | null | undefined): string {
  if (role === 'admin') return 'Admin';
  if (role === 'member') return 'Member';
  return role ? role.charAt(0).toUpperCase() + role.slice(1) : 'Member';
}

/** Who made a video, as the studio home shows it: "You", their name, or "Shared" when it has no owner. */
export function ownerLabel(
  ownerUserId: string | null | undefined,
  ownerName: string | null | undefined,
  myUserId: string | null | undefined,
): string {
  if (!ownerUserId) return 'Shared';
  if (myUserId && ownerUserId === myUserId) return 'You';
  return ownerName || 'Another member';
}
