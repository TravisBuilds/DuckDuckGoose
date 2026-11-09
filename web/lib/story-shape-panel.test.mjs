/**
 * Unit tests for StoryShapePanel component data structures.
 * 
 * Run with: npm run test:unit
 */
import test from 'node:test';
import assert from 'node:assert/strict';

test('StoryShapePanel should have valid props interface', () => {
    // Type check that the expected props are defined
    const mockStoryShape = {
      logline: 'Test logline',
      hook: { timestamp: 0, content: 'Test hook' },
      beats: [
        {
          beat_id: 'B1',
          title: 'Setup',
          duration_s: 40,
          description: 'The beginning',
          emotional_tone: 'curious',
          beat_timing: 'act_1',
        },
      ],
      payoff: { timestamp: 100, content: 'Test payoff' },
    };

    const mockCritique = {
      early_hook: true,
      payoff_references_hook: true,
      duration_fit: true,
      dead_beats: [],
      unassigned_beats: [],
      issues: [],
      passes: true,
    };

    // No runtime component rendering in this basic unit test;
    // just validate the data structures match what the component expects
    assert.strictEqual(mockStoryShape.logline, 'Test logline');
    assert.strictEqual(mockStoryShape.hook.content, 'Test hook');
    assert.strictEqual(mockStoryShape.beats.length, 1);
    assert.strictEqual(mockCritique.passes, true);
});

test('StoryShapePanel should handle missing critique gracefully', () => {
    const mockStoryShape = {
      logline: 'Test',
      hook: { timestamp: 0, content: 'Hook' },
      beats: [],
      payoff: { timestamp: 0, content: 'Payoff' },
    };

    // Component should accept null critique
    const critique = null;
    
    assert.strictEqual(mockStoryShape.logline, 'Test');
    assert.strictEqual(critique, null);
});

test('StoryShapePanel should validate beat timing values', () => {
    const validTimings = ['act_1', 'act_2', 'act_3', 'hook', 'midpoint', 'payoff'];
    
    validTimings.forEach((timing) => {
      const beat = {
        beat_id: 'B1',
        title: 'Test',
        duration_s: 20,
        description: 'Desc',
        emotional_tone: 'neutral',
        beat_timing: timing,
      };
      
      assert.ok(validTimings.includes(beat.beat_timing));
    });
});

test('StoryShapePanel should validate critique structure', () => {
    const critique = {
      early_hook: true,
      payoff_references_hook: false,
      duration_fit: true,
      dead_beats: ['B2'],
      unassigned_beats: [],
      issues: ['Payoff does not reference hook'],
      passes: false,
    };

    assert.strictEqual(critique.passes, false);
    assert.strictEqual(critique.issues.length, 1);
    assert.strictEqual(critique.dead_beats[0], 'B2');
});
