"""GitFinds comment bot — runs on GitHub Actions (free, no PC).

Every run:
  1. reads the comment-bot Google Sheet:
       Sheet1     no. | github repo | user setup guide          (master list)
       youtube    no. | github repo | user setup guide | posted video url
       Instagram  no. | github repo | user setup guide | posted video link
       Log        time | platform | post_id | post_url | commenter_id | commenter_name |
                  comment_id | comment | action | reply | status | note
  2. fetches new comments on recent Instagram posts and YouTube videos,
  3. for a comment on a post that is in the sheet -> replies with that post's
     GitHub repo + setup guide (Instagram: DM + short public reply; YouTube: public reply),
  4. writes every comment it handled to the Log tab (the Log is also its memory:
     a comment already in the Log is never handled twice, and one person gets
     one reply per post).

Secrets (GitHub > Settings > Secrets and variables > Actions):
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, SHEETS_REFRESH_TOKEN   (same as autopost)
  IG_ACCESS_TOKEN                                                (same as autopost)
  YOUTUBE_COMMENTS_REFRESH_TOKEN  (scope youtube.force-ssl; falls back to YOUTUBE_REFRESH_TOKEN)
Variables: COMMENT_SHEET_ID (default below), COMMENTS_PAUSED=true to pause.
"""
from __future__ import annotations

import datetime as dt
import os
import random
import re
import sys
import time
import unicodedata

import requests

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
IG_API = "https://graph.instagram.com/v21.0"
YT_API = "https://www.googleapis.com/youtube/v3"
HTTP_TIMEOUT = 60
DEFAULT_SHEET_ID = "176CJln8W2-mol-2FoBz1OJ786UINrFteR48QjPUO3pA"
LOOKBACK_HOURS = 72
MAX_REPLIES_PER_RUN = int(os.environ.get("MAX_REPLIES_PER_RUN", "20"))
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")

DM_TEXT = os.environ.get("DM_TEXT") or (
    "Hey {name}! Here is the GitHub repo: {repo}\nSetup guide: {guide}\nReply here if you get stuck 🙌")
PUBLIC_TEXT_IG = os.environ.get("PUBLIC_TEXT_IG") or "Sent you the repo + setup guide in your DMs 📩"
PUBLIC_TEXT_YT = os.environ.get("PUBLIC_TEXT_YT") or "Here you go 👉 Repo: {repo} | Setup guide: {guide}"

SPAM = ["http://", "https://", "www.", "crypto", "forex", "follow for follow", "f4f", "check my page",
        "earn money", "free followers", "buy followers", "onlyfans", "promo code", "binance", "whatsapp +"]
ABUSE = ["scam", "scammer", "fraud", "fake", "bullshit", "fuck", "shit", "idiot", "stupid", "chutiya",
         "bhosd", "madarch", "behench", "harami", "bakwas", "ghatiya"]

LOG_HEADERS = ["time", "platform", "post_id", "post_url", "commenter_id", "commenter_name",
               "comment_id", "comment", "action", "reply", "status", "note"]


# ---------------------------------------------------------------- helpers
def log(msg: str) -> None:
    print(f"[{dt.datetime.now(IST):%H:%M:%S IST}] {msg}", flush=True)


def env(name: str, default: str | None = None, required: bool = True) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        if default is not None:
            return default
        if required:
            sys.exit(f"Missing setting: {name} (add it in GitHub > Settings > Secrets and variables > Actions)")
    return val


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().lower()


def hit(text: str, words) -> str | None:
    t = norm(text)
    return next((w for w in words if w and w in t), None)


def google_token(refresh_token: str) -> str:
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": env("GOOGLE_CLIENT_ID"), "client_secret": env("GOOGLE_CLIENT_SECRET"),
        "refresh_token": refresh_token, "grant_type": "refresh_token"}, timeout=HTTP_TIMEOUT)
    if r.status_code != 200:
        sys.exit(f"Google sign-in failed ({r.status_code}): {r.text[:300]}")
    return r.json()["access_token"]


