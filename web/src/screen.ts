import { sanitizeAnsi } from './protocol';
import { VS15_NARROW, VS16_WIDE, WIDE, ZERO } from './widths';

// Kitty's `get-text --ansi` output as styled rows, plus row-level diffs so the view can
// patch only what changed instead of redrawing the whole history.

export interface Sgr {
  readonly key: string; readonly fg: string | null; readonly bg: string | null;
  readonly bold: boolean; readonly dim: boolean; readonly italic: boolean; readonly underline: boolean;
  readonly inverse: boolean; readonly strike: boolean; readonly hidden: boolean;
}
type Attrs = { -readonly [K in Exclude<keyof Sgr, 'key'>]: Sgr[K] };
/** One terminal row: sanitized text with SGR sequences, its inherited start style and end style. */
export interface Line { readonly raw: string; readonly style: Sgr; readonly end: Sgr }
/** Rows as received, aligned with their prepared lines so the next update can reuse them. */
export interface Screen { readonly rows: readonly string[]; readonly lines: readonly Line[] }
export const EMPTY_SCREEN: Screen = { rows: [], lines: [] };
/** Drop rows from the top, then replace `remove` rows at `start` with `insert` rows of the new content. */
export interface Patch { drop: number; start: number; remove: number; insert: number }
/**
 * `cells` is null for ASCII text; otherwise the run is boxed to its exact terminal width.
 * `cjk` runs hold full-em ideographs that can be spaced out to two cells each.
 */
export interface Run { text: string; style: Sgr; cells: number | null; cjk: boolean }

const FLAGS = ['bold', 'dim', 'italic', 'underline', 'inverse', 'strike', 'hidden'] as const;
function freeze(a: Attrs): Sgr {
  const key = `${a.fg ?? ''}|${a.bg ?? ''}|${FLAGS.map(f => (a[f] ? 1 : 0)).join('')}`;
  return Object.freeze({ ...a, key });
}
export const PLAIN = freeze({ fg: null, bg: null, bold: false, dim: false, italic: false,
  underline: false, inverse: false, strike: false, hidden: false });

// xterm.js default palette, kept so colours look the same as the previous renderer.
const BASE = ['#2e3436', '#cc0000', '#4e9a06', '#c4a000', '#3465a4', '#75507b', '#06989a', '#d3d7cf',
  '#555753', '#ef2929', '#8ae234', '#fce94f', '#729fcf', '#ad7fa8', '#34e2e2', '#eeeeec'];
const LEVELS = [0, 95, 135, 175, 215, 255];
function palette(n: number): string | null {
  if (!Number.isInteger(n) || n < 0 || n > 255) return null;
  if (n < 16) return BASE[n];
  if (n < 232) {
    const i = n - 16;
    return `rgb(${LEVELS[Math.floor(i / 36)]},${LEVELS[Math.floor(i / 6) % 6]},${LEVELS[i % 6]})`;
  }
  const g = 8 + 10 * (n - 232);
  return `rgb(${g},${g},${g})`;
}
function rgb(values: string[]): string | null {
  const n = values.map(Number);
  return n.length === 3 && n.every(v => Number.isInteger(v) && v >= 0 && v <= 255) ? `rgb(${n.join(',')})` : null;
}
/** Colon form after the 38/48/58 code: `5:n`, `2:r:g:b` or `2:colourspace:r:g:b`. */
function colonColor(parts: string[]): string | null {
  if (parts[0] === '5') return palette(Number(parts[1]));
  return parts[0] === '2' ? rgb(parts.slice(-3)) : null;
}

export function applySgr(state: Sgr, params: string): Sgr {
  const s: Attrs = { ...state };
  const list = params.split(';');
  for (let i = 0; i < list.length; i++) {
    const parts = list[i].split(':');
    const code = parts[0] === '' ? 0 : Number(parts[0]);
    if (code === 38 || code === 48 || code === 58) {
      let color: string | null = null;
      if (parts.length > 1) color = colonColor(parts.slice(1));
      else if (list[i + 1] === '5') { color = palette(Number(list[i + 2])); i += 2; }
      else if (list[i + 1] === '2') { color = rgb(list.slice(i + 2, i + 5)); i += 4; }
      if (color && code === 38) s.fg = color;
      if (color && code === 48) s.bg = color;
      continue;
    }
    if (code === 0) Object.assign(s, PLAIN);
    else if (code === 1) s.bold = true;
    else if (code === 2) s.dim = true;
    else if (code === 3) s.italic = true;
    else if (code === 4) s.underline = parts[1] !== '0';
    else if (code === 7) s.inverse = true;
    else if (code === 8) s.hidden = true;
    else if (code === 9) s.strike = true;
    else if (code === 21) s.underline = true;
    else if (code === 22) s.bold = s.dim = false;
    else if (code === 23) s.italic = false;
    else if (code === 24) s.underline = false;
    else if (code === 27) s.inverse = false;
    else if (code === 28) s.hidden = false;
    else if (code === 29) s.strike = false;
    else if (code >= 30 && code <= 37) s.fg = palette(code - 30);
    else if (code === 39) s.fg = null;
    else if (code >= 40 && code <= 47) s.bg = palette(code - 40);
    else if (code === 49) s.bg = null;
    else if (code >= 90 && code <= 97) s.fg = palette(code - 82);
    else if (code >= 100 && code <= 107) s.bg = palette(code - 92);
  }
  const next = freeze(s);
  return next.key === state.key ? state : next;
}

