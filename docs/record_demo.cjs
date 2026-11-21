'use strict';
/*
 * Records the walkthrough video of the screening desk.
 *
 *   python3 ui/server.py                       # in one terminal
 *   npm i playwright && node docs/record_demo.cjs --rehearse   # check every selector
 *   node docs/record_demo.cjs                  # writes docs/demo.webm
 *
 * Set CHROMIUM=/path/to/chrome-headless-shell to use a browser that is already installed.
 * Needs the Playwright package; it is only used to make the video, never by the system or the viewer.
 */
const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');

const BASE = process.env.DESK_URL || 'http://127.0.0.1:8765';
const OUT_DIR = path.join(__dirname);
const OUTPUT = path.join(OUT_DIR, 'demo.webm');
const REHEARSAL = process.argv.includes('--rehearse');
const SIZE = { width: 1440, height: 900 };

// Narration (optional): docs/narrate.py writes a clip per caption into docs/.voice. When the clips are there, the
// recording waits for each line to finish before the next one starts, and logs when each line began.
const VOICE_DIR = path.join(OUT_DIR, '.voice');
const VOICE = fs.existsSync(path.join(VOICE_DIR, 'voice.json')) ? JSON.parse(fs.readFileSync(path.join(VOICE_DIR, 'voice.json'), 'utf8')) : {};
const TIMELINE = [];
let t0 = 0, voiceUntil = 0;

async function speak(page, key) {
  const clip = VOICE[key];
  if (!clip) return 0;
  const wait = voiceUntil - Date.now();
  if (wait > 0) await page.waitForTimeout(wait + 250);
  TIMELINE.push({ key, file: clip.file, at: Date.now() - t0 + 150 });
  voiceUntil = Date.now() + 150 + clip.dur * 1000;
  return clip.dur * 1000;
}

async function injectOverlays(page) {
  await page.evaluate(() => {
    if (!document.getElementById('demo-cursor')) {
      const cursor = document.createElement('div');
      cursor.id = 'demo-cursor';
      cursor.innerHTML = '<svg width="26" height="26" viewBox="0 0 24 24" fill="none"><path d="M5 3L19 12L12 13L9 20L5 3Z" fill="white" stroke="black" stroke-width="1.5" stroke-linejoin="round"/></svg>';
      cursor.style.cssText = 'position:fixed;z-index:999999;pointer-events:none;width:26px;height:26px;left:720px;top:450px;transition:left .1s,top .1s,opacity .25s;opacity:0;filter:drop-shadow(1px 2px 2px rgba(0,0,0,.35))';
      document.body.appendChild(cursor);
      document.addEventListener('mousemove', (e) => { cursor.style.left = e.clientX + 'px'; cursor.style.top = e.clientY + 'px'; });
    }
    if (!document.getElementById('demo-subtitle')) {
      const bar = document.createElement('div');
      bar.id = 'demo-subtitle';
      bar.style.cssText = 'position:fixed;bottom:0;left:0;right:0;z-index:999998;text-align:center;padding:16px 28px;background:rgba(27,25,21,.92);color:#f4f0e6;font:500 21px/1.35 "Avenir Next","Segoe UI",sans-serif;letter-spacing:.2px;transition:opacity .3s;pointer-events:none;opacity:0';
      document.body.appendChild(bar);
    }
  });
}

async function say(page, text, hold = 0) {
  if (text) await speak(page, text);
  else if (voiceUntil > Date.now()) await page.waitForTimeout(voiceUntil - Date.now() + 250);
  await page.evaluate((t) => {
    const bar = document.getElementById('demo-subtitle');
    if (!bar) return;
    if (t) { bar.textContent = t; bar.style.opacity = '1'; } else { bar.style.opacity = '0'; }
  }, text);
  await page.waitForTimeout(hold || (text ? 700 : 300));
}

async function card(page, title, lines, hold, key) {
  const spoken = key ? await speak(page, 'card:' + key) : 0;
  hold = Math.max(hold, spoken ? spoken + 700 : 0);
  await page.evaluate(({ title, lines }) => {
    const el = document.createElement('div');
    el.id = 'demo-card';
    el.style.cssText = 'position:fixed;inset:0;z-index:999997;display:grid;place-items:center;background:#f4f0e6;opacity:0;transition:opacity .5s;pointer-events:none';
    el.innerHTML = '<div style="max-width:980px;padding:0 60px;text-align:left">' +
      '<div style="font:italic 500 62px/1.1 \'Iowan Old Style\',Palatino,Georgia,serif;letter-spacing:-.02em;color:#1b1915;border-bottom:3px solid #1b1915;padding-bottom:22px;margin-bottom:26px">' + title + '</div>' +
      lines.map((l) => '<div style="font:400 27px/1.5 \'Avenir Next\',\'Segoe UI\',sans-serif;color:#3b362f;margin:9px 0">' + l + '</div>').join('') + '</div>';
    document.body.appendChild(el);
    requestAnimationFrame(() => { el.style.opacity = '1'; });
  }, { title, lines });
  await page.waitForTimeout(hold);
}

