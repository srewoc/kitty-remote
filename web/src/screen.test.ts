import { describe, expect, it } from 'vitest';
import { PLAIN, applySgr, cellWidth, diffLines, lineRuns, prepareScreen, type Line, type Patch } from './screen';

const prepareLines = (content: string) => prepareScreen(content).lines;

// Reference application of a patch, mirroring what the view does to its DOM rows.
function apply(prev: readonly Line[], patch: Patch, next: readonly Line[]) {
  const rest = prev.slice(patch.drop);
  return [...rest.slice(0, patch.start), ...next.slice(patch.start, patch.start + patch.insert),
    ...rest.slice(patch.start + patch.remove)];
}
const raws = (lines: readonly Line[]) => lines.map(l => l.raw);
const numbered = (from: number, to: number) => Array.from({ length: to - from }, (_, i) => `line ${from + i}`);

describe('SGR styles', () => {
  it('resets, toggles attributes and compares by key', () => {
    const bold = applySgr(PLAIN, '1;3');
    expect(bold.bold && bold.italic).toBe(true);
    expect(applySgr(bold, '22').bold).toBe(false);
    expect(applySgr(bold, '').key).toBe(PLAIN.key);
    expect(applySgr(PLAIN, '1;3').key).toBe(bold.key);
    expect(applySgr(PLAIN, '4:0').underline).toBe(false);
    expect(applySgr(PLAIN, '4:3').underline).toBe(true);
  });
  it('parses palette, 256-colour and truecolour forms', () => {
    expect(applySgr(PLAIN, '31').fg).toBe('#cc0000');
    expect(applySgr(PLAIN, '97').fg).toBe('#eeeeec');
    expect(applySgr(PLAIN, '38;5;196').fg).toBe('rgb(255,0,0)');
    expect(applySgr(PLAIN, '38;5;244').fg).toBe('rgb(128,128,128)');
    expect(applySgr(PLAIN, '38;2;1;2;3;1').fg).toBe('rgb(1,2,3)');
    expect(applySgr(PLAIN, '38;2;1;2;3;1').bold).toBe(true);
    expect(applySgr(PLAIN, '48:2:10:20:30').bg).toBe('rgb(10,20,30)');
    expect(applySgr(PLAIN, '48:2::10:20:30').bg).toBe('rgb(10,20,30)');
    expect(applySgr(applySgr(PLAIN, '31;42'), '39;49').key).toBe(PLAIN.key);
    expect(applySgr(PLAIN, '58:2:1:2:3').key).toBe(PLAIN.key);
  });
});

describe('prepared lines', () => {
  it('carries styles across line breaks like Kitty output', () => {
    const lines = prepareLines('\x1b[31mred\nstill red\n\x1b[mplain');
    expect(lines.map(l => l.style.fg)).toEqual([null, '#cc0000', '#cc0000']);
    expect(lineRuns(lines[1])[0].style.fg).toBe('#cc0000');
    expect(lineRuns(lines[2]).map(r => [r.text, r.style.fg])).toEqual([['plain', null]]);
  });
  it('reuses unchanged rows from the previous content', () => {
    const before = prepareScreen('\x1b[31ma\nb\nspinner 1');
    const after = prepareScreen('\x1b[31ma\nb\nspinner 2\nc', before);
    expect(after.lines[0]).toBe(before.lines[0]);
    expect(after.lines[1]).toBe(before.lines[1]);
    expect(raws(after.lines)).toEqual(['\x1b[31ma', 'b', 'spinner 2', 'c']);
  });
  it('matches a fresh parse after incremental updates', () => {
    const cases: Array<[string, string]> = [
      ['\x1b[31ma\nb\nc', '\x1b[32ma\nb\nc'],
      ['a\n\x1b[31mb\nc\nd', 'a\nb\nc\nd'],
      [numbered(0, 30).join('\n'), numbered(4, 34).join('\n')],
      ['\x1b[1mx\n' + numbered(0, 20).join('\n'), numbered(0, 20).join('\n') + '\ny'],
      ['a\x1b[1;1H\nb', 'a\x1b]0;t\x07\nb\x07'],
    ];
    for (const [a, b] of cases) {
      const incremental = prepareScreen(b, prepareScreen(a)).lines;
      const fresh = prepareScreen(b).lines;
      expect(incremental.map(l => [l.raw, l.style.key, l.end.key])).toEqual(fresh.map(l => [l.raw, l.style.key, l.end.key]));
    }
  });
  it('drops trailing blank rows, unsafe sequences and carriage returns', () => {
    const lines = prepareLines('a\x1b]52;c;SGVsbG8=\x07b\r\n\x1b[31m  \n\x1b[1;1H\n');
    expect(raws(lines)).toEqual(['ab']);
    expect(raws(prepareLines('\n\n'))).toEqual(['']);
  });
});