# ---------------------------------------------------------------- sheet
class Sheet:
    def __init__(self, token: str, sheet_id: str):
        self.h = {"Authorization": f"Bearer {token}"}
        self.id = sheet_id
        meta = requests.get(f"{SHEETS}/{sheet_id}?fields=sheets.properties.title", headers=self.h,
                            timeout=HTTP_TIMEOUT)
        if meta.status_code != 200:
            sys.exit(f"Can't open the comment-bot sheet ({meta.status_code}): {meta.text[:300]}")
        self.tabs = [s["properties"]["title"] for s in meta.json().get("sheets", [])]

    def tab(self, wanted: str) -> str | None:
        for t in self.tabs:
            if t.strip().lower() == wanted.lower():
                return t
        return None

    def rows(self, wanted: str) -> list[dict]:
        name = self.tab(wanted)
        if not name:
            return []
        r = requests.get(f"{SHEETS}/{self.id}/values/{requests.utils.quote(name)}!A1:ZZ5000",
                         headers=self.h, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            log(f"can't read tab {name}: {r.text[:200]}")
            return []
        values = r.json().get("values", [])
        header = [c.strip() for c in (values[0] if values else [])]
        return [{h: (raw[j].strip() if j < len(raw) else "") for j, h in enumerate(header)} for raw in values[1:]]

    def append_log(self, row: list) -> None:
        name = self.tab("Log")
        if not name:
            log("no Log tab — creating one")
            requests.post(f"{SHEETS}/{self.id}:batchUpdate", headers=self.h, timeout=HTTP_TIMEOUT,
                          json={"requests": [{"addSheet": {"properties": {"title": "Log"}}}]})
            self.tabs.append("Log")
            name = "Log"
            requests.post(f"{SHEETS}/{self.id}/values/{name}!A1:append?valueInputOption=RAW",
                          headers=self.h, json={"values": [LOG_HEADERS]}, timeout=HTTP_TIMEOUT)
        r = requests.post(f"{SHEETS}/{self.id}/values/{requests.utils.quote(name)}!A1:append?valueInputOption=RAW",
                          headers=self.h, json={"values": [row]}, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            log(f"log append failed: {r.text[:200]}")


def pick(row: dict, *cands: str) -> str:
    keys = list(row.keys())
    for c in cands:  # exact-ish first
        for k in keys:
            if re.sub(r"[^a-z]", "", k.lower()) == c and str(row[k]).strip():
                return str(row[k]).strip()
    for c in cands:
        for k in keys:
            if c in k.lower().replace(" ", "") and str(row[k]).strip():
                return str(row[k]).strip()
    return ""


def url_key(u: str) -> str:
    u = u or ""
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{10,12})", u)
    if m:
        return "yt:" + m.group(1)
    m = re.search(r"instagram\.com/(?:[^/]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", u)
    if m:
        return "ig:" + m.group(1)
    return ""


def load_posts(sheet: Sheet) -> dict:
    """Return {key -> {repo, guide, no}} where key is yt:<videoId> or ig:<shortcode>."""
    master = {}
    for r in sheet.rows("Sheet1"):
        no = pick(r, "no", "reponumber", "number", "sno").rstrip(".0") or pick(r, "no")
        repo, guide = pick(r, "githubrepo", "github", "repo"), pick(r, "usersetupguide", "setupguide", "guide")
        if no and (repo or guide):
            master[no] = {"repo": repo, "guide": guide}
    out = {}
    for tab in ("youtube", "Instagram", "instagram", "facebook"):
        for r in sheet.rows(tab):
            no = pick(r, "no", "reponumber", "number", "sno")
            no = re.sub(r"\.0$", "", no)
            url = pick(r, "postedvideourl", "postedvideolink", "videourl", "posturl", "url", "link")
            k = url_key(url)
            if not k:
                continue
            base = dict(master.get(no, {}))
            repo, guide = pick(r, "githubrepo", "github", "repo"), pick(r, "usersetupguide", "setupguide", "guide")
            if repo:
                base["repo"] = repo
            if guide:
                base["guide"] = guide
            if base.get("repo") or base.get("guide"):
                base["no"] = no
                out[k] = base
    return out


def load_log(sheet: Sheet):
    seen, replied = set(), set()
    for r in sheet.rows("Log"):
        p, cid, pid, uid, st = (r.get("platform", ""), r.get("comment_id", ""), r.get("post_id", ""),
                                r.get("commenter_id", ""), r.get("status", ""))
        if cid:
            seen.add(f"{p}:{cid}")
        if st in ("sent", "dry-run") and uid:
            replied.add(f"{p}:{pid}:{uid}")
    return seen, replied


# ---------------------------------------------------------------- instagram
def ig(method: str, path: str, token: str, **kw):
    kw.setdefault("timeout", HTTP_TIMEOUT)
    params = kw.pop("params", {})
    params["access_token"] = token
    r = requests.request(method, f"{IG_API}/{path}", params=params, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"{method} {path}: {r.status_code} {r.text[:300]}")
    return r.json()


def instagram_comments(token: str):
    me = ig("GET", "me", token, params={"fields": "id,username"})
    my_id, my_name = me["id"], me.get("username", "")
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=LOOKBACK_HOURS)
    fields = ("id,caption,permalink,timestamp,comments.limit(50){id,text,username,timestamp,from,"
              "replies{id,text,username,timestamp,from}}")
    media = ig("GET", "me/media", token, params={"fields": fields, "limit": 15}).get("data", [])
    out = []
    for m in media:
        for c in (m.get("comments") or {}).get("data", []):
            items = [(c, "")] + [(r, c["id"]) for r in (c.get("replies") or {}).get("data", [])]
            for x, parent in items:
                ts = x.get("timestamp")
                if ts and dt.datetime.fromisoformat(ts.replace("+0000", "+00:00")) < cutoff:
                    continue
                frm = x.get("from") or {}
                uid = frm.get("id") or x.get("username") or ""
                uname = frm.get("username") or x.get("username") or ""
                out.append({"platform": "instagram", "comment_id": x["id"], "post_id": m["id"],
                            "post_url": m.get("permalink", ""), "key": url_key(m.get("permalink", "")),
                            "user_id": uid, "user_name": uname, "text": x.get("text", ""),
                            "is_own": uid == my_id or uname == my_name})
    return my_id, out