async function dropCard(page) {
  await page.evaluate(() => { const cur = document.getElementById('demo-cursor'); if (cur) cur.style.visibility = 'visible'; });
  await page.evaluate(() => { const el = document.getElementById('demo-card'); if (el) { el.style.opacity = '0'; setTimeout(() => el.remove(), 500); } });
  await page.waitForTimeout(650);
}

async function ensureVisible(page, selector, label) {
  const ok = await page.locator(selector).first().isVisible().catch(() => false);
  console.log((ok ? 'REHEARSAL OK:   ' : 'REHEARSAL FAIL: ') + label + '   ' + selector);
  return ok;
}

async function pointer(page, on) {
  await page.evaluate((on) => { const c = document.getElementById('demo-cursor'); if (c) c.style.opacity = on ? '1' : '0'; }, on);
}

async function moveAndClick(page, selector, label, pause = 900) {
  await pointer(page, true);
  const el = page.locator(selector).first();
  if (!(await el.isVisible().catch(() => false))) { console.error('WARNING: not visible: ' + label + ' ' + selector); return false; }
  await el.scrollIntoViewIfNeeded();
  await page.waitForTimeout(250);
  const box = await el.boundingBox();
  if (box) { await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps: 14 }); await page.waitForTimeout(350); }
  await el.click();
  await page.waitForTimeout(Math.min(pause, 700));
  await pointer(page, false);
  if (pause > 700) await page.waitForTimeout(pause - 700);
  return true;
}

async function hover(page, selector) {
  const box = await page.locator(selector).first().boundingBox().catch(() => null);
  if (box) { await page.mouse.move(box.x + Math.min(box.width / 2, 260), box.y + Math.min(box.height / 2, 60), { steps: 16 }); }
  await page.waitForTimeout(500);
}

// Smoothly scroll the centre pane so that an element sits near its top.
async function scrollTo(page, selector, offset = 18, pause = 1300) {
  await page.evaluate(({ selector, offset }) => {
    const pane = document.getElementById('process');
    const el = document.querySelector(selector);
    if (!pane || !el) return;
    const top = pane.scrollTop + el.getBoundingClientRect().top - pane.getBoundingClientRect().top - offset;
    pane.scrollTo({ top, behavior: 'smooth' });
  }, { selector, offset });
  await page.waitForTimeout(pause);
}

async function openCase(page, inst, caseId) {
  await moveAndClick(page, `.tab[data-inst="${inst}"]`, 'institution tab ' + inst, 1200);
  await page.waitForSelector('.q');
  await moveAndClick(page, '#search', 'search box', 300);
  await page.locator('#search').pressSequentially(caseId, { delay: 45 });
  await page.waitForTimeout(600);
  await moveAndClick(page, `.q[data-id="${caseId}"]`, 'queue row ' + caseId, 400);
  await page.waitForSelector('.step');
}

const step = (n) => `.steps .step:nth-child(${n})`;

async function rehearse(page) {
  let ok = true;
  const need = async (sel, label) => { if (!(await ensureVisible(page, sel, label))) ok = false; };
  await page.goto(BASE + '/?r=' + Date.now() + '#bank/BK-CS125943'); await page.waitForSelector('.step'); await page.click('#skip');
  for (const n of [4, 5, 7]) await need(step(n), 'escalation step ' + n);
  await need(`${step(5)} button[data-ev="PSC-5155"]`, 'control register chip');
  await need('.next', 'next-step panel'); await need('.warn', 'tipping-off warning'); await need('#replay', 'replay button');
  await page.goto(BASE + '/?r=' + Date.now() + '#investment/IV-CS103503'); await page.waitForSelector('.step'); await page.click('#skip');
  await need(`${step(4)} button[data-ev="GFT-2351"]`, 'gift letter chip'); await need(step(7), 'close step 7');
  await page.goto(BASE + '/bot?step&demo');
  await page.waitForFunction(() => window.botIdle === true, null, { timeout: 30000 });
  await need('#thread .card.ok', 'bot: connector card');
  const missing = Object.keys(JSON.parse(fs.readFileSync(path.join(OUT_DIR, 'narration.json'), 'utf8'))).filter((k) => !VOICE[k]);
  if (missing.length) { ok = false; console.log('REHEARSAL FAIL: narration not voiced yet: ' + missing.join(' | ')); }
  console.log(ok ? 'REHEARSAL PASSED' : 'REHEARSAL FAILED');
  return ok;
}

