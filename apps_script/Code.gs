/**
 * GitFinds helper — Google Apps Script, attached to the "repo shared in zoom group" sheet.
 *
 * What it does
 *   1. COMMENT BOT  — answers every new comment with that video's GitHub repo + setup guide
 *        Instagram: DM (repo + guide) + short public reply      → within seconds (webhook) or ≤1 min (check)
 *        YouTube:   public reply with repo + guide links        → within 1–2 minutes
 *      Never replies to itself, never twice to the same person on the same video,
 *      skips spam/abuse, logs everything in the "Log" tab (the Log is its memory).
 *   2. EMAILS       — sends every GitFinds notification to ALERT_EMAIL
 *        (posting results from GitHub, missing repo, broken login, daily comment summary).
 *   3. DAILY START  — at 18:05 IST tells GitHub to start the daily posting run (backup at 18:35),
 *        because GitHub's own timer often skips runs. Needs GH_DISPATCH_TOKEN.
 *   4. INSTAGRAM KEY — renews its own Instagram key every 7 days.
 *
 * Run once:  setup()   (Run ▸ setup, allow permissions).   Stop everything: stop()
 * Test mode: Script property DRY_RUN = true → nothing is sent, results go to the "Test Log" tab.
 *
 * Script properties (Project Settings ▸ Script properties):
 *   IG_ACCESS_TOKEN                       Instagram key for @gitfinds
 *   GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, YOUTUBE_COMMENTS_REFRESH_TOKEN   (YouTube comments)
 *   ALERT_EMAIL                           where every notification goes
 *   HOOK_SECRET                           any long random word; the same value is saved in GitHub as EMAIL_HOOK_SECRET
 *   GH_DISPATCH_TOKEN                     (optional) GitHub token that may start the posting workflow
 *   VERIFY_TOKEN                          (optional) word used when connecting the Instagram webhook (default: gitfinds)
 *   DRY_RUN                               true = test mode
 */

const IG_API = 'https://graph.instagram.com/v21.0';
const YT_API = 'https://www.googleapis.com/youtube/v3';
const GH_REPO = 'sourabhkddigital-gif/gitfinds-autopost';
const GH_WORKFLOW = 'post.yml';
const LOOKBACK_HOURS = 72;
const MAX_PER_RUN = 10;
const YT_RECENT_VIDEOS = 3;      // checked every minute
const YT_ALL_VIDEOS = 10;        // checked every 10 minutes
const FAIL_ALERT_AFTER = 15;     // consecutive failed checks (≈15 min) before an email
const DM_TEXT = 'Hey{at_name} 👋 thanks for the comment!\n\nHere\'s {tool}, as promised:\n🔗 GitHub: {repo}\n📘 Setup guide (step by step): {guide}\n\nStuck anywhere? Just reply here 🙌\nFollow @gitfinds for a new free tool every day at 7 PM.';
const PUBLIC_TEXT_IG = 'Sent you the repo + setup guide in your DMs 📩';
const PUBLIC_TEXT_YT = 'Here you go 👉 Repo: {repo} | Setup guide: {guide}';
const SPAM = ['http://', 'https://', 'www.', 'crypto', 'forex', 'follow for follow', 'f4f', 'check my page',
  'earn money', 'free followers', 'buy followers', 'onlyfans', 'promo code', 'binance', 'whatsapp +'];
const ABUSE = ['scam', 'scammer', 'fraud', 'fake', 'bullshit', 'fuck', 'shit', 'idiot', 'stupid', 'chutiya',
  'bhosd', 'madarch', 'behench', 'harami', 'bakwas', 'ghatiya'];
const LOG_HEADERS = ['time', 'platform', 'post_id', 'post_url', 'commenter_id', 'commenter_name',
  'comment_id', 'comment', 'action', 'reply', 'status', 'note'];

// ================================================================ setup
function setup() {
  stop();
  ScriptApp.newTrigger('poll').timeBased().everyMinutes(1).create();
  ScriptApp.newTrigger('refreshInstagramKey').timeBased().everyDays(7).create();
  logSheet_(false); logSheet_(true);
  if (!prop_('HOOK_SECRET')) props_().setProperty('HOOK_SECRET', Utilities.getUuid());
  Logger.log('OK — comment check every minute, Instagram key renewal every 7 days. HOOK_SECRET = ' + prop_('HOOK_SECRET'));
}