def instagram_reply(token: str, my_id: str, c: dict, public: str, dm: str) -> str:
    notes = []
    try:
        ig("POST", f"{my_id}/messages", token, json={"recipient": {"comment_id": c["comment_id"]},
                                                     "message": {"text": dm}})
        notes.append("dm:ok")
    except Exception as e:
        notes.append(f"dm:FAIL {e}")
    try:
        ig("POST", f"{c['comment_id']}/replies", token, data={"message": public})
        notes.append("public:ok")
    except Exception as e:
        notes.append(f"public:FAIL {e}")
    return " | ".join(notes)


# ---------------------------------------------------------------- youtube
def yt(method: str, path: str, token: str, **kw):
    kw.setdefault("timeout", HTTP_TIMEOUT)
    r = requests.request(method, f"{YT_API}/{path}", headers={"Authorization": f"Bearer {token}"}, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"{method} {path}: {r.status_code} {r.text[:300]}")
    return r.json()


def youtube_comments(token: str):
    ch = yt("GET", "channels", token, params={"part": "id,contentDetails", "mine": "true"})["items"][0]
    my_id = ch["id"]
    uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    vids = [(i["snippet"]["resourceId"]["videoId"], i["snippet"]["title"]) for i in
            yt("GET", "playlistItems", token, params={"part": "snippet", "playlistId": uploads, "maxResults": 10})["items"]]
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=LOOKBACK_HOURS)
    out = []
    for vid, title in vids:
        try:
            threads = yt("GET", "commentThreads", token, params={"part": "snippet,replies", "videoId": vid,
                                                                  "order": "time", "maxResults": 50,
                                                                  "textFormat": "plainText"})["items"]
        except Exception as e:
            log(f"youtube comments failed for {vid}: {e}")
            continue
        for t in threads:
            top = t["snippet"]["topLevelComment"]
            items = [(top, top["id"])] + [(r, top["id"]) for r in (t.get("replies") or {}).get("comments", [])]
            for x, parent in items:
                s = x["snippet"]
                if dt.datetime.fromisoformat(s["publishedAt"].replace("Z", "+00:00")) < cutoff:
                    continue
                uid = (s.get("authorChannelId") or {}).get("value") or s.get("authorDisplayName", "")
                out.append({"platform": "youtube", "comment_id": x["id"], "post_id": vid,
                            "post_url": f"https://www.youtube.com/watch?v={vid}", "key": "yt:" + vid,
                            "user_id": uid, "user_name": s.get("authorDisplayName", ""),
                            "text": s.get("textOriginal") or s.get("textDisplay", ""), "parent": parent,
                            "is_own": uid == my_id})
    return my_id, out