const LOGO = fs.readFileSync(path.join(OUT_DIR, 'logo.svg'), 'utf8').replace(/<title>.*?<\/title>/s, '').replace(/\s*\n\s*/g, ' ').trim();

// A full-screen card: optional logo, a headline, and smaller lines. Spoken when a narration key is given.
async function titleCard(page, html, hold, key) {
  const spoken = key ? await speak(page, 'card:' + key) : 0;
  hold = Math.max(hold, spoken ? spoken + 600 : 0);
  await page.evaluate((html) => {
    const cur = document.getElementById('demo-cursor'); if (cur) cur.style.visibility = 'hidden';
    let el = document.getElementById('demo-card');
    const inner = '<div class="demo-card-in" style="max-width:1000px;padding:0 70px;text-align:center;opacity:0;transition:opacity .5s">' + html + '</div>';
    if (el) {
      el.style.opacity = '1';
      el.innerHTML = inner;
    } else {
      el = document.createElement('div');
      el.id = 'demo-card';
      el.style.cssText = 'position:fixed;inset:0;z-index:999997;display:grid;place-items:center;background:radial-gradient(1100px 700px at 50% 30%,#fffdf6,#f4f0e6 70%);opacity:0;transition:opacity .5s;pointer-events:none';
      el.innerHTML = inner;
      document.body.appendChild(el);
      requestAnimationFrame(() => { el.style.opacity = '1'; });
    }
    requestAnimationFrame(() => requestAnimationFrame(() => { el.querySelector('.demo-card-in').style.opacity = '1'; }));
  }, html);
  await page.waitForTimeout(hold);
}
const H = (t) => '<div style="font:500 54px/1.15 \'Iowan Old Style\',Charter,Georgia,serif;letter-spacing:-.02em;color:#1b1915">' + t + '</div>';
const SUB = (t) => '<div style="font:400 24px/1.5 \'Avenir Next\',\'Segoe UI\',sans-serif;color:#57514a;margin-top:22px">' + t + '</div>';
const MARK = (size) => '<div style="width:' + size + 'px;height:' + size + 'px;margin:0 auto 26px">' + LOGO.replace('<svg ', '<svg style="width:100%;height:100%" ') + '</div>';
const WORD = '<div style="font:italic 500 40px/1 \'Iowan Old Style\',Charter,Georgia,serif;color:#1b1915;letter-spacing:-.015em"><b style="font-style:normal;font-weight:600">Screening</b> desk</div>';

async function quiet(page, extra = 150) {
  const wait = voiceUntil - Date.now();
  if (wait > 0) await page.waitForTimeout(wait + extra);
}

// Show the screening steps one at a time, as the narration reaches each.
async function hideSteps(page) {
  await page.evaluate(() => { const b = document.getElementById('skip'); if (b) b.click(); document.querySelectorAll('.step').forEach((s) => s.classList.remove('on')); document.getElementById('process').scrollTop = 0; });
}
async function stepLine(page, n, caption, hold, offset = 18) {
  await quiet(page);
  await speak(page, caption);
  await page.evaluate((t) => { const bar = document.getElementById('demo-subtitle'); if (bar) { bar.textContent = t; bar.style.opacity = '1'; } }, caption);
  await page.evaluate((n) => document.querySelector(`.steps .step:nth-child(${n})`).classList.add('on'), n);
  await scrollTo(page, step(n), offset, hold);
}

async function showStep(page, n, offset = 18) {
  await quiet(page);
  await page.evaluate((n) => document.querySelector(`.steps .step:nth-child(${n})`).classList.add('on'), n);
  await scrollTo(page, step(n), offset, 700);
}