function stop() {
  ScriptApp.getProjectTriggers().forEach(t => ScriptApp.deleteTrigger(t));
  Logger.log('All timers removed (bot stopped).');
}

// ================================================================ helpers
const props_ = () => PropertiesService.getScriptProperties();
const prop_ = (k, d) => String(props_().getProperty(k) || d || '').trim();
const dry_ = () => prop_('DRY_RUN').toLowerCase() === 'true';
const norm_ = s => String(s || '').normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase().replace(/\s+/g, ' ').trim();
const hit_ = (t, words) => words.find(w => norm_(t).includes(w)) || null;
const fill_ = (s, c, post) => {
  const repo = String(post.repo || '').trim(), guide = String(post.guide || '').trim(), name = String(c.user_name || '').trim();
  if (!guide) s = s.replace(/\s*\|\s*Setup guide:\s*\{guide\}/g, '').replace(/\n[^\n]*\{guide\}/g, '');
  const tool = repo ? repo.replace(/\/+$/, '').split('/').pop() : 'the repo';
  return s.replace(/{at_name}/g, name ? ' @' + name : '').replace(/{name}/g, name).replace(/{tool}/g, tool)
          .replace(/{repo}/g, repo).replace(/{guide}/g, guide);
};
const now_ = () => Utilities.formatDate(new Date(), 'Asia/Kolkata', 'yyyy-MM-dd HH:mm:ss');
const today_ = () => Utilities.formatDate(new Date(), 'Asia/Kolkata', 'yyyy-MM-dd');
const istMinutes_ = () => { const t = Utilities.formatDate(new Date(), 'Asia/Kolkata', 'HH:mm').split(':'); return +t[0] * 60 + +t[1]; };

// Our own reply texts: a comment that starts like one of these was written by the bot.
const OWN_PREFIXES = [PUBLIC_TEXT_IG, PUBLIC_TEXT_YT].map(t => norm_(t.split('{')[0]).slice(0, 18)).filter(Boolean);
const looksOwn_ = text => OWN_PREFIXES.some(p => norm_(text).startsWith(p));

function urlKey_(u) {
  u = String(u || '');
  let m = u.match(/(?:v=|youtu\.be\/|shorts\/|embed\/|live\/)([A-Za-z0-9_-]{10,12})/);
  if (m) return 'yt:' + m[1];
  m = u.match(/instagram\.com\/(?:[^\/]+\/)?(?:p|reel|reels|tv)\/([A-Za-z0-9_-]+)/);
  if (m) return 'ig:' + m[1];
  return '';
}

function sheetByName_(name) {
  return SpreadsheetApp.getActiveSpreadsheet().getSheets().find(s => s.getName().trim().toLowerCase() === name.toLowerCase()) || null;
}

function rows_(tabName) {
  const sh = sheetByName_(tabName);
  if (!sh) return [];
  const v = sh.getDataRange().getValues();
  if (v.length < 2) return [];
  const h = v[0].map(x => String(x).trim());
  return v.slice(1).map(r => { const o = {}; h.forEach((k, i) => o[k] = String(r[i] == null ? '' : r[i]).trim()); return o; });
}

function pick_(row, cands) {
  const keys = Object.keys(row);
  for (const c of cands) for (const k of keys) if (k.toLowerCase().replace(/[^a-z]/g, '') === c && row[k]) return row[k];
  for (const c of cands) for (const k of keys) if (k.toLowerCase().replace(/\s/g, '').includes(c) && row[k]) return row[k];
  return '';
}

function loadPosts_() {
  const cache = CacheService.getScriptCache(), hit = cache.get('posts');
  if (hit) return JSON.parse(hit);
  const out = readPosts_();
  try { cache.put('posts', JSON.stringify(out), 120); } catch (e) {}
  return out;
}

