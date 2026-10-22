/**
 * GitFinds comment bot — Google Apps Script version (attached to the comment-bot sheet).
 *
 * Two ways comments reach us:
 *   1. POLL  — `poll()` runs every minute (time trigger). Fetches recent Instagram + YouTube
 *              comments, replies with the video's GitHub repo + user setup guide.
 *   2. WEBHOOK — deploy as Web app; register the URL as the Instagram webhook (field `comments`).
 *              `doPost` replies within seconds. (Meta app must be in Live mode for webhooks.)
 *
 * Sheet tabs:  Sheet1 (no. | github repo | user setup guide)
 *              youtube / Instagram (same + posted video url)
 *              Log (every comment handled; also the bot's memory)
 *
 * Script properties (Project Settings → Script properties):
 *   IG_ACCESS_TOKEN                 Instagram token for @gitfinds
 *   GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / YOUTUBE_COMMENTS_REFRESH_TOKEN   (YouTube; optional)
 *   VERIFY_TOKEN                    any word, used when registering the webhook (default: gitfinds)
 *   DRY_RUN                         "true" = log only, never send
 */

const IG_API = 'https://graph.instagram.com/v21.0';
const YT_API = 'https://www.googleapis.com/youtube/v3';
const LOOKBACK_HOURS = 72;
const MAX_PER_RUN = 20;
const DM_TEXT = 'Hey {name}! Here is the GitHub repo: {repo}\nSetup guide: {guide}\nReply here if you get stuck 🙌';
const PUBLIC_TEXT_IG = 'Sent you the repo + setup guide in your DMs 📩';
const PUBLIC_TEXT_YT = 'Here you go 👉 Repo: {repo} | Setup guide: {guide}';
const SPAM = ['http://', 'https://', 'www.', 'crypto', 'forex', 'follow for follow', 'f4f', 'check my page',
  'earn money', 'free followers', 'buy followers', 'onlyfans', 'promo code', 'binance', 'whatsapp +'];
const ABUSE = ['scam', 'scammer', 'fraud', 'fake', 'bullshit', 'fuck', 'shit', 'idiot', 'stupid', 'chutiya',
  'bhosd', 'madarch', 'behench', 'harami', 'bakwas', 'ghatiya'];
const LOG_HEADERS = ['time', 'platform', 'post_id', 'post_url', 'commenter_id', 'commenter_name',
  'comment_id', 'comment', 'action', 'reply', 'status', 'note'];

// ---------------------------------------------------------------- setup (run once)
function setup() {
  ScriptApp.getProjectTriggers().forEach(t => { if (t.getHandlerFunction() === 'poll') ScriptApp.deleteTrigger(t); });
  ScriptApp.newTrigger('poll').timeBased().everyMinutes(1).create();
  ensureLog_();
  Logger.log('OK — poll() now runs every minute.');
}

function stop() {
  ScriptApp.getProjectTriggers().forEach(t => ScriptApp.deleteTrigger(t));
  Logger.log('All triggers removed.');
}

// ---------------------------------------------------------------- helpers
const props_ = () => PropertiesService.getScriptProperties();
const prop_ = (k, d) => (props_().getProperty(k) || d || '').trim();
const dry_ = () => prop_('DRY_RUN').toLowerCase() === 'true';
const norm_ = s => String(s || '').normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase().replace(/\s+/g, ' ').trim();
const hit_ = (t, words) => words.find(w => norm_(t).includes(w)) || null;
const fill_ = (s, c, post) => s.replace(/{name}/g, c.user_name || '').replace(/{repo}/g, post.repo || '').replace(/{guide}/g, post.guide || '');

function urlKey_(u) {
  u = String(u || '');
  let m = u.match(/(?:v=|youtu\.be\/|shorts\/|embed\/|live\/)([A-Za-z0-9_-]{10,12})/);
  if (m) return 'yt:' + m[1];
  m = u.match(/instagram\.com\/(?:[^\/]+\/)?(?:p|reel|reels|tv)\/([A-Za-z0-9_-]+)/);
  if (m) return 'ig:' + m[1];
  return '';
}

function rows_(tabName) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sh = ss.getSheets().find(s => s.getName().trim().toLowerCase() === tabName.toLowerCase());
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
    if (repo) base.repo = repo; if (guide) base.guide = guide;
    if (base.repo || base.guide) { base.no = no; out[k] = base; }
  }));
  return out;
}

function ensureLog_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sh = ss.getSheets().find(s => s.getName().trim().toLowerCase() === 'log');
  if (!sh) { sh = ss.insertSheet('Log'); sh.appendRow(LOG_HEADERS); }
  return sh;
}

function loadLog_() {
  const seen = new Set(), replied = new Set();
  rows_('Log').forEach(r => {
    if (r.comment_id) seen.add(r.platform + ':' + r.comment_id);
    if ((r.status === 'sent' || r.status === 'dry-run') && r.commenter_id) replied.add(r.platform + ':' + r.post_id + ':' + r.commenter_id);
  });
  return { seen, replied };
}