def youtube_reply(token: str, c: dict, text: str) -> str:
    try:
        yt("POST", "comments", token, params={"part": "snippet"},
           json={"snippet": {"parentId": c["parent"], "textOriginal": text}})
        return "public:ok"
    except Exception as e:
        return f"public:FAIL {e}"


# ---------------------------------------------------------------- main
def main() -> None:
    if os.environ.get("COMMENTS_PAUSED", "").lower() == "true":
        log("COMMENTS_PAUSED=true — nothing to do")
        return
    sheet_id = env("COMMENT_SHEET_ID", DEFAULT_SHEET_ID)
    sheet = Sheet(google_token(env("SHEETS_REFRESH_TOKEN")), sheet_id)
    posts = load_posts(sheet)
    seen, replied = load_log(sheet)
    log(f"sheet: {len(posts)} posts with repo/guide, {len(seen)} comments already in Log, dry_run={DRY_RUN}")

    sources = []
    ig_token = os.environ.get("IG_ACCESS_TOKEN", "").strip()
    if ig_token:
        try:
            my_ig, items = instagram_comments(ig_token)
            sources.append(("instagram", my_ig, items))
            log(f"instagram: {len(items)} recent comments")
        except Exception as e:
            log(f"instagram failed: {e}")
    yt_refresh = os.environ.get("YOUTUBE_COMMENTS_REFRESH_TOKEN", "").strip() or os.environ.get("YOUTUBE_REFRESH_TOKEN", "").strip()
    yt_token = None
    if yt_refresh:
        try:
            yt_token = google_token(yt_refresh)
            my_yt, items = youtube_comments(yt_token)
            sources.append(("youtube", my_yt, items))
            log(f"youtube: {len(items)} recent comments")
        except Exception as e:
            log(f"youtube failed (needs a refresh token with youtube.force-ssl scope): {e}")

    sent = 0
    for platform, my_id, items in sources:
        for c in items:
            key = f"{platform}:{c['comment_id']}"
            if key in seen or c["is_own"] or not c["text"].strip():
                continue
            if sent >= MAX_REPLIES_PER_RUN:
                break
            seen.add(key)
            now = dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
            base = [now, platform, c["post_id"], c["post_url"], c["user_id"], c["user_name"], c["comment_id"],
                    c["text"][:500]]
            rk = f"{platform}:{c['post_id']}:{c['user_id']}"
            if rk in replied:
                sheet.append_log(base + ["skip", "", "skipped", "already replied to this person on this post"])
                continue
            w = hit(c["text"], SPAM)
            if w:
                sheet.append_log(base + ["skip-spam", "", "skipped", f"matched: {w}"])
                continue
            w = hit(c["text"], ABUSE)
            if w:
                sheet.append_log(base + ["skip-abuse", "", "skipped", f"matched: {w}"])
                continue
            post = posts.get(c["key"])
            if not post:
                sheet.append_log(base + ["flag-no-post", "", "flagged",
                                         "post not in sheet — add its URL to the platform tab with its repo no."])
                continue
            fill = lambda s: (s.replace("{name}", c["user_name"] or "").replace("{repo}", post.get("repo", ""))
                              .replace("{guide}", post.get("guide", "")))
            if DRY_RUN:
                reply = fill(DM_TEXT if platform == "instagram" else PUBLIC_TEXT_YT)
                sheet.append_log(base + ["post-match", reply[:500], "dry-run", "would send"])
                sent += 1
                continue
            time.sleep(random.uniform(3, 12))
            if platform == "instagram":
                note = instagram_reply(ig_token, my_id, c, fill(PUBLIC_TEXT_IG), fill(DM_TEXT))
                reply = fill(DM_TEXT)
            else:
                note = youtube_reply(yt_token, c, fill(PUBLIC_TEXT_YT))
                reply = fill(PUBLIC_TEXT_YT)
            status = "sent" if ":ok" in note else "failed"
            if status == "sent":
                replied.add(rk)
                sent += 1
            sheet.append_log(base + ["post-match", reply[:500], status, note[:300]])
            log(f"{platform} {c['user_name']}: {status} ({note})")
    log(f"done — {sent} replies this run")


if __name__ == "__main__":
    main()