function readPosts_() {
  const master = {};
  rows_('Sheet1').forEach(r => {
    const no = pick_(r, ['no', 'reponumber', 'number', 'sno']).replace(/\.0$/, '');
    const repo = pick_(r, ['githubrepo', 'github', 'repo']), guide = pick_(r, ['usersetupguide', 'setupguide', 'guide']);
    if (no && (repo || guide)) master[no] = { repo, guide };
  });
  const out = {};
  ['youtube', 'Instagram', 'facebook'].forEach(tab => rows_(tab).forEach(r => {
    const no = pick_(r, ['no', 'reponumber', 'number', 'sno']).replace(/\.0$/, '');
    const k = urlKey_(pick_(r, ['postedvideourl', 'postedvideolink', 'videourl', 'posturl', 'url', 'link']));
    if (!k) return;
    const base = Object.assign({}, master[no] || {});
    const repo = pick_(r, ['githubrepo', 'github', 'repo']), guide = pick_(r, ['usersetupguide', 'setupguide', 'guide']);
    if (repo) base.repo = repo;
    if (guide) base.guide = guide;
    if (base.repo || base.guide) { base.no = no; out[k] = base; }
  }));
  return out;
}

function logSheet_(test) {
  const name = test ? 'Test Log' : 'Log';
  let sh = sheetByName_(name);
  if (!sh) { sh = SpreadsheetApp.getActiveSpreadsheet().insertSheet(name); sh.appendRow(LOG_HEADERS); }
  return sh;
}

// Memory from the Log (and, in test mode, also from the Test Log so tests don't repeat themselves).
function loadMemory_() {
  const mem = { seen: new Set(), replied: new Set(), flagged: new Set(), ownIds: new Set() };
  const read = r => {
    const st = r.status, cid = r.comment_id, key = r.platform + ':' + cid;
    if (cid) (st === 'flagged' ? mem.flagged : mem.seen).add(key);
    if ((st === 'sent' || st === 'dry-run') && r.commenter_id) mem.replied.add(r.platform + ':' + r.post_id + ':' + r.commenter_id);
    const m = String(r.note || '').match(/reply_id=([A-Za-z0-9_.\-]+)/g);
    if (m) m.forEach(x => mem.ownIds.add(r.platform + ':' + x.split('=')[1]));
  };
  rows_('Log').forEach(read);
  if (dry_()) rows_('Test Log').forEach(read);
  // a flagged comment that was answered later is "seen"
  mem.flagged.forEach(k => { if (mem.seen.has(k)) mem.flagged.delete(k); });
  return mem;
}

function logRow_(c, action, reply, status, note) {
  logSheet_(dry_()).appendRow([now_(), c.platform, c.post_id, c.post_url, c.user_id, c.user_name, c.comment_id,
    String(c.text).slice(0, 500), action, String(reply || '').slice(0, 500), status, String(note || '').slice(0, 300)]);
}

function http_(method, url, opts) {
  const o = Object.assign({ method, muteHttpExceptions: true }, opts || {});
  const r = UrlFetchApp.fetch(url, o);
  const code = r.getResponseCode(), body = r.getContentText();
  if (code >= 400) throw new Error(method.toUpperCase() + ' ' + url.split('?')[0].replace(/access_token=[^&]+/, '') + ': ' + code + ' ' + body.slice(0, 300));
  return body ? JSON.parse(body) : {};
}

// ================================================================ email
function email_(subject, body) {
  const to = prop_('ALERT_EMAIL');
  if (!to) { Logger.log('ALERT_EMAIL not set — email skipped: ' + subject); return false; }
  MailApp.sendEmail({ to, subject, body, name: 'GitFinds Bot' });
  return true;
}

// one email per key per day (stops repeats)
function emailOnce_(key, subject, body) {
  const k = 'mailed:' + key + ':' + today_();
  if (props_().getProperty(k)) return;
  if (email_(subject, body)) props_().setProperty(k, '1');
}

