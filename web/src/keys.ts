export const commonKeys = [
  ['Esc', 'esc'], ['Tab', 'tab'], ['Shift+Tab', 'shift+tab'], ['Enter ↵', 'enter'], ['Ctrl+C', 'ctrl+c'],
  ['←', 'left'], ['↑', 'up'], ['↓', 'down'], ['→', 'right'], ['⌫', 'backspace'],
] as const;
export const extraKeys = [
  { title: '导航与编辑 · 发送到终端', keys: [
    ['Home', 'home'], ['End', 'end'], ['PageUp', 'page_up'], ['PageDown', 'page_down'],
    ['Insert', 'insert'], ['Delete', 'delete'], ['Shift+Enter', 'shift+enter'], ['Ctrl+Enter', 'ctrl+enter'],
  ] },
  { title: '快捷键', keys: [
    ...'AEUKWRLDZ'.split('').map(k => [`Ctrl+${k}`, `ctrl+${k.toLowerCase()}`]),
    ['Alt+B', 'alt+b'], ['Alt+F', 'alt+f'], ['Alt+⌫', 'alt+backspace'],
  ] },
  { title: '功能键', keys: Array.from({ length: 12 }, (_, i) => [`F${i + 1}`, `f${i + 1}`]) },
];
