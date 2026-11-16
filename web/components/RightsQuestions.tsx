'use client';

import type { RightsAnswers } from '@/lib/cast.mjs';

/**
 * The rights questions every picture or photo upload needs (lib/cast.mjs rightsProblem / rightsFields):
 * the cast's pictures and the props and places photos. The API asks them again (cast_store attestation).
 */
export function RightsQuestions({ rights, onChange, group, testId }: {
  rights: RightsAnswers; onChange: (rights: RightsAnswers) => void; group: string; testId: (what: string) => string;
}) {
  return (
    <fieldset className="mt-3 text-sm space-y-1">
      <legend className="text-xs font-semibold mb-1">Whose picture is this?</legend>
      <label className="flex items-center gap-2">
        <input type="radio" name={group} checked={rights.rights === 'original'}
          onChange={() => onChange({ ...rights, rights: 'original' })} data-testid={testId('rights-original')} />
        I made it myself (my own original work)
      </label>
      <label className="flex items-center gap-2">
        <input type="radio" name={group} checked={rights.rights === 'owned'}
          onChange={() => onChange({ ...rights, rights: 'owned' })} data-testid={testId('rights-owned')} />
        I own the rights to use it
      </label>
      <label className="flex items-center gap-2">
        <input type="checkbox" checked={rights.realPerson}
          onChange={(e) => onChange({ ...rights, realPerson: e.target.checked, likenessConsent: false })}
          data-testid={testId('real-person')} />
        It shows a real person
      </label>
      {rights.realPerson && (
        <label className="flex items-center gap-2 ml-6">
          <input type="checkbox" checked={rights.likenessConsent}
            onChange={(e) => onChange({ ...rights, likenessConsent: e.target.checked })}
            data-testid={testId('likeness-consent')} />
          That person agreed to their likeness being used in this video
        </label>
      )}
    </fieldset>
  );
}