function trackHealth_(platform, ok, err) {
  const k = 'fails:' + platform, n = ok ? 0 : (+prop_(k, '0') + 1);
  props_().setProperty(k, String(n));
  if (n === FAIL_ALERT_AFTER) {
    const help = platform === 'youtube'
      ? 'Most likely the YouTube comments login (YOUTUBE_COMMENTS_REFRESH_TOKEN) is missing or expired. Make a new one and paste it in Script properties.'
      : 'Most likely the Instagram key (IG_ACCESS_TOKEN) expired or Meta blocked the app. Make a new key and paste it in Script properties.';
    email_('⚠️ GitFinds comment bot: ' + platform + ' replies are not working',
      'The comment bot could not read ' + platform + ' comments for the last ' + n + ' checks.\n\nError: ' + err + '\n\nWhat to do: ' + help);
  }
}

// ================================================================ instagram
function igGet_(path, params) {
  const q = Object.entries(Object.assign({ access_token: prop_('IG_ACCESS_TOKEN') }, params || {}))
    .map(([k, v]) => k + '=' + encodeURIComponent(v)).join('&');
  return http_('get', IG_API + '/' + path + '?' + q);
}
function igPost_(path, payload) {
  return http_('post', IG_API + '/' + path + '?access_token=' + encodeURIComponent(prop_('IG_ACCESS_TOKEN')),
    { contentType: 'application/json', payload: JSON.stringify(payload) });
}

function igMe_() {
  const c = CacheService.getScriptCache(), hit = c.get('ig_me');
  if (hit) return JSON.parse(hit);
  const me = igGet_('me', { fields: 'id,user_id,username' });
  const out = { ids: [me.id, me.user_id].filter(Boolean).map(String), username: me.username || '' };
  c.put('ig_me', JSON.stringify(out), 21600);
  return out;
}

function instagramComments_() {
  const me = igMe_();
  const cutoff = Date.now() - LOOKBACK_HOURS * 3600e3, out = [];
  const media = igGet_('me/media', { fields: 'id,permalink,timestamp,comments.limit(50){id,text,username,timestamp,from,replies{id,text,username,timestamp,from}}', limit: 15 }).data || [];
  media.forEach(m => ((m.comments || {}).data || []).forEach(c => {
    [c].concat(((c.replies || {}).data) || []).forEach(x => {
      if (x.timestamp && new Date(x.timestamp).getTime() < cutoff) return;
      out.push(igItem_(x, m.id, m.permalink || '', me));
    });
  }));
  return out;
}

function igItem_(x, mediaId, permalink, me) {
  const frm = x.from || {}, uname = frm.username || x.username || '';
  const uid = String(frm.id || x.username || x.id);
  return { platform: 'instagram', comment_id: String(x.id), post_id: String(mediaId), post_url: permalink, key: urlKey_(permalink),
    user_id: uid, user_name: uname, text: x.text || '',
    is_own: me.ids.indexOf(String(frm.id || '')) >= 0 || (!!uname && uname === me.username) };
}

function instagramReply_(c, pub, dm) {
  const notes = [], myId = igMe_().ids[0];
  try { igPost_(myId + '/messages', { recipient: { comment_id: c.comment_id }, message: { text: dm } }); notes.push('dm:ok'); }
  catch (e) { notes.push('dm:FAIL ' + e.message); }
  try { const r = igPost_(c.comment_id + '/replies', { message: pub }); notes.push('public:ok reply_id=' + (r.id || '')); }
  catch (e) { notes.push('public:FAIL ' + e.message); }
  return notes.join(' | ');
}

function refreshInstagramKey() {
  if (!prop_('IG_ACCESS_TOKEN')) return;
  try {
    const r = http_('get', 'https://graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token&access_token=' + encodeURIComponent(prop_('IG_ACCESS_TOKEN')));
    if (r.access_token) { props_().setProperty('IG_ACCESS_TOKEN', r.access_token); Logger.log('Instagram key renewed'); }
  } catch (e) {
    email_('⚠️ GitFinds: Instagram key renewal failed', 'The comment bot could not renew its Instagram key.\n\nError: ' + e.message +
      '\n\nWhat to do: make a new Instagram key (Meta app ▸ Instagram ▸ API setup ▸ Generate token) and paste it in Script properties as IG_ACCESS_TOKEN.');
  }
}

