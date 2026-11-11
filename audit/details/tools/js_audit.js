// Read-only JS audit helper. Usage: node js_audit.js <app.js> <index.html> <out.json>
'use strict';
const fs = require('fs');
const [,, jsPath, htmlPath, outPath] = process.argv;
const src = fs.readFileSync(jsPath, 'utf8');
const html = fs.readFileSync(htmlPath, 'utf8');
const N = src.length;

// ---------------- scanner: mask comments/strings/regex, keep newlines & ${} code ----------------
const masked = new Array(N);
const strings = []; // {start,end,text,kind}
const templates = []; // {start,end,text,exprs:[{start,end,text}]}
let i = 0;
const KW_BEFORE_REGEX = new Set(['return', 'typeof', 'case', 'in', 'of', 'delete', 'void', 'throw', 'new', 'else', 'do', 'instanceof', 'await', 'yield']);
function prevSignificant(pos) {
  let k = pos - 1;
  while (k >= 0 && (masked[k] === ' ' || masked[k] === '\n' || masked[k] === '\t' || masked[k] === '\r')) k--;
  return k;
}
function prevWord(pos) {
  let k = prevSignificant(pos);
  let w = '';
  while (k >= 0 && /[\w$]/.test(masked[k])) { w = masked[k] + w; k--; }
  return w;
}
function isRegexStart(pos) {
  const k = prevSignificant(pos);
  if (k < 0) return true;
  const c = masked[k];
  if (/[\w$)\]]/.test(c)) {
    const w = prevWord(pos);
    return KW_BEFORE_REGEX.has(w);
  }
  return '(,=:[!&|?{};+-*%<>~^'.includes(c);
}
function scanTemplate(start) {
  // start at '`'; returns end index (after closing '`'); records nested ${} as code
  let k = start + 1;
  masked[start] = '`';
  const exprs = [];
  while (k < N) {
    const c = src[k];
    if (c === '\\') { masked[k] = ' '; masked[k + 1] = src[k + 1] === '\n' ? '\n' : ' '; k += 2; continue; }
    if (c === '`') { masked[k] = '`'; k++; break; }
    if (c === '$' && src[k + 1] === '{') {
      masked[k] = '$'; masked[k + 1] = '{';
      const exprStart = k + 2;
      const end = scanCode(k + 2, true);
      exprs.push({ start: exprStart, end: end - 1, text: src.slice(exprStart, end - 1) });
      k = end;
      continue;
    }
    masked[k] = c === '\n' ? '\n' : 'T';
    k++;
  }
  templates.push({ start, end: k, text: src.slice(start, k), exprs });
  return k;
}
function scanCode(pos, untilCloseBrace) {
  // scan code; if untilCloseBrace, stop after matching '}' at depth 0 and return index after it
  let depth = 0;
  let k = pos;
  while (k < N) {
    const c = src[k];
    const d = src[k + 1];
    if (c === '/' && d === '/') {
      while (k < N && src[k] !== '\n') { masked[k] = ' '; k++; }
      continue;
    }
    if (c === '/' && d === '*') {
      const j = src.indexOf('*/', k + 2);
      const end = j < 0 ? N : j + 2;
      for (let q = k; q < end; q++) masked[q] = src[q] === '\n' ? '\n' : ' ';
      k = end;
      continue;
    }
    if (c === '"' || c === "'") {
      const q = c;
      const s = k;
      masked[k] = q; k++;
      while (k < N && src[k] !== q) {
        if (src[k] === '\\') { masked[k] = ' '; k++; }
        if (k < N) { masked[k] = src[k] === '\n' ? '\n' : 'S'; k++; }
      }
      masked[k] = q; k++;
      strings.push({ start: s, end: k, text: src.slice(s + 1, k - 1), kind: q });
      continue;
    }
    if (c === '`') { k = scanTemplate(k); continue; }
    if (c === '/' && isRegexStart(k)) {
      // regex literal
      let j = k + 1;
      let inClass = false;
      masked[k] = 'R';
      while (j < N) {
        const cj = src[j];
        if (cj === '\\') { masked[j] = 'R'; masked[j + 1] = 'R'; j += 2; continue; }
        if (cj === '[') inClass = true;
        else if (cj === ']') inClass = false;
        else if (cj === '/' && !inClass) { masked[j] = 'R'; j++; break; }
        else if (cj === '\n') { break; }
        masked[j] = 'R'; j++;
      }
      while (j < N && /[a-z]/.test(src[j])) { masked[j] = 'R'; j++; }
      k = j;
      continue;
    }
    masked[k] = c;
    if (c === '{') depth++;
    else if (c === '}') {
      depth--;
      if (untilCloseBrace && depth < 0) { return k + 1; }
    }
    k++;
  }
  return k;
}
scanCode(0, false);
const M = masked.join('');
if (M.length !== N) throw new Error('mask length mismatch');