function logRow_(c, action, reply, status, note) {
  ensureLog_().appendRow([Utilities.formatDate(new Date(), 'Asia/Kolkata', 'yyyy-MM-dd HH:mm:ss'), c.platform, c.post_id,
    c.post_url, c.user_id, c.user_name, c.comment_id, String(c.text).slice(0, 500), action, String(reply || '').slice(0, 500),
    status, String(note || '').slice(0, 300)]);
}

function http_(method, url, opts) {
  const o = Object.assign({ method, muteHttpExceptions: true }, opts || {});
  const r = UrlFetchApp.fetch(url, o);
  const code = r.getResponseCode(), body = r.getContentText();
  if (code >= 400) throw new Error(method + ' ' + url.split('?')[0] + ': ' + code + ' ' + body.slice(0, 300));
  return body ? JSON.parse(body) : {};
}

// ---------------------------------------------------------------- instagram
function igGet_(path, params) {
  const q = Object.entries(Object.assign({ access_token: prop_('IG_ACCESS_TOKEN') }, params || {}))
    .map(([k, v]) => k + '=' + encodeURIComponent(v)).join('&');
  return http_('get', IG_API + '/' + path + '?' + q);
}
function igPost_(path, payload) {
  return http_('post', IG_API + '/' + path + '?access_token=' + encodeURIComponent(prop_('IG_ACCESS_TOKEN')),
    { contentType: 'application/json', payload: JSON.stringify(payload) });
}

function instagramComments_() {
  const me = igGet_('me', { fields: 'id,username' });
  const cutoff = Date.now() - LOOKBACK_HOURS * 3600e3, out = [];
  const media = igGet_('me/media', { fields: 'id,caption,permalink,timestamp,comments.limit(50){id,text,username,timestamp,from,replies{id,text,username,timestamp,from}}', limit: 15 }).data || [];
  media.forEach(m => ((m.comments || {}).data || []).forEach(c => {
    [[c, ''], ...(((c.replies || {}).data) || []).map(r => [r, c.id])].forEach(([x]) => {
      if (x.timestamp && new Date(x.timestamp).getTime() < cutoff) return;
      const frm = x.from || {}, uid = frm.id || x.username || x.id, uname = frm.username || x.username || '';
      out.push({ platform: 'instagram', comment_id: x.id, post_id: m.id, post_url: m.permalink || '', key: urlKey_(m.permalink),
        user_id: uid, user_name: uname, text: x.text || '', is_own: uid === me.id || uname === me.username });
    });
  }));
  return { my_id: me.id, items: out };
}

function instagramReply_(myId, c, pub, dm) {
  const notes = [];
  try { igPost_(myId + '/messages', { recipient: { comment_id: c.comment_id }, message: { text: dm } }); notes.push('dm:ok'); }
  catch (e) { notes.push('dm:FAIL ' + e.message); }
  try { igPost_(c.comment_id + '/replies', { message: pub }); notes.push('public:ok'); }
  catch (e) { notes.push('public:FAIL ' + e.message); }
  return notes.join(' | ');
}

// ---------------------------------------------------------------- youtube
function ytToken_() {
  const rt = prop_('YOUTUBE_COMMENTS_REFRESH_TOKEN');
  if (!rt) return null;
  const r = http_('post', 'https://oauth2.googleapis.com/token', { payload: { client_id: prop_('GOOGLE_CLIENT_ID'),
    client_secret: prop_('GOOGLE_CLIENT_SECRET'), refresh_token: rt, grant_type: 'refresh_token' } });
  return r.access_token;
}
function yt_(method, path, token, params, payload) {
  const q = Object.entries(params || {}).map(([k, v]) => k + '=' + encodeURIComponent(v)).join('&');
  return http_(method, YT_API + '/' + path + (q ? '?' + q : ''), { headers: { Authorization: 'Bearer ' + token },
    contentType: 'application/json', payload: payload ? JSON.stringify(payload) : undefined });
}

function youtubeComments_(token) {
  const ch = yt_('get', 'channels', token, { part: 'id,contentDetails', mine: 'true' }).items[0];
  const vids = yt_('get', 'playlistItems', token, { part: 'snippet', playlistId: ch.contentDetails.relatedPlaylists.uploads, maxResults: 10 }).items
    .map(i => i.snippet.resourceId.videoId);
  const cutoff = Date.now() - LOOKBACK_HOURS * 3600e3, out = [];
  vids.forEach(vid => {
    let threads = [];
    try { threads = yt_('get', 'commentThreads', token, { part: 'snippet,replies', videoId: vid, order: 'time', maxResults: 50, textFormat: 'plainText' }).items; } catch (e) { return; }
    threads.forEach(t => {
      const top = t.snippet.topLevelComment;
      [[top, top.id], ...(((t.replies || {}).comments) || []).map(r => [r, top.id])].forEach(([x, parent]) => {
        const s = x.snippet;
        if (new Date(s.publishedAt).getTime() < cutoff) return;
        const uid = (s.authorChannelId || {}).value || s.authorDisplayName || '';
        out.push({ platform: 'youtube', comment_id: x.id, post_id: vid, post_url: 'https://www.youtube.com/watch?v=' + vid, key: 'yt:' + vid,
          user_id: uid, user_name: s.authorDisplayName || '', text: s.textOriginal || s.textDisplay || '', parent, is_own: uid === ch.id });
      });
    });
  });
  return { my_id: ch.id, items: out };
}