// ================================================================ youtube
function ytToken_() {
  const c = CacheService.getScriptCache(), hit = c.get('yt_token');
  if (hit) return hit;
  const rt = prop_('YOUTUBE_COMMENTS_REFRESH_TOKEN');
  if (!rt) return null;
  const r = http_('post', 'https://oauth2.googleapis.com/token', { payload: { client_id: prop_('GOOGLE_CLIENT_ID'),
    client_secret: prop_('GOOGLE_CLIENT_SECRET'), refresh_token: rt, grant_type: 'refresh_token' } });
  c.put('yt_token', r.access_token, 3000);
  return r.access_token;
}
function yt_(method, path, token, params, payload) {
  const q = Object.entries(params || {}).map(([k, v]) => k + '=' + encodeURIComponent(v)).join('&');
  const o = { headers: { Authorization: 'Bearer ' + token } };
  if (payload) { o.contentType = 'application/json'; o.payload = JSON.stringify(payload); }
  return http_(method, YT_API + '/' + path + (q ? '?' + q : ''), o);
}

function ytChannel_(token) {
  const c = CacheService.getScriptCache(), hit = c.get('yt_channel');
  if (hit) return JSON.parse(hit);
  const ch = yt_('get', 'channels', token, { part: 'id,contentDetails', mine: 'true' }).items[0];
  const out = { id: ch.id, uploads: ch.contentDetails.relatedPlaylists.uploads };
  c.put('yt_channel', JSON.stringify(out), 21600);
  return out;
}

function ytVideos_(token, ch) {
  const c = CacheService.getScriptCache(), hit = c.get('yt_videos');
  if (hit) return JSON.parse(hit);
  const vids = yt_('get', 'playlistItems', token, { part: 'snippet', playlistId: ch.uploads, maxResults: YT_ALL_VIDEOS }).items
    .map(i => i.snippet.resourceId.videoId);
  c.put('yt_videos', JSON.stringify(vids), 600);
  return vids;
}

function youtubeComments_(token, all) {
  const ch = ytChannel_(token);
  const vids = ytVideos_(token, ch).slice(0, all ? YT_ALL_VIDEOS : YT_RECENT_VIDEOS);
  const cutoff = Date.now() - LOOKBACK_HOURS * 3600e3, out = [];
  vids.forEach(vid => {
    let threads = [];
    try { threads = yt_('get', 'commentThreads', token, { part: 'snippet,replies', videoId: vid, order: 'time', maxResults: 50, textFormat: 'plainText' }).items || []; }
    catch (e) { if (String(e.message).indexOf('commentsDisabled') < 0) throw e; return; }
    threads.forEach(t => {
      const top = t.snippet.topLevelComment;
      [top].concat(((t.replies || {}).comments) || []).forEach(x => {
        const s = x.snippet;
        if (new Date(s.publishedAt).getTime() < cutoff) return;
        const uid = (s.authorChannelId || {}).value || s.authorDisplayName || '';
        out.push({ platform: 'youtube', comment_id: x.id, post_id: vid, post_url: 'https://www.youtube.com/watch?v=' + vid, key: 'yt:' + vid,
          user_id: uid, user_name: s.authorDisplayName || '', text: s.textOriginal || s.textDisplay || '', parent: top.id, is_own: uid === ch.id });
      });
    });
  });
  return out;
}

