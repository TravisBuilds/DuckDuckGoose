export interface PresetData {
  id: string;
  name: string;
  description: string;
  template: string;
  aspect: string;
  lengthRange: [number, number];
  tags: string[];
  briefFields: string[];
  voiceOver: string;
  music: string;
}

// Parsed from genre_presets.draft.yaml - all content rewritten to avoid named IP
export const presets: PresetData[] = [
  {
    id: "nostalgia-time-travel",
    name: "Back to the 2000s",
    description: "A family memory brought back in period detail",
    template: "dialogue_episode",
    aspect: "9:16",
    lengthRange: [25, 40],
    tags: ["nostalgic", "dialogue", "family"],
    briefFields: ["era", "place", "cast_roles", "memory_moment"],
    voiceOver: "dialogue",
    music: "soft nostalgic"
  },
  {
    id: "satire-sketch",
    name: "Satire sketch",
    description: "A one-gag satire with invented stand-ins",
    template: "gag_short",
    aspect: "9:16",
    lengthRange: [20, 30],
    tags: ["comedy", "satire", "quick"],
    briefFields: ["issue", "stand_in_characters", "gag", "punchline"],
    voiceOver: "none",
    music: "orchestral pomp"
  },
  {
    id: "cartoon-made-real",
    name: "Cartoon world, made real",
    description: "Iconic animated moments recreated photoreal",
    template: "music_montage",
    aspect: "9:16",
    lengthRange: [30, 60],
    tags: ["fantasy", "transformation", "cinematic"],
    briefFields: ["source_world", "protagonist", "moments"],
    voiceOver: "none",
    music: "emotional piano"
  },
  {
    id: "farewell-tribute",
    name: "Farewell tribute",
    description: "A single-shot goodbye with a quote card",
    template: "mood_loop",
    aspect: "9:16",
    lengthRange: [15, 30],
    tags: ["emotional", "tribute", "slow"],
    briefFields: ["honoree", "setting", "quote", "dates"],
    voiceOver: "none",
    music: "swelling score"
  },
  {
    id: "living-painting",
    name: "Living painting",
    description: "A slow walk into an illustrated world",
    template: "mood_loop",
    aspect: "9:16",
    lengthRange: [10, 15],
    tags: ["artistic", "ambient", "relaxing"],
    briefFields: ["world", "palette", "figure", "creature"],
    voiceOver: "none",
    music: "ambient"
  },
  {
    id: "what-if-ending",
    name: "What-if ending",
    description: "Retell a story moment with a new ending",
    template: "music_montage",
    aspect: "16:9",
    lengthRange: [45, 75],
    tags: ["storytelling", "alternate", "emotional"],
    briefFields: ["story_world", "characters", "changed_moment", "ending"],
    voiceOver: "caption_only",
    music: "melancholic score"
  },
  {
    id: "cozy-hangout",
    name: "Cozy hangout loop",
    description: "Your characters chilling in one cozy scene",
    template: "mood_loop",
    aspect: "9:16",
    lengthRange: [10, 15],
    tags: ["cozy", "lofi", "relaxing"],
    briefFields: ["place", "characters", "activity", "time_of_day"],
    voiceOver: "none",
    music: "lo-fi"
  },
  {
    id: "original-mini-movie",
    name: "Original mini-movie",
    description: "A short wordless journey story",
    template: "music_montage",
    aspect: "9:16",
    lengthRange: [60, 90],
    tags: ["cinematic", "journey", "original"],
    briefFields: ["hero", "goal", "stops", "ending"],
    voiceOver: "caption_only",
    music: "gentle orchestral"
  },
  {
    id: "mascot-sketch",
    name: "Recurring mascot sketch",
    description: "Episodic comedy starring your non-human mascot",
    template: "dialogue_episode",
    aspect: "16:9",
    lengthRange: [30, 45],
    tags: ["comedy", "episodic", "mascot"],
    briefFields: ["mascot", "setting", "gag", "side_characters"],
    voiceOver: "dialogue",
    music: "comedic SFX"
  },
  {
    id: "dreamscape",
    name: "Dreamscape wallpaper",
    description: "Your character facing a surreal dream sky",
    template: "mood_loop",
    aspect: "9:16",
    lengthRange: [10, 20],
    tags: ["surreal", "wallpaper", "atmospheric"],
    briefFields: ["character", "setting", "phenomenon"],
    voiceOver: "none",
    music: "dreamy"
  },
  {
    id: "animal-remake",
    name: "Animal cast remake",
    description: "A story replayed by animals",
    template: "dialogue_episode",
    aspect: "16:9",
    lengthRange: [45, 75],
    tags: ["animals", "parody", "wholesome"],
    briefFields: ["story", "animal_cast", "episode_beat"],
    voiceOver: "dialogue",
    music: "score pastiche"
  },
  {
    id: "ambience-journey",
    name: "Sleep / ambience journey",
    description: "A few calm scenes then a long relaxing hold",
    template: "mood_loop",
    aspect: "4:3",
    lengthRange: [60, 600],
    tags: ["ambient", "sleep", "relaxing"],
    briefFields: ["vessel_or_place", "traveller", "destination", "ambience"],
    voiceOver: "none",
    music: "ambient"
  },
  {
    id: "original-series",
    name: "Original series episode",
    description: "An episode of your own animated series",
    template: "dialogue_episode",
    aspect: "16:9",
    lengthRange: [90, 180],
    tags: ["series", "animated", "original"],
    briefFields: ["series_bible", "premise", "cast", "cliffhanger"],
    voiceOver: "dialogue",
    music: "score"
  },
  {
    id: "cute-greeting",
    name: "Cute holiday greeting",
    description: "Plush mascots wishing a happy occasion",
    template: "cozy_micro",
    aspect: "9:16",
    lengthRange: [10, 15],
    tags: ["cute", "greeting", "wholesome"],
    briefFields: ["mascots", "occasion", "greeting_text"],
    voiceOver: "caption_only",
    music: "soft"
  },
  {
    id: "mascot-day",
    name: "Mascot day-in-the-life",
    description: "Your animal mascot runs a tiny errand",
    template: "cozy_micro",
    aspect: "9:16",
    lengthRange: [15, 20],
    tags: ["cute", "mascot", "slice-of-life"],
    briefFields: ["mascot", "outfit", "errand", "payoff"],
    voiceOver: "caption_only",
    music: "upbeat"
  },
  {
    id: "ride-along-pov",
    name: "Ride-along POV",
    description: "First-person ride through an epic landscape",
    template: "continuous_pov",
    aspect: "9:16",
    lengthRange: [20, 40],
    tags: ["pov", "adventure", "immersive"],
    briefFields: ["mount", "landscape", "destination", "mood"],
    voiceOver: "none",
    music: "ambient + wind"
  },
  {
    id: "character-spotlight",
    name: "Character spotlight loop",
    description: "Your character's silhouette in a dramatic spotlight",
    template: "mood_loop",
    aspect: "9:16",
    lengthRange: [10, 15],
    tags: ["dramatic", "silhouette", "atmospheric"],
    briefFields: ["character", "motif", "palette"],
    voiceOver: "none",
    music: "atmospheric"
  },
  {
    id: "3d-moment",
    name: "3D character moment",
    description: "One continuous emotional beat between two characters",
    template: "continuous_pov",
    aspect: "16:9",
    lengthRange: [15, 30],
    tags: ["emotional", "3d", "cinematic"],
    briefFields: ["character_a", "character_b", "setting", "beat"],
    voiceOver: "none",
    music: "bed"
  },
  {
    id: "anime-cooking",
    name: "Cozy anime cooking",
    description: "A hand-painted anime dish coming together",
    template: "cozy_micro",
    aspect: "9:16",
    lengthRange: [10, 15],
    tags: ["cozy", "food", "anime"],
    briefFields: ["dish", "kitchen", "hero_moment"],
    voiceOver: "none",
    music: "ASMR SFX"
  },
  {
    id: "rewrite-the-scene",
    name: "Rewrite the scene",
    description: "Step in and change a sad story moment",
    template: "gag_short",
    aspect: "9:16",
    lengthRange: [10, 20],
    tags: ["remix", "intervention", "heartwarming"],
    briefFields: ["moment", "intervener", "outcome"],
    voiceOver: "caption_only",
    music: "bed"
  },
  {
    id: "live-action-fight",
    name: "Live-action fight scene",
    description: "A one-take showdown between original fighters",
    template: "action_sequence",
    aspect: "16:9",
    lengthRange: [20, 30],
    tags: ["action", "intense", "cinematic"],
    briefFields: ["fighters", "arena", "combat_style", "finisher"],
    voiceOver: "none",
    music: "SFX-heavy"
  },
  {
    id: "fullscreen-action-anime",
    name: "Full-screen action anime",
    description: "Action anime rotated to fill the phone",
    template: "action_sequence",
    aspect: "9:16",
    lengthRange: [30, 45],
    tags: ["action", "anime", "intense"],
    briefFields: ["hero", "ally", "set_piece", "rescue_beat"],
    voiceOver: "none",
    music: "score + SFX"
  },
  {
    id: "creature-legend",
    name: "Fantasy creature legend",
    description: "A painterly legend of magical creatures",
    template: "music_montage",
    aspect: "4:3",
    lengthRange: [60, 180],
    tags: ["fantasy", "painterly", "narration"],
    briefFields: ["creatures", "legend", "world", "closing_line"],
    voiceOver: "narration",
    music: "score"
  },
  {
    id: "time-travel-vlog",
    name: "Time-travel history vlog",
    description: "A host vlogs from a real historical moment",
    template: "continuous_pov",
    aspect: "9:16",
    lengthRange: [45, 60],
    tags: ["educational", "history", "vlog"],
    briefFields: ["date_place", "host", "event", "twist"],
    voiceOver: "dialogue",
    music: "light score"
  }
];

export function getPresetById(id: string): PresetData | undefined {
  return presets.find(p => p.id === id);
}

export function getPresetsByTag(tag: string): PresetData[] {
  return presets.filter(p => p.tags.includes(tag));
}
