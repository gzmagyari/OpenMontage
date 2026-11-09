// Finding spoken words. Cuts and overlays are pinned to words of the script, so matching
// ignores case, punctuation and hyphens ("re-renders," finds "re-renders").

export type SpokenWord = {text: string; start: number; end: number; line: string; part: string};

export const norm = (s: string): string =>
  s.normalize('NFKC').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '');

const EPS = 1e-6;

/** Every place the phrase is spoken, in order: the matched words of each occurrence. */
export function occurrences(words: SpokenWord[], phrase: string, line?: string): SpokenWord[][] {
  const want = phrase.split(/\s+/).map(norm).filter(Boolean);
  if (!want.length) return [];
  const pool = line === undefined ? words : words.filter((w) => w.line === line);
  const found: SpokenWord[][] = [];
  for (let i = 0; i + want.length <= pool.length; i++) {
    let ok = true;
    for (let k = 0; k < want.length; k++) {
      if (norm(pool[i + k].text) !== want[k]) {
        ok = false;
        break;
      }
    }
    if (ok) found.push(pool.slice(i, i + want.length));
  }
  return found;
}

export function findPhrase(
  words: SpokenWord[],
  phrase: string,
  opts: {after?: number; line?: string; nth?: number} = {},
): SpokenWord[] | null {
  const after = opts.after ?? -Infinity;
  const hits = occurrences(words, phrase, opts.line).filter((m) => m[0].start >= after - EPS);
  return hits[(opts.nth ?? 1) - 1] ?? null;
}

/** Words of the script that look like the one that was not found, for the error message. */
export function suggestions(words: SpokenWord[], phrase: string, limit = 5): string[] {
  const first = norm(phrase.split(/\s+/)[0] ?? '');
  if (!first) return [];
  const seen = new Set<string>();
  const out: string[] = [];
  for (const w of words) {
    const n = norm(w.text);
    if (!n || seen.has(n)) continue;
    const close = n.startsWith(first.slice(0, 3)) || first.startsWith(n.slice(0, 3)) || n.includes(first) || first.includes(n);
    if (close) {
      seen.add(n);
      out.push(w.text.replace(/[.,!?;:]+$/, ''));
      if (out.length >= limit) break;
    }
  }
  return out;
}

/** True for scripts written without spaces between words (Chinese, Japanese, Thai, ...). */
export const isUnspaced = (text: string): boolean =>
  /[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\u0e00-\u0e7f\u1780-\u17ff]/u.test(text);

/** A script's words as captions show them. Text written with spaces splits on them; text
 *  written without (Chinese, Japanese, Thai, ...) is segmented into words, and punctuation
 *  rides with the word before it. */
export function tokenize(text: string): string[] {
  if (!isUnspaced(text)) return text.split(/\s+/).filter(Boolean);
  const out: string[] = [];
  for (const piece of new Intl.Segmenter(undefined, {granularity: 'word'}).segment(text)) {
    const s = piece.segment.trim();
    if (!s) continue;
    if (piece.isWordLike || !out.length) out.push(s);
    else out[out.length - 1] += s;
  }
  return out;
}

/** True for right-to-left scripts (Arabic, Hebrew, ...). */
export const isRtl = (text: string): boolean => /[\u0590-\u08ff\ufb1d-\ufdff\ufe70-\ufefc]/u.test(text);
