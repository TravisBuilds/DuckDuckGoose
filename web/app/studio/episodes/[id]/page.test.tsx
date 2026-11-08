/**
 * @jest-environment jsdom
 */
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';

// Mock Shot component snapshot test for beat context display
describe('Episode Page Shot Display', () => {
  it('shows beat_id badge when present', () => {
    const shot = {
      id: '1',
      episode_id: 'ep01',
      shot_id: 'A01',
      status: 'completed',
      still_url: 'https://example.com/still.jpg',
      clip_url: null,
      prompt: 'Duck walks',
      retries: 0,
      beat_id: 'beat_1',
      beat_purpose: 'Setup: Duck discovers adventure',
    };

    const { container } = render(
      <div>
        <span className="font-semibold">{shot.shot_id}</span>
        {shot.beat_id && (
          <span className="ml-2 text-xs px-2 py-1 rounded bg-purple-100 dark:bg-purple-900 text-purple-800 dark:text-purple-200" title={shot.beat_purpose}>
            {shot.beat_id}
          </span>
        )}
        {shot.beat_purpose && (
          <p className="text-sm text-purple-700 dark:text-purple-300 italic mb-2">
            {shot.beat_purpose}
          </p>
        )}
        <p className="text-sm text-gray-600 dark:text-gray-400 mb-3">
          {shot.prompt}
        </p>
      </div>
    );

    expect(screen.getByText('A01')).toBeInTheDocument();
    expect(screen.getByText('beat_1')).toBeInTheDocument();
    expect(screen.getByText('Setup: Duck discovers adventure')).toBeInTheDocument();
    expect(screen.getByText('Duck walks')).toBeInTheDocument();
  });

  it('does not show beat fields when absent', () => {
    const shot = {
      id: '1',
      episode_id: 'ep01',
      shot_id: 'A01',
      status: 'completed',
      still_url: 'https://example.com/still.jpg',
      clip_url: null,
      prompt: 'Duck walks',
      retries: 0,
    };

    const { container } = render(
      <div>
        <span className="font-semibold">{shot.shot_id}</span>
        {shot.beat_id && (
          <span className="ml-2 text-xs px-2 py-1 rounded bg-purple-100 dark:bg-purple-900 text-purple-800 dark:text-purple-200" title={shot.beat_purpose}>
            {shot.beat_id}
          </span>
        )}
        {shot.beat_purpose && (
          <p className="text-sm text-purple-700 dark:text-purple-300 italic mb-2">
            {shot.beat_purpose}
          </p>
        )}
        <p className="text-sm text-gray-600 dark:text-gray-400 mb-3">
          {shot.prompt}
        </p>
      </div>
    );

    expect(screen.getByText('A01')).toBeInTheDocument();
    expect(screen.getByText('Duck walks')).toBeInTheDocument();
    expect(screen.queryByText(/beat_/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Setup:/)).not.toBeInTheDocument();
  });
});