async function record(page) {
  // 0:00  The question. A plain cover is in place before the page paints, so the video opens on the card.
  await page.addInitScript(() => {
    if (!/cover=1/.test(location.search)) return;
    const put = () => {
      if (document.getElementById('demo-card') || !document.body) return false;
      const el = document.createElement('div');
      el.id = 'demo-card';
      el.style.cssText = 'position:fixed;inset:0;z-index:999997;display:grid;place-items:center;background:radial-gradient(1100px 700px at 50% 30%,#fffdf6,#f4f0e6 70%);opacity:1;transition:opacity .5s;pointer-events:none';
      document.body.appendChild(el);
      return true;
    };
    if (!put()) new MutationObserver((m, o) => { if (put()) o.disconnect(); }).observe(document.documentElement, { childList: true, subtree: true });
  });
  await page.goto(BASE + '/?cover=1&r=' + Date.now() + '#bank/BK-CS125943');
  await page.waitForSelector('.step');
  await injectOverlays(page);
  await hideSteps(page);
  await titleCard(page, H('A £1,800 payment.<br>The invoice matches to the penny.') + SUB('Should this alert be closed?'), 1500, 'hook');
  await titleCard(page, MARK(120) + WORD + SUB('Reads each alert the way a good analyst does, and shows its working.'), 1200, 'title');
  await dropCard(page);

  // 0:15  The seven screening steps, one at a time: each appears as its line starts.
  await stepLine(page, 1, '1 · Read the case', 2600, 120);
  await stepLine(page, 2, '2 · Read the alert', 2600);
  await stepLine(page, 3, '3 · Read the policy', 1800);
  await stepLine(page, 4, '4 · Match the documents to the money', 2600);
  await stepLine(page, 5, '5 · Look for signs of crime', 1600);
  await moveAndClick(page, `${step(5)} button[data-ev="PSC-5155"]`, 'control register', 2600);
  await stepLine(page, 6, '6 · Test each alert', 2000);
  await stepLine(page, 7, '7 · Decide: ESCALATE, and never contact the customer', 3200);
  await quiet(page);
  await scrollTo(page, '.next', 40, 600);
  await say(page, '', 1800);

  // 1:10  Most alerts close.
  await quiet(page);
  await page.goto(BASE + '/?r=' + Date.now() + '#investment/IV-CS103503');
  await page.waitForSelector('.step');
  await injectOverlays(page);
  await page.click('#skip');
  await scrollTo(page, step(4), 18, 500);
  await page.click(`${step(4)} button[data-ev="GFT-2351"]`).catch(() => {});
  await say(page, 'Most alerts do close: the gift letter matches donor, amount and date', 2600);
  await quiet(page);
  await scrollTo(page, step(7), 18, 800);
  await say(page, '28 checks that run as code: the same answer every time', 3000);
  await say(page, '', 1200);

  // 1:30  The same desk inside a chat bot.
  await quiet(page);
  await page.goto(BASE + '/bot?step&demo');
  await injectOverlays(page);
  const scene = async () => { await page.waitForFunction(() => window.botIdle === true, null, { timeout: 60000 }); await page.waitForTimeout(300); };
  const next = async () => { await quiet(page); await page.keyboard.press('Space'); await page.waitForTimeout(400); await scene(); };
  await scene();
  await say(page, 'The same desk, inside a chat bot, through the MCP connector', 600);
  await next();
  await say(page, 'Overnight, it reports the queue', 300);
  await next();
  await say(page, 'A new alert: escalated, referral drafted, customer not contacted', 300);
  await next();
  await say(page, 'Asked why, it answers from the case file. The rules decide; the bot explains', 300);
  await say(page, '', 2500);

  // Close.
  await quiet(page);
  await titleCard(page, MARK(92) + H('4,000 alerts in 2.6 seconds.<br>Identical on every run.') + SUB('Nothing closes unless the evidence explains it.'), 1500, 'close');
  await titleCard(page, MARK(110) + WORD + SUB('Track 1 · Transaction monitoring<br>London Financial Crime Hackathon, November 2026'), 3600);
}

async function replay(page) {
  await page.evaluate(() => { const b = document.getElementById('replay'); if (b) b.click(); });
  await page.waitForTimeout(3000);
}

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM || undefined });
  if (REHEARSAL) {
    const page = await (await browser.newContext({ viewport: SIZE })).newPage();
    const ok = await rehearse(page);
    await browser.close();
    process.exit(ok ? 0 : 1);
  }
  const context = await browser.newContext({ viewport: SIZE, recordVideo: { dir: OUT_DIR, size: SIZE } });
  const page = await context.newPage();
  t0 = Date.now();
  let failed = false;
  try { await record(page); } catch (err) { failed = true; console.error('DEMO ERROR:', err.message); }
  if (voiceUntil > Date.now()) await page.waitForTimeout(voiceUntil - Date.now() + 500);
  if (TIMELINE.length) fs.writeFileSync(path.join(VOICE_DIR, 'timeline.json'), JSON.stringify(TIMELINE, null, 1));
  await context.close();
  const src = await page.video().path();
  fs.copyFileSync(src, OUTPUT);
  fs.unlinkSync(src);
  console.log((failed ? 'Video saved with errors: ' : 'Video saved: ') + OUTPUT);
  await browser.close();
  process.exit(failed ? 1 : 0);
})();