// ================================================================ core
function handle_(c, ctx) {
  const key = c.platform + ':' + c.comment_id;
  if (ctx.mem.seen.has(key) || c.is_own || ctx.mem.ownIds.has(key) || looksOwn_(c.text) || !String(c.text).trim()) return false;
  const rk = c.platform + ':' + c.post_id + ':' + c.user_id;
  if (ctx.mem.replied.has(rk)) { ctx.mem.seen.add(key); logRow_(c, 'skip', '', 'skipped', 'already replied to this person on this post'); return false; }
  let w = hit_(c.text, SPAM);
  if (w) { ctx.mem.seen.add(key); logRow_(c, 'skip-spam', '', 'skipped', 'matched: ' + w); return false; }
  w = hit_(c.text, ABUSE);
  if (w) { ctx.mem.seen.add(key); logRow_(c, 'skip-abuse', '', 'skipped', 'matched: ' + w); return false; }
  const post = ctx.posts[c.key];
  if (!post) {
    // not answered yet — retried every check, so it is answered as soon as the repo is added to the sheet
    if (!ctx.mem.flagged.has(key)) {
      ctx.mem.flagged.add(key);
      logRow_(c, 'flag-no-post', '', 'flagged', 'post not in sheet — add its URL to the platform tab with its repo no.');
      emailOnce_('nopost:' + c.key, '📌 GitFinds: comments waiting — video has no repo in the sheet',
        'People are commenting on this ' + c.platform + ' video, but the comment bot doesn\'t know which repo to send:\n\n' + c.post_url +
        '\n\nWhat to do: open the "repo shared in zoom group" sheet, and in the "' + (c.platform === 'youtube' ? 'youtube' : 'Instagram') +
        '" tab add a row with the repo no., GitHub repo, setup guide and this video link.\nWaiting comments are answered automatically within a minute after that.');
    }
    return false;
  }
  ctx.mem.seen.add(key);
  const reply = fill_(c.platform === 'instagram' ? DM_TEXT : PUBLIC_TEXT_YT, c, post);
  if (dry_()) { ctx.mem.replied.add(rk); logRow_(c, 'post-match', reply, 'dry-run', 'TEST — would send'); return true; }
  if (ctx.sent > 0) Utilities.sleep(2000 + Math.floor(Math.random() * 3000));   // look human, avoid rate limits
  let note;
  if (c.platform === 'instagram') {
    note = instagramReply_(c, fill_(PUBLIC_TEXT_IG, c, post), reply);
  } else {
    try {
      const r = yt_('post', 'comments', ctx.yt_token, { part: 'snippet' }, { snippet: { parentId: c.parent, textOriginal: reply } });
      note = 'public:ok reply_id=' + (r.id || '');
    } catch (e) { note = 'public:FAIL ' + e.message; }
  }
  const ok = /:ok/.test(note);
  if (ok) ctx.mem.replied.add(rk);
  const reIds = note.match(/reply_id=([A-Za-z0-9_.\-]+)/g);
  if (reIds) reIds.forEach(x => ctx.mem.ownIds.add(c.platform + ':' + x.split('=')[1]));
  logRow_(c, 'post-match', reply, ok ? 'sent' : 'failed', note);
  return ok;
}

function run_(ctx, items) {
  for (const c of items) {
    if (ctx.sent >= MAX_PER_RUN) break;
    if (handle_(c, ctx)) ctx.sent++;
  }
}

function poll() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) return;
  try {
    const ctx = { posts: loadPosts_(), mem: loadMemory_(), sent: 0 };
    if (prop_('IG_ACCESS_TOKEN')) {
      try { run_(ctx, instagramComments_()); trackHealth_('instagram', true); }
      catch (e) { Logger.log('instagram failed: ' + e.message); trackHealth_('instagram', false, e.message); }
    }
    if (prop_('YOUTUBE_COMMENTS_REFRESH_TOKEN')) {
      try {
        ctx.yt_token = ytToken_();
        run_(ctx, youtubeComments_(ctx.yt_token, new Date().getMinutes() % 10 === 0));
        trackHealth_('youtube', true);
      } catch (e) { Logger.log('youtube failed: ' + e.message); trackHealth_('youtube', false, e.message); }
    }
    Logger.log('done — ' + ctx.sent + ' replies' + (dry_() ? ' (TEST mode)' : ''));
  } finally { lock.releaseLock(); }
  try { timedJobs_(); } catch (e) { Logger.log('timed jobs: ' + e.message); }
}

