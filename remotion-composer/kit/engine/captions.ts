// Grouping spoken words into the short phrases a caption shows at once. How a phrase LOOKS is
// the look's business; which words belong together and for how long is decided here.

import {isRtl, isUnspaced, norm} from './words.ts';

export type CaptionRules = {
  /** Most words in one phrase. */
  maxWords: number;
  /** A phrase of two or more words closes once it holds this many characters. */
  maxChars: number;
  /** A pause longer than this (seconds) closes the phrase. */
  gap: number;
  /** A phrase stays up until the next one starts when the pause is shorter than this. */
  holdGap: number;
  /** Otherwise it stays this long after its last word. */
  tail: number;
};

export const DEFAULT_RULES: CaptionRules = {maxWords: 4, maxChars: 24, gap: 0.38, holdGap: 0.5, tail: 0.2};
// Scripts without spaces arrive as much smaller tokens; count characters, not tokens.
const UNSPACED_RULES: Partial<CaptionRules> = {maxWords: 14, maxChars: 12};

export type TimedWord = {text: string; start: number; end: number; part?: string};
export type GroupSec = {words: TimedWord[]; start: number; end: number; visibleEnd: number; spaced: boolean; rtl: boolean};

const SENT_END = /[.!?…。！？]["')\]」』]?$/;
const SOFT_END = /[,:;—–、，-]["')\]]?$/;

export function groupWords(input: TimedWord[], rules: Partial<CaptionRules>, maxVisibleEnd: number): GroupSec[] {
  const words = [...input].sort((a, b) => a.start - b.start).filter((w) => w.text.trim().length > 0);
  if (!words.length) return [];
  const unspaced = isUnspaced(words.map((w) => w.text).join(''));
  const r: CaptionRules = {...DEFAULT_RULES, ...(unspaced ? UNSPACED_RULES : {}), ...rules};

  const groups: TimedWord[][] = [];
  let cur: TimedWord[] = [];
  words.forEach((w, i) => {
    cur.push(w);
    const next = words[i + 1];
    const pause = next ? next.start - w.end : Infinity;
    const chars = cur.reduce((n, x) => n + x.text.length, 0);
    const close =
      !next ||
      SENT_END.test(w.text) ||
      (SOFT_END.test(w.text) && cur.length >= 2) ||
      cur.length >= r.maxWords ||
      pause > r.gap ||
      (next.part !== w.part && cur.length >= 2) ||
      (chars >= r.maxChars && cur.length >= 2);
    if (close) {
      groups.push(cur);
      cur = [];
    }
  });

  // A single word trailing a full phrase reads as an orphan: move the phrase's last word over.
  for (let i = 1; i < groups.length; i++) {
    const g = groups[i];
    const p = groups[i - 1];
    const lastP = p[p.length - 1];
    if (
      g.length === 1 &&
      p.length >= r.maxWords &&
      p.length >= 3 &&
      !SENT_END.test(lastP.text) &&
      g[0].start - lastP.end < r.gap &&
      lastP.part === g[0].part
    ) {
      g.unshift(p.pop() as TimedWord);
    }
  }

  return groups.map((ws, i) => {
    const start = ws[0].start;
    const end = ws[ws.length - 1].end;
    const nextStart = groups[i + 1]?.[0].start;
    const visibleEnd = nextStart !== undefined && nextStart - end < r.holdGap ? nextStart : end + r.tail;
    const text = ws.map((w) => w.text).join('');
    return {words: ws, start, end, visibleEnd: Math.min(visibleEnd, maxVisibleEnd), spaced: !unspaced, rtl: isRtl(text)};
  });
}

/** Phrases the director set by hand: each takes the next spoken words that spell it. Returns
 *  the words in groups, how many words were consumed, and the first phrase that did not match. */
export function takePhrases(words: TimedWord[], phrases: string[]): {groups: TimedWord[][]; used: number; failed: string | null} {
  const groups: TimedWord[][] = [];
  let i = 0;
  for (const phrase of phrases) {
    const tokens = phrase.split(/\s+/).filter(Boolean);
    const want = tokens.map(norm).filter(Boolean);
    if (!want.length) continue;
    // A phrase token may cover several spoken tokens ("24MP+" spoken as "24", "MP").
    const taken: TimedWord[] = [];
    let j = i;
    let ok = true;
    for (let k = 0; k < tokens.length; k++) {
      const target = norm(tokens[k]);
      if (!target) continue;
      let acc = '';
      const start = j;
      while (j < words.length && acc.length < target.length) acc += norm(words[j++].text);
      if (acc !== target) {
        ok = false;
        break;
      }
      taken.push({text: tokens[k], start: words[start].start, end: words[j - 1].end, part: words[start].part});
    }
    if (!ok) return {groups, used: i, failed: phrase};
    groups.push(taken);
    i = j;
  }
  return {groups, used: i, failed: null};
}

/** Timing for groups whose words are already chosen. */
export function timeGroups(groups: TimedWord[][], rules: Partial<CaptionRules>, maxVisibleEnd: number): GroupSec[] {
  const r: CaptionRules = {...DEFAULT_RULES, ...rules};
  return groups.map((ws, i) => {
    const start = ws[0].start;
    const end = ws[ws.length - 1].end;
    const nextStart = groups[i + 1]?.[0].start;
    const visibleEnd = nextStart !== undefined && nextStart - end < r.holdGap ? nextStart : end + r.tail;
    const text = ws.map((w) => w.text).join('');
    return {words: ws, start, end, visibleEnd: Math.min(visibleEnd, maxVisibleEnd), spaced: !isUnspaced(text), rtl: isRtl(text)};
  });
}