// line helpers
const lineStarts = [0];
for (let p = 0; p < N; p++) if (src[p] === '\n') lineStarts.push(p + 1);
function lineOf(pos) {
  let lo = 0, hi = lineStarts.length - 1;
  while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (lineStarts[mid] <= pos) lo = mid; else hi = mid - 1; }
  return lo + 1;
}
const totalLines = lineStarts.length;

// ---------------- top-level declarations ----------------
function matchBrace(openPos) {
  let depth = 0;
  for (let k = openPos; k < N; k++) {
    const c = M[k];
    if (c === '{') depth++;
    else if (c === '}') { depth--; if (depth === 0) return k; }
  }
  return N - 1;
}
const funcs = [];
const globals = [];
const reFn = /^(async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\(/gm;
let m;
while ((m = reFn.exec(M))) {
  const paren = M.indexOf('(', m.index + m[0].length - 1);
  // find matching ')' then '{'
  let depth = 0, k = paren;
  for (; k < N; k++) { if (M[k] === '(') depth++; else if (M[k] === ')') { depth--; if (depth === 0) break; } }
  const open = M.indexOf('{', k);
  const close = matchBrace(open);
  funcs.push({ name: m[2], start: m.index, end: close, startLine: lineOf(m.index), endLine: lineOf(close), isAsync: !!m[1], kind: 'function' });
}
const reVar = /^(const|let|var)\s+([A-Za-z_$][\w$]*|\{[^}]*\}|\[[^\]]*\])\s*(=|;)/gm;
while ((m = reVar.exec(M))) {
  const name = m[2];
  // initializer snippet from source
  const lineEnd = src.indexOf('\n', m.index);
  const init = src.slice(m.index + m[0].length, Math.min(lineEnd, m.index + m[0].length + 90)).trim();
  // arrow / function expression?
  const rest = M.slice(m.index + m[0].length, m.index + m[0].length + 200);
  const arrow = rest.match(/^\s*(async\s*)?(\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>\s*(\{)?/);
  const fexpr = rest.match(/^\s*(async\s+)?function\b/);
  if (arrow || fexpr) {
    let close;
    if (arrow && arrow[3]) {
      const open = m.index + m[0].length + arrow[0].length - 1;
      close = matchBrace(open);
    } else if (fexpr) {
      const open = M.indexOf('{', m.index + m[0].length + fexpr[0].length);
      close = matchBrace(open);
    } else {
      // expression-bodied arrow: ends at ';' at depth 0 on same statement
      let k = m.index + m[0].length + arrow[0].length, depth = 0;
      for (; k < N; k++) { const c = M[k]; if ('([{'.includes(c)) depth++; else if (')]}'.includes(c)) depth--; else if (c === ';' && depth === 0) break; }
      close = k;
    }
    funcs.push({ name, start: m.index, end: close, startLine: lineOf(m.index), endLine: lineOf(close), isAsync: !!((arrow && arrow[1]) || (fexpr && fexpr[1])), kind: m[1] + ' ' + (arrow ? 'arrow' : 'function-expr') });
  } else {
    let cls = 'const-primitive';
    if (m[1] !== 'const') cls = 'mutable-binding';
    else if (/^(\[|\{|new (Map|Set|WeakMap|Object|Array)|Object\.create)/.test(init)) cls = 'const-mutable-container';
    globals.push({ name, kind: m[1], line: lineOf(m.index), init: init.slice(0, 70), cls });
  }
}
funcs.sort((a, b) => a.start - b.start);
const funcByName = new Map(funcs.map(f => [f.name, f]));
function enclosing(pos) {
  let best = null;
  for (const f of funcs) if (f.start <= pos && pos <= f.end) { if (!best || f.start > best.start) best = f; }
  return best ? best.name : '(top-level)';
}

// ---------------- reference counts / unused ----------------
const idRe = /[A-Za-z_$][\w$]*/g;
const refs = new Map();
while ((m = idRe.exec(M))) {
  const w = m[0];
  if (!funcByName.has(w)) continue;
  const f = funcByName.get(w);
  // skip own declaration & self references inside own body
  if (m.index >= f.start && m.index <= f.end) continue;
  // property access like obj.name should not count
  const before = M[m.index - 1];
  if (before === '.') continue;
  refs.set(w, (refs.get(w) || 0) + 1);
}
const stringTexts = strings.map(s => s.text).join('\n') + templates.map(t => t.text).join('\n');
const unused = [];
for (const f of funcs) {
  const r = refs.get(f.name) || 0;
  if (r === 0) {
    const inHtml = new RegExp('\\b' + f.name.replace(/\$/g, '\\$') + '\\b').test(html);
    const inStr = new RegExp('\\b' + f.name.replace(/\$/g, '\\$') + '\\b').test(stringTexts);
    unused.push({ name: f.name, line: f.startLine, size: f.endLine - f.startLine + 1, inHtml, inStrings: inStr, confidence: (!inHtml && !inStr) ? 'high' : 'low' });
  }
}
const globalRefs = new Map();
for (const g of globals) {
  if (/[{[]/.test(g.name)) continue;
  const re = new RegExp('(?<![\\w$.])' + g.name.replace(/\$/g, '\\$') + '(?![\\w$])', 'g');
  let c = 0; let mm;
  while ((mm = re.exec(M))) { if (lineOf(mm.index) !== g.line) c++; }
  globalRefs.set(g.name, c);
}
const unusedGlobals = globals.filter(g => globalRefs.get(g.name) === 0);

// ---------------- per-function metrics ----------------
const fnMetrics = funcs.map(f => {
  const body = M.slice(f.start, f.end + 1);
  return {
    name: f.name, startLine: f.startLine, endLine: f.endLine, size: f.endLine - f.startLine + 1, isAsync: f.isAsync,
    api: (body.match(/\bapi(Form)?\(/g) || []).length,
    innerHTML: (body.match(/\.innerHTML\s*=|insertAdjacentHTML\(/g) || []).length,
    listeners: (body.match(/addEventListener\(|\.on(click|change|input|submit|keydown|keyup)\s*=/g) || []).length,
    notify: (body.match(/\bnotify\(/g) || []).length,
    tryBlocks: (body.match(/\btry\s*\{/g) || []).length,
    awaits: (body.match(/\bawait\b/g) || []).length,
  };
});
const bigFns = fnMetrics.filter(f => f.size > 120).sort((a, b) => b.size - a.size);
const mixed = fnMetrics.filter(f => f.api > 0 && f.innerHTML > 0 && f.listeners > 0);
const fetchRender = fnMetrics.filter(f => f.api > 0 && f.innerHTML > 0);

// ---------------- api() call handling ----------------
// try ranges
const tryRanges = [];
const reTry = /\btry\s*\{/g;
while ((m = reTry.exec(M))) { const open = m.index + m[0].length - 1; tryRanges.push([open, matchBrace(open)]); }
function inTry(pos) { return tryRanges.some(([a, b]) => pos > a && pos < b); }
function matchParen(openPos) { let d = 0; for (let k = openPos; k < N; k++) { if (M[k] === '(') d++; else if (M[k] === ')') { d--; if (d === 0) return k; } } return N - 1; }
const apiSites = [];
const reApi = /(?<![\w$.])api(Form)?\(/g;
while ((m = reApi.exec(M))) {
  const pos = m.index;
  // skip the definition itself
  const encl = enclosing(pos);
  if (encl === 'api' || encl === 'apiForm') continue;
  const before = M.slice(Math.max(0, pos - 60), pos);
  const awaited = /\bawait\s*$/.test(before) || /\bawait\s*\(\s*$/.test(before);
  const returned = /\breturn\s*$/.test(before) || /=>\s*$/.test(before);
  const close = matchParen(pos + m[0].length - 1);
  const after = M.slice(close + 1, close + 400);
  // chain detection
  let chain = after.match(/^(\s*\.(then|catch|finally)\s*\()/g) ? after : '';
  let hasCatch = false, hasThen = false;
  {
    let k = close + 1;
    while (k < N) {
      const seg = M.slice(k, k + 30);
      const mm = seg.match(/^\s*\.(then|catch|finally)\s*\(/);
      if (!mm) break;
      if (mm[1] === 'catch') hasCatch = true;
      if (mm[1] === 'then') hasThen = true;
      const op = k + mm[0].length - 1;
      k = matchParen(op) + 1;
    }
  }
  let cls;
  if (awaited && inTry(pos)) cls = 'await-in-try';
  else if (awaited) cls = 'await-no-try(propagates)';
  else if (hasCatch) cls = 'then/catch';
  else if (returned) cls = 'returned/arrow(propagates)';
  else if (hasThen) cls = 'then-without-catch';
  else cls = 'fire-and-forget';
  apiSites.push({ line: lineOf(pos), fn: encl, cls, snippet: src.slice(pos, Math.min(close + 1, pos + 90)).replace(/\s+/g, ' ') });
}
const apiClsCount = {};
for (const s of apiSites) apiClsCount[s.cls] = (apiClsCount[s.cls] || 0) + 1;

// ---------------- async loader fire-and-forget ----------------
const asyncNames = new Set(funcs.filter(f => f.isAsync).map(f => f.name));
const fireForget = [];
const reStmt = /^[ \t]*([A-Za-z_$][\w$]*)\((?:[^()\n]|\([^()\n]*\))*\)\s*;?[ \t]*$/gm;
while ((m = reStmt.exec(M))) {
  const name = m[1];
  if (!asyncNames.has(name)) continue;
  const line = lineOf(m.index);
  const text = src.slice(m.index, m.index + m[0].length).trim();
  if (/^(await|return|void)\b/.test(text)) continue;
  fireForget.push({ line, callee: name, fn: enclosing(m.index), text: text.slice(0, 80) });
}
const ffByCallee = {};
for (const f of fireForget) ffByCallee[f.callee] = (ffByCallee[f.callee] || 0) + 1;
// does the async callee itself wrap everything in try? (approx: has tryBlocks and first statement is try)
const calleeHasTry = {};
for (const name of Object.keys(ffByCallee)) {
  const f = funcByName.get(name);
  const body = M.slice(f.start, f.end + 1);
  const open = body.indexOf('{');
  const firstStmt = body.slice(open + 1).trim().slice(0, 12);
  calleeHasTry[name] = { startsWithTry: /^try\b/.test(firstStmt), tryBlocks: (body.match(/\btry\s*\{/g) || []).length, awaits: (body.match(/\bawait\b/g) || []).length };
}

// ---------------- empty catch ----------------
const emptyCatch = [];
const reCatch = /\bcatch\s*(\([^)]*\))?\s*\{([^{}]*)\}/g;
while ((m = reCatch.exec(M))) {
  const inner = m[2].trim();
  if (inner === '') emptyCatch.push({ line: lineOf(m.index), fn: enclosing(m.index), original: src.slice(m.index, m.index + m[0].length).replace(/\s+/g, ' ').slice(0, 80) });
}
// catch blocks with only return/comment
const catchTotal = (M.match(/\bcatch\s*(\([^)]*\))?\s*\{/g) || []).length;

// ---------------- console ----------------
const consoleSites = [];
const reCon = /console\.(log|warn|error|info|debug)\(/g;
while ((m = reCon.exec(M))) {
  const close = matchParen(m.index + m[0].length - 1);
  consoleSites.push({ line: lineOf(m.index), level: m[1], fn: enclosing(m.index), text: src.slice(m.index, Math.min(close + 1, m.index + 110)).replace(/\s+/g, ' ') });
}
const consoleByLevel = {};
for (const c of consoleSites) consoleByLevel[c.level] = (consoleByLevel[c.level] || 0) + 1;

// ---------------- DOM binding patterns ----------------
const innerHTMLAssign = (M.match(/\.innerHTML\s*=/g) || []).length;
const innerHTMLPlus = (M.match(/\.innerHTML\s*\+=/g) || []).length;
const insertAdj = (M.match(/insertAdjacentHTML\(/g) || []).length;
const addEv = (M.match(/addEventListener\(/g) || []).length;
const onProp = (M.match(/\.on(click|change|input|submit|keydown|keyup|focus|blur|scroll|load|error|toggle)\s*=/g) || []).length;
let qsaForEachBind = 0; const qsaSites = [];
const reQsa = /querySelectorAll\([^)]*\)\s*\.forEach\(/g;
while ((m = reQsa.exec(M))) {
  const open = M.indexOf('(', m.index + m[0].length - 1);
  const close = matchParen(open);
  const body = M.slice(open, close);
  if (/addEventListener\(|\.on\w+\s*=/.test(body)) { qsaForEachBind++; qsaSites.push({ line: lineOf(m.index), fn: enclosing(m.index) }); }
}
let delegated = 0; const delegatedSites = [];
const reDeleg = /addEventListener\(\s*'(click|change|input|submit|keydown)'\s*,/g;
while ((m = reDeleg.exec(M))) {
  const open = M.lastIndexOf('(', m.index + m[0].length);
  const close = matchParen(m.index + 'addEventListener'.length);
  const body = M.slice(m.index, close);
  if (/\.closest\(/.test(body) || /target\.(dataset|matches|classList)/.test(body)) { delegated++; delegatedSites.push({ line: lineOf(m.index), fn: enclosing(m.index) }); }
}
// per-element binding inside loops: addEventListener inside forEach/for bodies (approx: count addEventListener whose enclosing 400 chars before contains forEach( or 'for (')
let addEvInLoops = 0;
const reAdd = /addEventListener\(/g;
while ((m = reAdd.exec(M))) {
  const before = M.slice(Math.max(0, m.index - 500), m.index);
  if (/\.forEach\(|\bfor\s*\(/.test(before)) addEvInLoops++;
}

// ---------------- strings: status literals / view names / roles ----------------
const cmpCounts = {};
const reCmp = /[!=]==\s*(['"])/g;
while ((m = reCmp.exec(M))) {
  const s = strings.find(x => x.start === m.index + m[0].length - 1);
  if (s) cmpCounts[s.text] = (cmpCounts[s.text] || 0) + 1;
}
const reCmp2 = /(['"])\s*[!=]==/g;
while ((m = reCmp2.exec(M))) {
  const s = strings.find(x => x.end - 1 === m.index);
  if (s) cmpCounts[s.text] = (cmpCounts[s.text] || 0) + 1;
}
const cmpTop = Object.entries(cmpCounts).sort((a, b) => b[1] - a[1]).slice(0, 60);
const watch = ['done', 'archived', 'open', 'in_progress', 'new', 'waiting', 'closed', 'UNPROCESSED', 'PROCESSED', 'NEW', 'admin', 'manager', 'operator', 'user', 'running', 'finished', 'failed', 'stopped', 'interrupted', 'awaiting_answer', 'pending', 'queued', 'success', 'error', 'ok', 'active', 'all', 'chats', 'tasks', 'reviews', 'questions', 'knowledge', 'users', 'techSettings', 'profile', 'analytics', 'supplyPlanning', 'ozonFbo', 'ozon', 'yandex', 'wildberries', 'dark', 'light'];
const litCounts = {};
for (const s of strings) if (watch.includes(s.text)) litCounts[s.text] = (litCounts[s.text] || 0) + 1;

// ---------------- timers / intervals ----------------
const timerSites = [];
const reTimer = /set(Interval|Timeout)\(/g;
while ((m = reTimer.exec(M))) {
  const close = matchParen(m.index + m[0].length - 1);
  const args = src.slice(m.index + m[0].length, close);
  // last top-level argument
  let depth = 0, lastComma = -1;
  for (let k = 0; k < args.length; k++) { const c = M[m.index + m[0].length + k]; if ('([{'.includes(c)) depth++; else if (')]}'.includes(c)) depth--; else if (c === ',' && depth === 0) lastComma = k; }
  const delay = lastComma >= 0 ? args.slice(lastComma + 1).trim() : '(none)';
  timerSites.push({ line: lineOf(m.index), kind: m[1], delay: delay.slice(0, 50), fn: enclosing(m.index) });
}
const msConsts = globals.filter(g => /(_MS|_INTERVAL|_DELAY|_TIMEOUT|_MS\b|Ms$|_SECONDS)/.test(g.name) || /^\d+\s*\*\s*\d+/.test(g.init) || /^\d{3,}\s*;?$/.test(g.init));
const numericDelays = timerSites.filter(t => /^\d+$/.test(t.delay));

// ---------------- style consistency ----------------
const asyncFnCount = funcs.filter(f => f.isAsync).length;
const thenCount = (M.match(/\.then\(/g) || []).length;
const catchChainCount = (M.match(/\.catch\(/g) || []).length;
const awaitCount = (M.match(/\bawait\b/g) || []).length;
const dollarDef = (src.match(/^const \$ = .*$/m) || [''])[0];
const dollarCalls = (M.match(/(?<![\w$])\$\(/g) || []).length;
const gebi = (M.match(/document\.getElementById\(/g) || []).length;
const qs = (M.match(/\.querySelector\(/g) || []).length;
const qsa = (M.match(/\.querySelectorAll\(/g) || []).length;
const createEl = (M.match(/document\.createElement\(/g) || []).length;
const showViewCalls = (M.match(/showView\(\s*'/g) || []).length;
const activeViewCmp = (M.match(/activeView\s*[!=]==\s*'/g) || []).length;
const kbTimer = []; const reKb = /window\.__\w+/g; while ((m = reKb.exec(M))) kbTimer.push({ line: lineOf(m.index), text: m[0], fn: enclosing(m.index) });
const windowProps = {}; for (const k of kbTimer) windowProps[k.text] = (windowProps[k.text] || 0) + 1;
// debounce pattern: clearTimeout(X); ... X = setTimeout(
const debounce = []; const reDeb = /clearTimeout\(\s*([\w$.]+)\s*\)\s*;?\s*\n?\s*\1\s*=\s*setTimeout\(/g; while ((m = reDeb.exec(M))) debounce.push({ line: lineOf(m.index), timer: m[1], fn: enclosing(m.index) });
// notify('Ошибка загрузки...') sites
const notifyErr = []; const reNe = /notify\(\s*(['"`])/g; while ((m = reNe.exec(M))) { const s = strings.find(x => x.start === m.index + m[0].length - 1); const t = s ? s.text : src.slice(m.index + m[0].length, m.index + m[0].length + 50); if (/^Ошибка загрузки/.test(t)) notifyErr.push({ line: lineOf(m.index), fn: enclosing(m.index), text: t.slice(0, 60) }); }
const notifyTotal = (M.match(/\bnotify\(/g) || []).length;
const notifyMsgs = {}; { const re = /notify\(\s*(['"`])/g; let mm; while ((mm = re.exec(M))) { const s = strings.find(x => x.start === mm.index + mm[0].length - 1); if (s) notifyMsgs[s.text] = (notifyMsgs[s.text] || 0) + 1; } }
const notifyDupes = Object.entries(notifyMsgs).filter(([, c]) => c >= 3).sort((a, b) => b[1] - a[1]);
const comments = []; { const re = /временно|TODO|FIXME|HACK|legacy|deprecated|скрыт/gi; let mm; while ((mm = re.exec(src))) { if (M[mm.index] === ' ') comments.push({ line: lineOf(mm.index), text: src.slice(lineStarts[lineOf(mm.index) - 1], lineStarts[lineOf(mm.index) - 1] + 120).trim() }); } }

// ---------------- templates: longest + security ----------------
const tplInfo = templates.map(t => {
  const sl = lineOf(t.start), el = lineOf(t.end);
  const before = M.slice(Math.max(0, t.start - 80), t.start);
  const html = /<[a-z][\s\S]*>/i.test(t.text);
  return { startLine: sl, endLine: el, lines: el - sl + 1, chars: t.text.length, fn: enclosing(t.start), html, sink: /innerHTML\s*=\s*$/.test(before) ? 'innerHTML' : (/insertAdjacentHTML\([^,]*,\s*$/.test(before) ? 'insertAdjacentHTML' : (/return\s*$/.test(before) ? 'return' : (/push\(\s*$/.test(before) ? 'push' : '-'))), t };
});
const longestTpl = tplInfo.filter(t => t.html).sort((a, b) => b.lines - a.lines).slice(0, 20).map(({ t, ...r }) => r);
const htmlTplCount = tplInfo.filter(t => t.html).length;
const htmlTplLines = tplInfo.filter(t => t.html).reduce((a, t) => a + t.lines, 0);
// security: ${} expressions inside html templates not wrapped by escapeHtml
const SAFE_RE = /escapeHtml\(|encodeURIComponent\(|Number\(|\.length\b|toFixed\(|Math\.|\bidx\b|\bindex\b|\bi\b|Class\(|Classes\(|Badge\(|Icon\(|Label\(|Html\(|Markup\(|Template\(|render\w*\(|format\w*\(|\?\s*'[^']*'\s*:\s*'[^']*'/;
const suspicious = [];
let exprTotal = 0, exprEscaped = 0, exprOther = 0;
for (const t of tplInfo) {
  if (!t.html) continue;
  for (const e of t.t.exprs) {
    exprTotal++;
    const txt = e.text.trim();
    if (/escapeHtml\(/.test(txt)) { exprEscaped++; continue; }
    if (/^`/.test(txt) || /^\(?\s*[\w$.]+\s*\?\s*`/.test(txt)) { exprOther++; continue; } // nested template
    if (SAFE_RE.test(txt) || /^[\w$]+$/.test(txt) && /^(cls|className|classes|idx|i|n|count|total|id|key|href|url|tone|state|status|level|variant|icon|checked|disabled|selected|hidden|style|width|percent|pct|attr|attrs)$/.test(txt)) { exprOther++; continue; }
    suspicious.push({ line: lineOf(e.start), fn: t.fn, sink: t.sink, expr: txt.replace(/\s+/g, ' ').slice(0, 90) });
  }
}
// rank suspicious by variable-name heuristics
const RISKY = /(title|name|text|comment|message|question|answer|sku|offer|product|article|label|body|note|email|login|username|display|author|customer|client|content|tag|description|reason|error|detail|warehouse|cluster|order|posting|value)/i;
const suspiciousRisky = suspicious.filter(s => RISKY.test(s.expr));
// target=_blank
const blank = []; { const re = /target="_blank"/g; let mm; while ((mm = re.exec(src))) { const seg = src.slice(Math.max(0, mm.index - 200), mm.index + 120); blank.push({ line: lineOf(mm.index), rel: /rel="[^"]*"/.exec(seg)?.[0] || 'NO rel', fn: enclosing(mm.index) }); } }

// ---------------- init() ----------------
const initF = funcByName.get('init');
let initInfo = null;
if (initF) {
  const body = M.slice(initF.start, initF.end + 1);
  const bodySrc = src.slice(initF.start, initF.end + 1);
  const ids = new Set(); { const re = /\$\(\s*'([^']+)'\s*\)|getElementById\(\s*'([^']+)'\s*\)|bind\(\s*'([^']+)'/g; let mm; while ((mm = re.exec(bodySrc))) ids.add(mm[1] || mm[2] || mm[3]); }
  const calls = {}; { const re = /(?<![\w$.])([A-Za-z_$][\w$]*)\(/g; let mm; while ((mm = re.exec(body))) { const nme = mm[1]; if (funcByName.has(nme) && nme !== 'init') calls[nme] = (calls[nme] || 0) + 1; } }
  initInfo = { startLine: initF.startLine, endLine: initF.endLine, size: initF.endLine - initF.startLine + 1, addEventListener: (body.match(/addEventListener\(/g) || []).length, bindCalls: (body.match(/(?<![\w$.])bind\(/g) || []).length, distinctIds: ids.size, distinctFnCalls: Object.keys(calls).length, topCalls: Object.entries(calls).sort((a, b) => b[1] - a[1]).slice(0, 25), setInterval: (body.match(/setInterval\(/g) || []).length, awaits: (body.match(/\bawait\b/g) || []).length, api: (body.match(/\bapi\(/g) || []).length };
}

// ---------------- named families ----------------
function fam(names) { return names.map(n => { const f = funcByName.get(n); return f ? { name: n, line: f.startLine, size: f.endLine - f.startLine + 1, refs: refs.get(n) || 0 } : { name: n, missing: true }; }); }
const badge = fam(['statusBadge', 'reviewStatusBadge', 'questionStatusBadge', 'ozonFboStatusBadge', 'taskUiStatusClass', 'taskUiStatusLabel', 'supplyPlanningStatusBadge', 'waitingResponseBadge', 'yandexOAuthStatusBadge']);
const dates = fam(['parseDate', 'formatDateTime', 'formatChatTime', 'formatMessageTime', 'ozonFboFormatTime', 'datetimeLocalValue', 'formatTaskDueDate', 'formatDateInput', 'formatShortDate', 'formatDate', 'formatTime', 'formatRelativeTime', 'formatDueDate']);
const modals = funcs.filter(f => /^(open|close|show|hide)\w*(Modal|Decision|Sheet|Panel|Editor|Popover|Menu)/.test(f.name)).map(f => { const b = M.slice(f.start, f.end + 1); return { name: f.name, line: f.startLine, size: f.endLine - f.startLine + 1, focus: /\.focus\(/.test(b), ariaHidden: /aria-hidden/.test(b), escape: /Escape/.test(b), keydown: /keydown/.test(b), bodyClass: /body\.classList|document\.body/.test(b), inert: /inert/.test(b) }; });
// name families by prefix (potential duplicates)
const prefixes = {}; for (const f of funcs) { const mm = f.name.match(/^(render|load|bind|open|close|format|normalize|build|apply|sync|update|set|get|is|has|hydrate|refresh|paint|toggle|show|hide|schedule|run|handle|parse|create|ensure|reset|clear|fill|collect|read|save|persist|compute|resolve|map|escape|notify|track|mark|start|stop)/); const p = mm ? mm[1] : '(other)'; prefixes[p] = (prefixes[p] || 0) + 1; }
// functions grouped by section suffix
const suffixes = {}; for (const f of funcs) { const mm = f.name.match(/(OzonFbo|Knowledge|Supply|SupplyPlanning|Analytics|Task|Review|Question|Chat|Message|User|Notification|Yandex|Theme|Route|Auth|Login|Profile|Mobile|Composer|Template|Push|Sound)/); const p = mm ? mm[1] : '(other)'; suffixes[p] = (suffixes[p] || 0) + 1; }

// escapeHtml usage
const escapeCalls = (M.match(/escapeHtml\(/g) || []).length;
const textContent = (M.match(/\.textContent\s*=/g) || []).length;

const out = {
  totals: { lines: totalLines, bytes: Buffer.byteLength(src), topLevelFunctions: funcs.length, asyncFunctions: asyncFnCount, topLevelGlobals: globals.length, mutableBindings: globals.filter(g => g.cls === 'mutable-binding').length, constContainers: globals.filter(g => g.cls === 'const-mutable-container').length, constPrimitives: globals.filter(g => g.cls === 'const-primitive').length, strings: strings.length, templates: templates.length, htmlTemplates: htmlTplCount, htmlTemplateLines: htmlTplLines, innerHTMLAssign, innerHTMLPlus, insertAdj, addEventListener: addEv, onPropHandlers: onProp, qsaForEachBind, delegated, addEvInLoops, apiSites: apiSites.length, apiClsCount, fireForget: fireForget.length, emptyCatch: emptyCatch.length, catchTotal, consoleByLevel, consoleTotal: consoleSites.length, thenCount, catchChainCount, awaitCount, dollarDef, dollarCalls, gebi, qs, qsa, createEl, showViewCalls, activeViewCmp, windowProps, debounce: debounce.length, notifyTotal, notifyErr: notifyErr.length, escapeCalls, textContent, exprTotal, exprEscaped, exprOther, suspicious: suspicious.length, suspiciousRisky: suspiciousRisky.length, unusedFns: unused.length, unusedHigh: unused.filter(u => u.confidence === 'high').length, unusedGlobals: unusedGlobals.length, bigFns: bigFns.length, mixedFns: mixed.length, fetchRenderFns: fetchRender.length, timers: timerSites.length, numericDelays: numericDelays.length },
  globals, unusedGlobals, bigFns, mixed, fetchRender: fetchRender.map(f => f.name), apiSites, fireForget, ffByCallee, calleeHasTry, emptyCatch, consoleSites, qsaSites, delegatedSites, cmpTop, litCounts, timerSites, msConsts, debounce, notifyErr, notifyDupes, comments, longestTpl, suspicious, suspiciousRisky, blank, initInfo, badge, dates, modals, prefixes, suffixes, unused,
  funcIndex: funcs.map(f => [f.name, f.startLine, f.endLine, f.isAsync ? 'async' : '']),
};
fs.writeFileSync(outPath, JSON.stringify(out, null, 1), 'utf8');
console.log(JSON.stringify(out.totals, null, 1));
