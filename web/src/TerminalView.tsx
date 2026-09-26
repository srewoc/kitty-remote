import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from 'react';
import type { Snapshot } from './protocol';
import { EMPTY_SCREEN, PLAIN, diffLines, lineRuns, prepareScreen, type Line, type Patch, type Sgr } from './screen';

// Row height relative to the font size; every row gets exactly this height.
const LINE_HEIGHT = 1.2;
// Within this many rows of the end still counts as "at the bottom" and keeps following output.
const FOLLOW_SLACK_ROWS = 2;

function paint(el: HTMLElement, s: Sgr) {
  const fg = s.inverse ? s.bg ?? 'var(--term-bg)' : s.fg;
  const bg = s.inverse ? s.fg ?? 'var(--term-fg)' : s.bg;
  if (fg) el.style.color = fg;
  if (bg) { el.style.backgroundColor = bg; el.classList.add('term-fill'); }
  if (s.bold) el.style.fontWeight = '700';
  if (s.italic) el.style.fontStyle = 'italic';
  if (s.underline || s.strike) el.style.textDecorationLine = [s.underline && 'underline', s.strike && 'line-through'].filter(Boolean).join(' ');
  if (s.dim) el.style.opacity = '0.6';
  if (s.hidden) el.style.visibility = 'hidden';
}

// Text is only ever inserted as text nodes; terminal output is never parsed as HTML.
function renderLine(line: Line) {
  const row = document.createElement('div');
  row.className = 'term-line';
  for (const run of lineRuns(line)) {
    const plain = run.style.key === PLAIN.key;
    if (plain && run.cells === null) { row.append(run.text); continue; }
    const span = document.createElement('span');
    span.textContent = run.text;
    if (run.cells !== null) { span.className = run.cjk ? 'term-cells term-cjk' : 'term-cells'; span.style.width = `${run.cells}ch`; }
    if (!plain) paint(span, run.style);
    row.append(span);
  }
  return row;
}

function removeRows(host: HTMLElement, start: number, count: number) {
  if (!count) return;
  const range = document.createRange();
  range.setStartBefore(host.children[start]);
  range.setEndAfter(host.children[start + count - 1]);
  range.deleteContents();
}

function applyPatch(host: HTMLElement, patch: Patch, next: readonly Line[]) {
  const fresh = document.createDocumentFragment();
  for (let i = patch.start; i < patch.start + patch.insert; i++) fresh.append(renderLine(next[i]));
  if (!patch.drop && !patch.start && patch.remove >= host.childElementCount) { host.replaceChildren(fresh); return; }
  removeRows(host, 0, patch.drop);
  removeRows(host, patch.start, patch.remove);
  host.insertBefore(fresh, host.children[patch.start] ?? null);
}

/** Whole Kitty buffer as native-scrolling rows; updates patch changed rows in place. */
interface TerminalViewProps { snapshot: Snapshot | null; fontSize: number }

export function TerminalView({ snapshot, fontSize }: TerminalViewProps) {
  const scroller = useRef<HTMLDivElement>(null);
  const rows = useRef<HTMLDivElement>(null);
  const screen = useRef(EMPTY_SCREEN);
  const alternate = useRef<boolean | null>(null);
  const following = useRef(true);
  const anchor = useRef(0);
  const rowPx = Math.round(fontSize * LINE_HEIGHT);
  const rowRef = useRef(rowPx);
  const fontRef = useRef(fontSize);
  const [atBottom, setAtBottom] = useState(true);
  const [unseen, setUnseen] = useState(false);

  // Content can also shrink under a reader without any scroll event, so updates re-check too.
  const syncFollow = (el: HTMLElement) => {
    anchor.current = el.scrollTop / rowRef.current;
    const bottom = el.scrollTop + el.clientHeight >= el.scrollHeight - rowRef.current * FOLLOW_SLACK_ROWS;
    following.current = bottom;
    setAtBottom(bottom);
    if (bottom) setUnseen(false);
    return bottom;
  };

  useEffect(() => {
    const el = scroller.current!;
    const onScroll = () => { syncFollow(el); };
    // The frame shrinks when the soft keyboard opens; keep the latest output in view.
    const observer = new ResizeObserver(() => { if (following.current) el.scrollTop = el.scrollHeight; });
    el.addEventListener('scroll', onScroll, { passive: true });
    observer.observe(el);
    return () => { el.removeEventListener('scroll', onScroll); observer.disconnect(); };
  }, []);

  useLayoutEffect(() => {
    const el = scroller.current!;
    const next = snapshot ? prepareScreen(snapshot.content, screen.current) : EMPTY_SCREEN;
    const patch = diffLines(screen.current.lines, next.lines);
    // Entering or leaving the alternate screen shows a different page; start at its latest rows.
    const switched = snapshot !== null && alternate.current !== null && alternate.current !== snapshot.alternate;
    if (snapshot) alternate.current = snapshot.alternate;
    if (!patch.drop && !patch.remove && !patch.insert) { screen.current = next; return; }
    const firstVisible = Math.floor(el.scrollTop / rowRef.current) - patch.drop;
    applyPatch(rows.current!, patch, next.lines);
    screen.current = next;
    if (switched || following.current) {
      el.scrollTop = el.scrollHeight;
      syncFollow(el);
      return;
    }
    // Keep the row being read in place when history above it scrolls off or changes length.
    // The alternate screen is a fixed page, so its rows stay where they are.
    let shift = 0;
    if (!snapshot?.alternate) {
      shift -= patch.drop;
      if (patch.start + patch.remove <= firstVisible) shift += patch.insert - patch.remove;
    }
    if (shift) el.scrollTop += shift * rowRef.current;
    if (!syncFollow(el)) setUnseen(true);
  }, [snapshot]);

  useLayoutEffect(() => {
    const el = scroller.current!;
    const previousFont = fontRef.current;
    if (previousFont === fontSize) return;
    fontRef.current = fontSize;
    rowRef.current = Math.round(fontSize * LINE_HEIGHT);
    el.scrollLeft = el.scrollLeft * fontSize / previousFont;
    el.scrollTop = following.current ? el.scrollHeight : anchor.current * rowRef.current;
  }, [fontSize]);

  const jumpToLatest = () => {
    const el = scroller.current!;
    following.current = true; setAtBottom(true); setUnseen(false);
    el.scrollTop = el.scrollHeight;
  };
  const style = { fontSize: `${fontSize}px`, '--row': `${rowPx}px` } as CSSProperties;
  return <>
    <div ref={scroller} className="terminal-scroll" style={style} tabIndex={0} role="region"
      aria-label="终端文字视图" data-testid="terminal"><div ref={rows} className="terminal-lines"/></div>
    {!atBottom && <button className={`jump-latest ${unseen ? 'has-new' : ''}`} onClick={jumpToLatest}>
      {unseen ? '有新输出 ↓' : '回到最新 ↓'}</button>}
  </>;
}