// ================================================================ daily jobs (run from poll)
function timedJobs_() {
  const m = istMinutes_(), day = today_();
  // tell GitHub to start the daily posting (18:05 IST, backup 18:35)
  [[18 * 60 + 5, 'a'], [18 * 60 + 35, 'b']].forEach(([at, tag]) => {
    const k = 'dispatched:' + tag + ':' + day;
    if (m >= at && m < at + 50 && prop_('GH_DISPATCH_TOKEN') && !props_().getProperty(k)) {
      props_().setProperty(k, '1');
      try { startPosting(); } catch (e) { Logger.log('dispatch failed: ' + e.message); }
    }
  });
  // daily comment summary at 21:30 IST
  if (m >= 21 * 60 + 30 && !props_().getProperty('summary:' + day)) {
    props_().setProperty('summary:' + day, '1');
    dailySummary();
  }
  // clean old one-day markers
  if (m < 5) props_().getKeys().filter(k => /:(\d{4}-\d\d-\d\d)$/.test(k) && !k.endsWith(day)).forEach(k => props_().deleteProperty(k));
}

function startPosting() {
  http_('post', 'https://api.github.com/repos/' + GH_REPO + '/actions/workflows/' + GH_WORKFLOW + '/dispatches', {
    headers: { Authorization: 'Bearer ' + prop_('GH_DISPATCH_TOKEN'), Accept: 'application/vnd.github+json' },
    contentType: 'application/json', payload: JSON.stringify({ ref: 'main', inputs: { mode: 'normal' } }) });
  Logger.log('Asked GitHub to start the daily posting run');
}

function dailySummary() {
  const day = today_(), rows = rows_('Log').filter(r => String(r.time).startsWith(day));
  const count = (p, st) => rows.filter(r => r.platform === p && (!st || r.status === st)).length;
  const lines = ['instagram', 'youtube'].map(p => (p === 'instagram' ? '📸 Instagram' : '▶️ YouTube') + ': ' + count(p) + ' comments — ' +
    count(p, 'sent') + ' answered, ' + count(p, 'skipped') + ' skipped (spam/repeat), ' + count(p, 'flagged') + ' waiting for a repo, ' + count(p, 'failed') + ' failed');
  const waiting = rows.filter(r => r.status === 'flagged').map(r => r.post_url).filter((u, i, a) => a.indexOf(u) === i);
  email_('💬 GitFinds comments today (' + day + ')', lines.join('\n') +
    (waiting.length ? '\n\nVideos that need a repo in the sheet:\n' + waiting.join('\n') : '') +
    '\n\nFull details: "Log" tab of the "repo shared in zoom group" sheet.');
}

// ================================================================ web app (Instagram webhook + email hook)
function doGet(e) {
  const p = (e && e.parameter) || {};
  if (p['hub.mode'] === 'subscribe' && p['hub.verify_token'] === prop_('VERIFY_TOKEN', 'gitfinds')) {
    return ContentService.createTextOutput(p['hub.challenge']);
  }
  return ContentService.createTextOutput('GitFinds helper is running');
}

function doPost(e) {
  let body = {};
  try { body = JSON.parse((e && e.postData && e.postData.contents) || '{}'); } catch (err) { return ContentService.createTextOutput('bad json'); }

  // 1) email request from the GitHub posting robot
  if (body.type === 'email') {
    if (!body.secret || body.secret !== prop_('HOOK_SECRET')) return ContentService.createTextOutput('forbidden');
    email_(String(body.subject || 'GitFinds update'), String(body.body || ''));
    return ContentService.createTextOutput('sent');
  }

  // 2) Instagram webhook: a new comment → answer now
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(20000)) return ContentService.createTextOutput('busy');   // the 1-minute check picks it up
  try {
    const items = [], me = igMe_();
    (body.entry || []).forEach(en => (en.changes || []).forEach(ch => {
      if (ch.field !== 'comments') return;
      const v = ch.value || {}, mediaId = (v.media || {}).id;
      if (!mediaId || !v.id) return;
      let permalink = '';
      try { permalink = igGet_(mediaId, { fields: 'permalink' }).permalink || ''; } catch (err) {}
      items.push(igItem_({ id: v.id, text: v.text, from: v.from }, mediaId, permalink, me));
    }));
    if (items.length) run_({ posts: loadPosts_(), mem: loadMemory_(), sent: 0 }, items);
  } catch (err) { Logger.log('webhook error: ' + err.message); }
  finally { lock.releaseLock(); }
  return ContentService.createTextOutput('ok');
}