const CSI = /\x1b\[([0-9:;?]*)([ -/]*)([@-~])/g;
const isSgr = (m: RegExpMatchArray) => m[3] === 'm' && !m[2] && !m[1].includes('?');
function endStyle(raw: string, style: Sgr): Sgr {
  for (const m of raw.matchAll(CSI)) if (isSgr(m)) style = applySgr(style, m[1]);
  return style;
}
const isBlank = (raw: string) => !raw.replace(CSI, '').trim();
// Printable text and SGR only is already safe, so the per-character sanitizer can be skipped.
const SAFE_ROW = /^(?:[^\x00-\x08\x0a-\x1f\x7f-\x9f]|\x1b\[[0-9:;]*m)*$/;
const cleanRow = (row: string) => (SAFE_ROW.test(row) ? row : sanitizeAnsi(row));

/**
 * Split content into sanitized rows without trailing empty screen rows. Rows that match the
 * previous content are reused as-is, so an update only parses the rows that changed.
 */
export function prepareScreen(content: string, prev: Screen = EMPTY_SCREEN): Screen {
  const rows = content.replace(/\r/g, '').split('\n');
  while (rows.length > 1 && isBlank(cleanRow(rows[rows.length - 1]))) rows.pop();
  const { drop, start, remove, insert } = align(prev.rows, rows, (a, b) => a === b);
  const tailFrom = start + insert;
  const lines: Line[] = [];
  let style = PLAIN;
  for (let i = 0; i < rows.length; i++) {
    const from = i < start ? drop + i : i >= tailFrom ? drop + start + remove + i - tailFrom : -1;
    const old = from >= 0 ? prev.lines[from] : undefined;
    if (old && old.style.key === style.key) { lines.push(old); style = old.end; continue; }
    const raw = old ? old.raw : cleanRow(rows[i]);
    const end = raw.includes('\x1b') ? endStyle(raw, style) : style;
    lines.push({ raw, style, end });
    style = end;
  }
  return { rows, lines };
}

/** Binary search in a flat, sorted list of inclusive [first, last] pairs. */
function inTable(table: readonly number[], cp: number) {
  let lo = 0, hi = table.length / 2 - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (cp < table[2 * mid]) hi = mid - 1;
    else if (cp > table[2 * mid + 1]) lo = mid + 1;
    else return true;
  }
  return false;
}
// Wide CJK text drawn one em wide by CJK fonts, so it can be spaced out to two cells.
const CJK_TEXT = [0x1100, 0x115f, 0x2e80, 0x303e, 0x3041, 0x33ff, 0x3400, 0x4dbf, 0x4e00, 0x9fff,
  0xa000, 0xa4cf, 0xa960, 0xa97f, 0xac00, 0xd7a3, 0xf900, 0xfaff, 0xfe10, 0xfe19, 0xfe30, 0xfe6f,
  0xff00, 0xff60, 0xffe0, 0xffe6, 0x20000, 0x3fffd];
const ZWJ = 0x200d, VS15 = 0xfe0e, VS16 = 0xfe0f;
const isSkinTone = (cp: number) => cp >= 0x1f3fb && cp <= 0x1f3ff;
const isRegional = (cp: number) => cp >= 0x1f1e6 && cp <= 0x1f1ff;

export function cellWidth(cp: number): 0 | 1 | 2 {
  if (cp < 0x80) return 1;
  if (inTable(ZERO, cp)) return 0;
  return inTable(WIDE, cp) ? 2 : 1;
}

/**
 * Split non-ASCII text into CJK and other runs sized like Kitty: variation selectors widen or
 * narrow emoji, and joiners, skin tones and flag pairs stay inside one wide character.
 */
function pushWide(runs: Run[], text: string, style: Sgr) {
  let run = '', cells = 0, cjk = false;
  let prev = 0, base = 0, openFlag = false;
  for (const ch of text) {
    const cp = ch.codePointAt(0)!;
    let width: number = cellWidth(cp);
    if (prev === ZWJ) width = 0;
    else if (cp === VS16) width = base === 1 && inTable(VS16_WIDE, prev) ? 1 : 0;
    else if (cp === VS15) width = base === 2 && inTable(VS15_NARROW, prev) ? -1 : 0;
    else if (isSkinTone(cp) && base === 2) width = 0;
    else if (isRegional(cp) && openFlag) width = 0;
    openFlag = isRegional(cp) && !openFlag;
    const starts = prev !== ZWJ && cellWidth(cp) > 0 && width > 0;
    const kind: boolean = starts ? width === 2 && inTable(CJK_TEXT, cp) : cjk;
    if (run && kind !== cjk) { runs.push({ text: run, style, cells, cjk }); run = ''; cells = 0; }
    run += ch; cells += width; cjk = kind;
    base = starts ? width : base + width;
    prev = cp;
  }
  if (run) runs.push({ text: run, style, cells, cjk });
}
function pushRuns(runs: Run[], text: string, style: Sgr) {
  let last = 0;
  for (const m of text.matchAll(/[^\x00-\x7f]+/g)) {
    // A combining mark or selector after ASCII belongs to that ASCII character.
    const from = m.index > last && cellWidth(m[0].codePointAt(0)!) === 0 ? m.index - 1 : m.index;
    if (from > last) runs.push({ text: text.slice(last, from), style, cells: null, cjk: false });
    pushWide(runs, text.slice(from, m.index + m[0].length), style);
    last = m.index + m[0].length;
  }
  if (last < text.length) runs.push({ text: text.slice(last), style, cells: null, cjk: false });
}

/** Text runs of one row; non-SGR sequences such as cursor positioning are ignored. */
export function lineRuns(line: Pick<Line, 'raw' | 'style'>): Run[] {
  const runs: Run[] = [];
  let style = line.style, text = '', last = 0;
  for (const m of line.raw.matchAll(CSI)) {
    text += line.raw.slice(last, m.index);
    last = m.index + m[0].length;
    if (!isSgr(m)) continue;
    const next = applySgr(style, m[1]);
    if (next.key === style.key) continue;
    if (text) pushRuns(runs, text, style);
    text = ''; style = next;
  }
  text += line.raw.slice(last);
  if (text) pushRuns(runs, text, style);
  return runs;
}

// A top drop needs this many matching rows (or all remaining new rows) to count as scrolled-off
// history; runs are compared up to MATCH_CAP rows and the longest wins, ties keeping drop 0.
const DROP_CONFIRM_ROWS = 8;
const MATCH_CAP = 64;
type Same<T> = (a: T, b: T) => boolean;
function matchRun<T>(prev: readonly T[], next: readonly T[], k: number, same: Same<T>) {
  const limit = Math.min(MATCH_CAP, prev.length - k, next.length);
  let i = 0;
  while (i < limit && same(prev[k + i], next[i])) i++;
  return i;
}
function scrolledOff<T>(prev: readonly T[], next: readonly T[], same: Same<T>) {
  let best = 0, bestRun = matchRun(prev, next, 0, same);
  const needed = Math.min(DROP_CONFIRM_ROWS, next.length);
  for (let k = 1; k < prev.length && bestRun < MATCH_CAP; k++) {
    if (!same(prev[k], next[0])) continue;
    const run = matchRun(prev, next, k, same);
    if (run >= needed && run > bestRun) { best = k; bestRun = run; }
  }
  return best;
}
function align<T>(prev: readonly T[], next: readonly T[], same: Same<T>): Patch {
  const drop = scrolledOff(prev, next, same);
  const rest = prev.length - drop;
  let start = 0;
  while (start < rest && start < next.length && same(prev[drop + start], next[start])) start++;
  let tail = 0;
  const limit = Math.min(rest, next.length) - start;
  while (tail < limit && same(prev[prev.length - 1 - tail], next[next.length - 1 - tail])) tail++;
  return { drop, start, remove: rest - start - tail, insert: next.length - start - tail };
}

// Reused rows are the same objects, so unchanged rows compare by reference.
export const diffLines = (prev: readonly Line[], next: readonly Line[]) =>
  align(prev, next, (a, b) => a.raw === b.raw && a.style.key === b.style.key);
