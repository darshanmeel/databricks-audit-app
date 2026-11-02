// Spend bars against the average of the bars before them (7 days, or 3 months): 30%+ under turns
// green, 15-30% over orange, 30%+ over red; anything in between keeps the colour before. One rule
// for every daily and monthly spend chart, buildMonthBars (hooks.ts) included.

export type Tone = "calm" | "steady" | "warn" | "crit";
export const TONE_DAYS = 7;
export const TONE_MONTHS = 3;
export const TONE_COLOR: Record<Tone, string> = { calm: "var(--st-ok)", steady: "var(--c1)", warn: "var(--st-warn)", crit: "var(--st-crit)" };

export function nextTone(pct: number, prev: Tone | null): Tone {
  if (pct <= -30) return "calm";
  if (pct > 30) return "crit";
  if (pct > 15) return "warn";
  return prev || "steady";
}

// Each value's tone; skipped values (a part day) and values without `lookback` whole ones before
// them keep the tone so far.
export function trendTones(values: number[], lookback: number, skip?: (boolean | undefined)[]): (Tone | null)[] {
  let tone: Tone | null = null;
  return values.map((v, i) => {
    if (skip && skip[i]) return tone;
    const prior = values.map((x, k) => ({ x, k })).slice(Math.max(0, i - lookback), i).filter((p) => !(skip && skip[p.k]));
    if (prior.length < lookback) return tone;
    const avg = prior.reduce((a, p) => a + p.x, 0) / prior.length;
    if (avg > 0) tone = nextTone(((v - avg) / avg) * 100, tone);
    else if (v > 0) tone = "crit";
    return tone;
  });
}

export const toneColors = (tones: (Tone | null)[]) => tones.map((t) => (t && t !== "steady" ? TONE_COLOR[t] : null));

export const TONE_LEGEND = [
  { label: "30%+ under the average before", color: TONE_COLOR.calm },
  { label: "15-30% over", color: TONE_COLOR.warn },
  { label: "30%+ over", color: TONE_COLOR.crit },
];