// ---------------------------------------------------------------- core
function handle_(c, ctx) {
  const key = c.platform + ':' + c.comment_id;
  if (ctx.seen.has(key) || c.is_own || norm_(c.text).startsWith('sent you the repo') || norm_(c.text).startsWith('here you go') || !String(c.text).trim()) return false;
  ctx.seen.add(key);
  const rk = c.platform + ':' + c.post_id + ':' + c.user_id;
  if (ctx.replied.has(rk)) { logRow_(c, 'skip', '', 'skipped', 'already replied to this person on this post'); return false; }
  let w = hit_(c.text, SPAM); if (w) { logRow_(c, 'skip-spam', '', 'skipped', 'matched: ' + w); return false; }
  w = hit_(c.text, ABUSE); if (w) { logRow_(c, 'skip-abuse', '', 'skipped', 'matched: ' + w); return false; }
  const post = ctx.posts[c.key];
  if (!post) { logRow_(c, 'flag-no-post', '', 'flagged', 'post not in sheet — add its URL to the platform tab with its repo no.'); return false; }
  const reply = fill_(c.platform === 'instagram' ? DM_TEXT : PUBLIC_TEXT_YT, c, post);
  if (dry_()) { ctx.replied.add(rk); logRow_(c, 'post-match', reply, 'dry-run', 'would send'); return true; }
  const note = c.platform === 'instagram'
    ? instagramReply_(ctx.ig_id, c, fill_(PUBLIC_TEXT_IG, c, post), reply)
    : (() => { try { yt_('post', 'comments', ctx.yt_token, { part: 'snippet' }, { snippet: { parentId: c.parent, textOriginal: reply } }); return 'public:ok'; } catch (e) { return 'public:FAIL ' + e.message; } })();
  const ok = note.includes(':ok');
  if (ok) ctx.replied.add(rk);
  logRow_(c, 'post-match', reply, ok ? 'sent' : 'failed', note);
  return ok;
}

function poll() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) return;
  try {
    const ctx = Object.assign({ posts: loadPosts_() }, loadLog_());
    let sent = 0;
    if (prop_('IG_ACCESS_TOKEN')) {
      try { const r = instagramComments_(); ctx.ig_id = r.my_id; for (const c of r.items) { if (sent >= MAX_PER_RUN) break; if (handle_(c, ctx)) sent++; } }
      catch (e) { Logger.log('instagram failed: ' + e.message); }
    }
    try { const tok = ytToken_(); if (tok) { ctx.yt_token = tok; const r = youtubeComments_(tok); for (const c of r.items) { if (sent >= MAX_PER_RUN) break; if (handle_(c, ctx)) sent++; } } }
    catch (e) { Logger.log('youtube failed: ' + e.message); }
    Logger.log('done — ' + sent + ' replies');
  } finally { lock.releaseLock(); }
}

// ---------------------------------------------------------------- webhook (instant mode)
function doGet(e) {
  const p = (e && e.parameter) || {};
  if (p['hub.mode'] === 'subscribe' && p['hub.verify_token'] === prop_('VERIFY_TOKEN', 'gitfinds')) {
    return ContentService.createTextOutput(p['hub.challenge']);
  }
  return ContentService.createTextOutput('GitFinds comment bot is running');
}

function doPost(e) {
  try {
    const body = JSON.parse(e.postData.contents || '{}');
    const ctx = Object.assign({ posts: loadPosts_() }, loadLog_());
    ctx.ig_id = igGet_('me', { fields: 'id' }).id;
    (body.entry || []).forEach(en => (en.changes || []).forEach(ch => {
      if (ch.field !== 'comments') return;
      const v = ch.value || {};
      const mediaId = (v.media || {}).id;
      let permalink = '';
      try { permalink = igGet_(mediaId, { fields: 'permalink' }).permalink || ''; } catch (err) {}
      const c = { platform: 'instagram', comment_id: v.id, post_id: mediaId, post_url: permalink, key: urlKey_(permalink),
        user_id: (v.from || {}).id || '', user_name: (v.from || {}).username || '', text: v.text || '',
        is_own: ((v.from || {}).id || '') === ctx.ig_id };
      handle_(c, ctx);
    }));
  } catch (err) { Logger.log('webhook error: ' + err.message); }
  return ContentService.createTextOutput('ok');
}
