import { describe, expect, it } from 'vitest';
import { RelayClock, disconnected, sameTarget, sanitizeAnsi, utf8Size, type Receipt } from './protocol';
const target = { device_id:'device', agent_epoch:'epoch', kitty_instance_id:'instance', window_id:1 };
describe('screen and input boundaries', () => {
  it('removes executable terminal protocols while preserving Chinese and style', () => {
    expect(sanitizeAnsi('中文🙂\x1b[31mred\x1b[0m\x1b]52;c;secret\x07\x1bPpayload\x1b\\')).toBe('中文🙂\x1b[31mred\x1b[0m');
    expect(sanitizeAnsi('a\x9d52;c;secret\x9cb\x1b[6n')).toBe('ab');
  });
  it('does not confuse a reused window ID or restarted agent', () => {
    expect(sameTarget(target,{ ...target, agent_epoch:'new' })).toBe(false);
    expect(sameTarget(target,{ ...target, kitty_instance_id:'new' })).toBe(false);
    expect(sameTarget(target,{ ...target })).toBe(true);
  });
  it('counts UTF-8 bytes, not JavaScript characters', () => { expect(utf8Size('中文🙂')).toBe(10); });
  it('marks only pending input uncertain after disconnect', () => {
    const input: Receipt[] = ['received','submitted'].map((status,i) => ({request_id:String(i),target,status:status as Receipt['status'],detail:''}));
    expect(disconnected(input).map(r=>r.status)).toEqual(['uncertain','submitted']);
  });
});

it('uses relay time without depending on browser wall time', () => {
  const clock = new RelayClock();
  expect(clock.deadline(1000)).toBeNull();
  clock.sync(100000, 1000);
  expect(clock.deadline(1500)).toBe(100010.5);
  clock.sync(Number.NaN, 2000);
  expect(clock.deadline(2100)).toBeNull();
});
