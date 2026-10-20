// No client component may assign a field named after an A-Frame lifecycle method.
//
// A component instance IS the object A-Frame calls `play()`, `pause()`, `tick()` … on, so `this.play =
// someNode` replaces the method. voice-link.js did exactly that with its playback worklet: `init` set
// `this.play = null`, the scene's start-up then called null, threw, and stopped — the voice link's tick
// (push-to-talk) never ran, and neither did anything the scene would have started after it (2026-10-20).
// Nothing failed loudly enough to notice on the headset, so this is checked statically, across every file.

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const CLIENT = path.join(__dirname, '..', '..', 'client');
const LIFECYCLE = ['init', 'update', 'play', 'pause', 'tick', 'tock', 'remove', 'events', 'schema'];
const ASSIGN = new RegExp(`\\b(?:this|self)\\.(${LIFECYCLE.join('|')})\\s*=(?!=)`, 'g');

test('no client component shadows an A-Frame lifecycle method with a field', () => {
  const hits = [];
  for (const f of fs.readdirSync(CLIENT)) {
    if (!f.endsWith('.js') || f === 'three.module.min.js') continue;
    const lines = fs.readFileSync(path.join(CLIENT, f), 'utf8').split('\n');
    lines.forEach((line, i) => {
      const code = line.replace(/\/\/.*$/, '');               // a comment may name the rule
      for (const m of code.matchAll(ASSIGN)) hits.push(`${f}:${i + 1} assigns .${m[1]}`);
    });
  }
  assert.deepStrictEqual(hits, []);
});

test('the check catches the shape that broke the voice link', () => {
  assert.match('      this.ctx = null; this.play = null;', ASSIGN);
  assert.doesNotMatch('if (this.play == null) {}', ASSIGN);
});