describe('cell runs', () => {
  it('keeps ASCII as text and gives other runs their exact terminal width', () => {
    const runs = lineRuns({ raw: 'ab中文─🙂c', style: PLAIN });
    expect(runs.map(r => [r.text, r.cells, r.cjk])).toEqual(
      [['ab', null, false], ['中文', 4, true], ['─🙂', 3, false], ['c', null, false]]);
  });
  it('keeps zero-width marks with the character they modify', () => {
    const runs = lineRuns({ raw: '中\u0301文❤\ufe0f', style: PLAIN });
    expect(runs.map(r => [r.text, r.cells, r.cjk])).toEqual([['中\u0301文', 4, true], ['❤\ufe0f', 2, false]]);
    const nfd = lineRuns({ raw: 'cafe\u0301 #\ufe0f\u20e3', style: PLAIN });
    expect(nfd.map(r => [r.text, r.cells])).toEqual([['caf', null], ['e\u0301', 1], [' ', null], ['#\ufe0f\u20e3', 2]]);
  });
  it('sizes emoji sequences the way Kitty does', () => {
    const width = (text: string) => lineRuns({ raw: text, style: PLAIN }).reduce((n, r) => n + (r.cells ?? r.text.length), 0);
    expect(['⚠\ufe0f', '⚠', '→\ufe0f', '↔\ufe0f', '🖥', '🖥\ufe0f', '😀\ufe0e', '☰',
      '👍🏽', '🇨🇳', '🇨🇳🇯', '👨\u200d👩', '\u00ad'].map(width)).toEqual([2, 1, 1, 2, 1, 2, 1, 2, 2, 2, 4, 2, 0]);
  });
  it('splits runs at style changes', () => {
    const runs = lineRuns({ raw: 'a\x1b[1mb\x1b[mc\x1b[1;1H', style: PLAIN });
    expect(runs.map(r => [r.text, r.style.bold])).toEqual([['a', false], ['b', true], ['c', false]]);
  });
  it('measures wide, narrow and zero-width characters', () => {
    expect(['中', '，', '🙂', 'é', '─', '́', '‍'].map(c => cellWidth(c.codePointAt(0)!)))
      .toEqual([2, 2, 2, 1, 1, 0, 0]);
  });
});

describe('line diff', () => {
  const check = (prev: string, next: string) => {
    const a = prepareLines(prev), b = prepareLines(next);
    const patch = diffLines(a, b);
    expect(raws(apply(a, patch, b))).toEqual(raws(b));
    return patch;
  };
  it('does nothing when content is unchanged', () => {
    expect(check('a\nb', 'a\nb')).toEqual({ drop: 0, start: 2, remove: 0, insert: 0 });
  });
  it('only touches appended or rewritten bottom rows', () => {
    expect(check('a\nb\nc', 'a\nb\nc\nd\ne')).toEqual({ drop: 0, start: 3, remove: 0, insert: 2 });
    expect(check('a\nb\nspinner 1', 'a\nb\nspinner 2')).toEqual({ drop: 0, start: 2, remove: 1, insert: 1 });
  });
  it('detects rows scrolled off the top of a full scrollback', () => {
    const prev = numbered(0, 100).join('\n'), next = numbered(3, 103).join('\n');
    expect(check(prev, next)).toEqual({ drop: 3, start: 97, remove: 0, insert: 3 });
  });
  it('detects scrolled-off rows even when the new top row repeats the old one', () => {
    const logs = numbered(0, 200);
    expect(check(['', '', ...logs].join('\n'), ['', ...logs, 'new'].join('\n')))
      .toEqual({ drop: 1, start: 201, remove: 0, insert: 1 });
  });
  it('needs more than one matching row to call it a scroll', () => {
    expect(check([...numbered(0, 50), 'prompt $'].join('\n'), 'prompt $\nls').drop).toBe(0);
  });
  it('does not mistake repeated blank rows for a scroll', () => {
    expect(check('x\n\n\n\ny', 'z\n\n\n\ny')).toEqual({ drop: 0, start: 0, remove: 1, insert: 1 });
  });
  it('re-renders rows whose inherited style changed', () => {
    expect(check('\x1b[31ma\nb', '\x1b[32ma\nb')).toEqual({ drop: 0, start: 0, remove: 2, insert: 2 });
  });
  it('handles full replacement and shrinking content', () => {
    check('a\nb\nc', 'x');
    check(numbered(0, 50).join('\n'), 'only');
    check('', numbered(0, 5).join('\n'));
  });
});
