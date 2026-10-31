"""GitFinds auto-poster.

Runs on GitHub Actions (no PC, no Docker). Every run it:
  1. reads the "YouTube" and "Instagram" tabs of the Google Sheet,
  2. for each platform, if today's post isn't done yet and it's close to the
     posting time, takes the first row with Status = Pending,
  3. downloads the video from Google Drive,
  4. posts it (YouTube: uploaded with publishAt = the exact posting time;
     Instagram: Reel uploaded, then published at the posting time),
  5. marks the row Uploaded + date (+ link if a "Post Link" column exists).

All secrets come from environment variables (GitHub repo secrets).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import time

import requests

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
IG_API = "https://graph.instagram.com/v21.0"
LOOKAHEAD_MIN = 60          # start working this many minutes before posting time
LATEST_IST = dt.time(23, 30)  # don't post after this (wait for tomorrow instead)
HTTP_TIMEOUT = 120


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


def parse_time(text: str) -> dt.time:
    m = re.match(r"^\s*(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?\s*$", text or "", re.I)
    if not m:
        sys.exit(f"Bad time '{text}'. Use 18:00 or 6:30 PM")
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3):
        h = h % 12 + (12 if m.group(3).lower() == "pm" else 0)
    if h > 23 or mi > 59:
        sys.exit(f"Bad time '{text}'")
    return dt.time(h, mi)


def google_token(refresh_token: str) -> str:
    r = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": env("GOOGLE_CLIENT_ID"),
            "client_secret": env("GOOGLE_CLIENT_SECRET"),
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code != 200:
        sys.exit(f"Google sign-in failed ({r.status_code}): {r.text[:300]}\n"
                 "-> The refresh token is wrong or expired. Make a new one (see README).")
    return r.json()["access_token"]


# ---------------------------------------------------------------- sheet
class Tab:
    def __init__(self, token: str, sheet_id: str, name: str):
        self.h = {"Authorization": f"Bearer {token}"}
        self.sheet_id, self.name = sheet_id, name
        r = requests.get(f"{SHEETS}/{sheet_id}/values/" + requests.utils.quote(f"'{name}'!A1:ZZ2000"),
                         headers=self.h, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            sys.exit(f"Can't read tab '{name}' ({r.status_code}): {r.text[:300]}")
        values = r.json().get("values", [])
        self.header = [c.strip() for c in (values[0] if values else [])]
        self.rows = []  # (sheet_row_number, dict)
        for i, raw in enumerate(values[1:], start=2):
            self.rows.append((i, {h: (raw[j].strip() if j < len(raw) else "") for j, h in enumerate(self.header)}))

    def col(self, name: str) -> int | None:
        for j, h in enumerate(self.header):
            if h.lower() == name.lower():
                return j
        return None

    @staticmethod
    def get(row: dict, name: str) -> str:
        for k, v in row.items():
            if k.lower() == name.lower():
                return v
        return ""

    def set(self, row_no: int, updates: dict) -> None:
        data = []
        for name, value in updates.items():
            j = self.col(name)
            if j is None:
                continue
            letter = ""
            n = j + 1
            while n:
                n, rem = divmod(n - 1, 26)
                letter = chr(65 + rem) + letter
            data.append({"range": f"'{self.name}'!{letter}{row_no}", "values": [[value]]})
        if not data:
            return
        last = ""
        for wait in (0, 5, 15, 30, 60):
            time.sleep(wait)
            try:
                r = requests.post(f"{SHEETS}/{self.sheet_id}/values:batchUpdate", headers=self.h,
                                  json={"valueInputOption": "RAW", "data": data}, timeout=HTTP_TIMEOUT)
                if r.status_code == 200:
                    return
                last = r.text[:300]
            except requests.RequestException as e:
                last = str(e)
        raise RuntimeError(f"Could not update sheet '{self.name}' row {row_no} after 5 tries: {last}. "
                           "Mark that row Uploaded by hand so it isn't posted twice.")

    def done_today(self, today: str) -> bool:
        for _, row in self.rows:
            if re.match(r"^(posted|scheduled|uploaded)$", self.get(row, "Status"), re.I) and \
                    self.get(row, "Schedule Date").startswith(today):
                return True
        return False

    def last_post_date(self, today: dt.date) -> dt.date | None:
        """Most recent day a video went out on this platform (from Schedule Date or Date Uploaded)."""
        best = None
        for _, row in self.rows:
            if not re.match(r"^(posted|uploaded)$", self.get(row, "Status"), re.I):
                continue
            day = None
            m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", self.get(row, "Schedule Date"))
            if m:
                day = dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            else:
                try:
                    d = dt.datetime.strptime(f"{self.get(row, 'Date Uploaded')} {today.year}", "%d %b %Y").date()
                    day = d if d <= today else d.replace(year=today.year - 1)
                except ValueError:
                    pass
            if day and day <= today and (best is None or day > best):
                best = day
        return best

    def dated_pending_today(self, today: dt.date) -> bool:
        for _, row in self.rows:
            m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", self.get(row, "Schedule Date"))
            if re.match(r"^pending$", self.get(row, "Status"), re.I) and m and \
                    dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))) == today:
                return True
        return False

    def next_pending(self, today: dt.date):
        """Pending row for today: a row dated today first, else the first undated (or overdue) row.
        Pending rows dated in the future are kept for their own day."""
        undated = None
        for row_no, row in self.rows:
            if not (re.match(r"^pending$", self.get(row, "Status"), re.I) and drive_id(self.get(row, "Video Drive Link"))):
                continue
            m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", self.get(row, "Schedule Date"))
            day = dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None
            if day == today:
                return row_no, row
            if (day is None or day < today) and undated is None:
                undated = (row_no, row)
        return undated


def drive_id(url: str) -> str | None:
    m = re.search(r"/d/([A-Za-z0-9_-]{10,})", url or "") or re.search(r"[?&]id=([A-Za-z0-9_-]{10,})", url or "")
    return m.group(1) if m else None


def download(file_id: str, token: str, dest: str) -> None:
    attempts = [
        (f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&supportsAllDrives=true",
         {"Authorization": f"Bearer {token}"}),
        (f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t", {}),
    ]
    last = ""
    for url, headers in attempts:
        try:
            with requests.get(url, headers=headers, stream=True, timeout=HTTP_TIMEOUT) as r:
                ctype = r.headers.get("content-type", "")
                if r.status_code == 200 and "text/html" not in ctype:
                    with open(dest, "wb") as f:
                        for chunk in r.iter_content(1 << 20):
                            f.write(chunk)
                    size = os.path.getsize(dest)
                    if size > 10_000:
                        log(f"Downloaded video ({size / 1e6:.1f} MB)")
                        return
                last = f"{r.status_code} {ctype} {r.text[:200] if 'text' in ctype else ''}"
        except requests.RequestException as e:
            last = str(e)
    raise RuntimeError(f"Could not download Drive file {file_id}: {last}\n"
                       "-> Check the Drive link and that the Sheets Google account can open the video.")


# ---------------------------------------------------------------- YouTube
def youtube_upload(path: str, row: dict, publish_at: dt.datetime | None, token: str) -> str:
    title = Tab.get(row, "YouTube Title")[:100] or "New video"
    description = "\n\n".join(x for x in [Tab.get(row, "Description"), Tab.get(row, "Hashtags")] if x)[:4900]
    tags, total = [], 0
    for t in (x.strip() for x in Tab.get(row, "YouTube Tags").split(",")):
        if t and total + len(t) + 1 <= 450:
            tags.append(t)
            total += len(t) + 1
    status = {"selfDeclaredMadeForKids": False}
    if publish_at:
        status.update(privacyStatus="private", publishAt=publish_at.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"))
    else:
        status["privacyStatus"] = "public"
    meta = {"snippet": {"title": title, "description": description, "tags": tags, "categoryId": "28"},
            "status": status}
    size = os.path.getsize(path)
    r = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8",
                 "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/*"},
        data=json.dumps(meta), timeout=HTTP_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"YouTube upload start failed ({r.status_code}): {r.text[:500]}")
    upload_url = r.headers["Location"]
    with open(path, "rb") as f:
        r = requests.put(upload_url, data=f, headers={"Content-Type": "video/*", "Content-Length": str(size)},
                         timeout=1800)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"YouTube upload failed ({r.status_code}): {r.text[:500]}")
    vid = r.json()["id"]
    return f"https://youtube.com/shorts/{vid}"


# ---------------------------------------------------------------- Instagram
def ig_call(method: str, url: str, token: str, **kw) -> dict:
    params = kw.pop("params", {}) or {}
    params["access_token"] = token
    r = requests.request(method, url, params=params, timeout=HTTP_TIMEOUT, **kw)
    try:
        data = r.json()
    except ValueError:
        data = {"raw": r.text[:300]}
    if r.status_code >= 400 or "error" in data:
        raise RuntimeError(f"Instagram error ({r.status_code}): {json.dumps(data)[:500]}")
    return data


def instagram_post(path: str, row: dict, post_at: dt.datetime, token: str, fallback_url: str,
                   publish: bool = True) -> str:
    me = ig_call("GET", f"{IG_API}/me", token, params={"fields": "user_id,username"})
    uid = me.get("user_id") or me["id"]
    log(f"Instagram account: @{me.get('username')}")
    caption = "\n\n".join(x for x in [Tab.get(row, "Instagram Title"), Tab.get(row, "Instagram Hashtags")] if x)[:2200]
    try:
        recent = ig_call("GET", f"{IG_API}/me/media", token, params={"fields": "caption,permalink,timestamp", "limit": 10})
        for m in recent.get("data", []):
            if caption[:60] and (m.get("caption") or "")[:60] == caption[:60]:
                log("Instagram: this video is already on the profile - not posting it twice")
                return m.get("permalink", "")
    except RuntimeError as e:
        log(f"(couldn't check recent posts: {e})")

    try:  # 1) Instagram fetches the video from the Drive link (works with Instagram-login keys)
        container = ig_call("POST", f"{IG_API}/{uid}/media", token,
                            params={"media_type": "REELS", "video_url": fallback_url, "caption": caption})["id"]
    except RuntimeError as e:  # 2) fallback: upload the file itself
        log(f"Drive-link method didn't work ({e}); trying direct upload")
        c = ig_call("POST", f"{IG_API}/{uid}/media", token,
                    params={"media_type": "REELS", "upload_type": "resumable", "caption": caption})
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            r = requests.post(c.get("uri") or f"https://rupload.facebook.com/ig-api-upload/v21.0/{c['id']}",
                              headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)},
                              data=f, timeout=1800)
        if r.status_code != 200:
            raise RuntimeError(f"Instagram upload failed {r.status_code}: {r.text[:300]}")
        container = c["id"]

    for _ in range(80):  # wait until Instagram finished processing (max ~20 min)
        s = ig_call("GET", f"{IG_API}/{container}", token, params={"fields": "status_code,status"})
        if s.get("status_code") == "FINISHED":
            break
        if s.get("status_code") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram couldn't process the video: {s}")
        time.sleep(15)
    else:
        raise RuntimeError("Instagram is still processing the video after 20 minutes")

    if not publish:
        return "REHEARSAL OK - video uploaded and processed by Instagram, NOT published"
    wait = (post_at - dt.datetime.now(IST)).total_seconds()
    if wait > 0:
        log(f"Video ready. Waiting {wait / 60:.0f} min to publish at {post_at:%H:%M} IST")
        time.sleep(wait)
    pub = ig_call("POST", f"{IG_API}/{uid}/media_publish", token, params={"creation_id": container})
    try:
        link = ig_call("GET", f"{IG_API}/{pub['id']}", token, params={"fields": "permalink"}).get("permalink", "")
    except RuntimeError:
        link = ""
    return link or f"instagram media {pub['id']}"


def refresh_instagram_token(token: str) -> None:
    """Instagram tokens last 60 days; refreshing gives a fresh 60 days."""
    if not os.environ.get("GH_PAT"):
        log("Instagram token refresh skipped (no GH_PAT secret). Remember to renew the token every ~50 days.")
        return
    try:
        new = ig_call("GET", "https://graph.instagram.com/refresh_access_token", token,
                      params={"grant_type": "ig_refresh_token"})["access_token"]
    except RuntimeError as e:
        raise RuntimeError(f"Instagram key renewal failed: {e}")
    r = subprocess.run(["gh", "secret", "set", "IG_ACCESS_TOKEN", "--body", new, "--repo",
                        os.environ["GITHUB_REPOSITORY"]],
                       env={**os.environ, "GH_TOKEN": os.environ["GH_PAT"]}, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"Couldn't save the renewed Instagram key: {r.stderr[:300]}")
    log("Instagram key renewed and saved (valid for another 60 days)")


# ---------------------------------------------------------------- repo sheet ("repo shared in zoom group")
REPO_SHEET_DEFAULT = "176CJln8W2-mol-2FoBz1OJ786UINrFteR48QjPUO3pA"
GENERIC = {"ai", "the", "and", "for", "with", "tool", "tools", "repo", "repos", "github", "free", "open", "source", "skill"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def match_repo(text: str, repos: list) -> tuple | None:
    """repos: [(sheet_row, repo_url)]. Finds the repo a video is about from its title/description/tags."""
    t = _norm(text)
    low = (text or "").lower()
    name = lambda url: url.strip().rstrip("/").split("/")[-1]

    def whole(n: str) -> bool:  # repo name as a whole word, e.g. "awesome-agents" / "Awesome AITools"
        parts = [re.escape(x) for x in re.split(r"[-_.]", n.lower()) if x]
        return bool(parts) and re.search(r"(?<![a-z0-9])" + r"[\s\-_.]*".join(parts) + r"(?![a-z0-9])", low) is not None

    exact = [r for r in repos if len(_norm(name(r[1]))) > 2 and whole(name(r[1]))]
    if exact:
        return max(exact, key=lambda r: len(name(r[1])))
    scored = []
    for r in repos:
        toks = [x for x in re.split(r"[-_.]", name(r[1]).lower()) if x and x not in GENERIC and (len(x) >= 3)]
        if not toks:
            continue
        hit = sum(1 for x in toks if x[:5] in t)
        if hit and (hit / len(toks) >= 0.6) and (hit >= 2 or (len(toks) == 1 and len(toks[0]) >= 5)):
            scored.append((hit / len(toks), hit, r))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    if scored and (len(scored) == 1 or scored[0][:2] != scored[1][:2]):
        return scored[0][2]
    return None


def record_link(sheet_token: str, platform: str, row: dict, link: str, write: bool = True) -> None:
    """Puts the posted link in the repo sheet's youtube/Instagram tab, next to the repo the video is about."""
    try:
        sid = env("REPO_SHEET_ID", REPO_SHEET_DEFAULT, required=False)
        meta = requests.get(f"{SHEETS}/{sid}?fields=sheets.properties.title",
                            headers={"Authorization": f"Bearer {sheet_token}"}, timeout=HTTP_TIMEOUT).json()
        title = next((x["properties"]["title"] for x in meta.get("sheets", [])
                      if x["properties"]["title"].strip().lower() == platform.lower()), None)
        if not title:
            log(f"WARNING: repo sheet has no '{platform}' tab - link not recorded")
            return
        tab = Tab(sheet_token, sid, title)
        link_col = next((h for h in tab.header if "posted video" in h.lower()), None)
        if not link_col:
            log(f"WARNING: no 'posted video' column in repo sheet tab '{title}'")
            return
        repos = [(rn, Tab.get(r, "github repo")) for rn, r in tab.rows if Tab.get(r, "github repo")]
        wanted = Tab.get(row, "Repo No.") or Tab.get(row, "Repo No")
        hit = None
        if wanted:
            hit = next(((rn, u) for rn, r in tab.rows for u in [Tab.get(r, "github repo")]
                        if Tab.get(r, "no.").strip() == wanted.strip()), None)
        if not hit:
            text = " ".join(v for k, v in row.items() if "drive" not in k.lower())
            hit = match_repo(text, repos)
        if not hit:
            log(f"WARNING: couldn't tell which repo this {platform} video is about - add a 'Repo No.' "
                f"column to the posting sheet, or paste {link} into the repo sheet yourself")
            return
        if not write:
            log(f"{platform}: link would be saved in repo sheet tab '{title.strip()}' row {hit[0]} ({hit[1].strip()})")
            return
        tab.set(hit[0], {link_col: link})
        log(f"{platform}: link saved in repo sheet next to {hit[1].strip()}")
    except Exception as e:  # never let this break posting
        log(f"WARNING: couldn't save link to repo sheet: {e}")


# ---------------------------------------------------------------- main
def health_check(name: str, tab: "Tab", now: dt.datetime, sheet_token: str) -> None:
    """Daily check on in-between days, so a broken key is found before the next posting day."""
    if name == "YouTube":
        google_token(env("YOUTUBE_REFRESH_TOKEN"))
    else:
        ig_call("GET", f"{IG_API}/me", env("IG_ACCESS_TOKEN"), params={"fields": "user_id,username"})
    if not tab.next_pending(now.date() + dt.timedelta(days=2)) and not tab.next_pending(now.date()):
        raise RuntimeError(f"No 'Pending' videos left in the '{tab.name}' tab for the next post. Add new rows.")
    log(f"{name}: health check OK (key works, next video is ready)")


def run_platform(name: str, tab_name: str, post_time: dt.time, force: bool, dry: bool,
                 sheet_token: str) -> bool:
    now = dt.datetime.now(IST)
    today = now.strftime("%d/%m/%Y")
    target = dt.datetime.combine(now.date(), post_time, tzinfo=IST)
    log(f"--- {name}: posting time {post_time:%H:%M} IST")

    tab = Tab(sheet_token, env("SHEET_ID"), tab_name)
    if tab.done_today(today) and not force and not dry:
        log(f"{name}: today's video is already done. Nothing to do.")
        return True
    every = max(1, int(env("POST_EVERY_DAYS", "2", required=False) or 2))
    last = tab.last_post_date(now.date())
    if not force and last and (now.date() - last).days < every and not tab.dated_pending_today(now.date()):
        nxt = last + dt.timedelta(days=every)
        log(f"{name}: posts every {every} days. Last post {last:%d %b}, so no post today. "
            f"Next post: {nxt:%a %d %b} at {post_time:%H:%M}.")
        if now.hour == 12 or dry:
            health_check(name, tab, now, sheet_token)
        return True
    if not force and not dry:
        if now < target - dt.timedelta(minutes=LOOKAHEAD_MIN):
            log(f"{name}: too early, will post at {post_time:%H:%M}. Nothing to do now.")
            return True
        if now.time() > LATEST_IST:
            log(f"{name}: too late for today. Will post tomorrow.")
            return True
    pick = tab.next_pending(now.date())
    if not pick:
        raise RuntimeError(f"No 'Pending' videos left in the '{tab_name}' tab, so nothing was posted today. "
                           "Add new rows with Status Pending.")
    row_no, row = pick
    title = Tab.get(row, "YouTube Title") or Tab.get(row, "Instagram Title")
    log(f"{name}: picked row {row_no}: {title[:70]}")
    if dry:
        if name == "Instagram":
            me = ig_call("GET", f"{IG_API}/me", env("IG_ACCESS_TOKEN"), params={"fields": "user_id,username"})
            log(f"Instagram: key works for @{me.get('username')}")
        else:
            yt = google_token(env("YOUTUBE_REFRESH_TOKEN"))
            r = requests.get("https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true",
                             headers={"Authorization": f"Bearer {yt}"}, timeout=HTTP_TIMEOUT)
            if r.status_code == 200 and r.json().get("items"):
                log(f"YouTube: key works for channel '{r.json()['items'][0]['snippet']['title']}'")
            else:
                log(f"YouTube: key accepted (channel name not readable with upload-only permission: {r.status_code})")
        record_link(sheet_token, name, row, "(dry-run)", write=False)
        log(f"{name}: DRY RUN - would post now. Nothing posted.")
        return True

    post_at = max(target, dt.datetime.now(IST)) if not force else dt.datetime.now(IST)
    fid = drive_id(Tab.get(row, "Video Drive Link"))
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "video.mp4")
        download(fid, sheet_token, path)
        if name == "YouTube":
            yt_token = google_token(env("YOUTUBE_REFRESH_TOKEN"))
            future = post_at - dt.datetime.now(IST) > dt.timedelta(minutes=10)
            link = youtube_upload(path, row, post_at if future else None, yt_token)
            log(f"YouTube: uploaded {link} " + (f"(goes public at {post_at:%H:%M} IST)" if future else "(public now)"))
        else:
            link = instagram_post(path, row, post_at, env("IG_ACCESS_TOKEN"),
                                  f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t")
            log(f"Instagram: published {link}")
    tab.set(row_no, {"Status": "Uploaded", "Schedule Date": f"{post_at:%d/%m/%Y %H:%M}",
                     "Date Uploaded": f"{post_at.day} {post_at:%b}", "Post Link": link})
    if link.startswith("http"):
        record_link(sheet_token, name, row, link)
    return True


def _row_day(row: dict, today: dt.date) -> dt.date | None:
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", Tab.get(row, "Schedule Date"))
    if m:
        return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    try:
        d = dt.datetime.strptime(f"{Tab.get(row, 'Date Uploaded')} {today.year}", "%d %b %Y").date()
        return d if d <= today else d.replace(year=today.year - 1)
    except ValueError:
        return None


def latest_youtube_post(yt: "Tab", today: dt.date):
    """(day, row) of the video YouTube published most recently (up to today)."""
    best = None
    for _, row in yt.rows:
        if not re.match(r"^(posted|uploaded)$", Tab.get(row, "Status"), re.I):
            continue
        day = _row_day(row, today)
        if day and day <= today and (best is None or day >= best[0]):
            best = (day, row)
    return best


def run_instagram_follow(post_time: dt.time, force: bool, dry: bool, sheet_token: str) -> bool:
    """Instagram follows YouTube: the day after YouTube publishes a video, Instagram posts that same video."""
    now = dt.datetime.now(IST)
    target = dt.datetime.combine(now.date(), post_time, tzinfo=IST)
    log(f"--- Instagram (follows YouTube): posting time {post_time:%H:%M} IST")
    yt = Tab(sheet_token, env("SHEET_ID"), "YouTube")
    ig = Tab(sheet_token, env("SHEET_ID"), "Instagram")
    latest = latest_youtube_post(yt, now.date())
    if not latest:
        log("Instagram: YouTube hasn't published anything yet. Nothing to do.")
        return True
    yt_day, yt_row = latest
    fid = drive_id(Tab.get(yt_row, "Video Drive Link"))
    yt_title = Tab.get(yt_row, "YouTube Title")
    log(f"Instagram: YouTube's latest video ({yt_day:%d %b}): {yt_title[:70]}")
    if not fid:
        raise RuntimeError("YouTube's latest row has no Video Drive Link, so Instagram can't post it.")

    # the same video in the Instagram tab (same Drive file, else same Repo No.)
    repo_no = (Tab.get(yt_row, "Repo No.") or Tab.get(yt_row, "Repo No")).strip()
    match = next(((rn, r) for rn, r in ig.rows if drive_id(Tab.get(r, "Video Drive Link")) == fid), None)
    if not match and repo_no:
        match = next(((rn, r) for rn, r in ig.rows
                      if (Tab.get(r, "Repo No.") or Tab.get(r, "Repo No")).strip() == repo_no), None)
    if match and re.match(r"^(posted|uploaded|scheduled)$", Tab.get(match[1], "Status"), re.I):
        log(f"Instagram: this video is already on Instagram (row {match[0]}). Nothing to do.")
        return True
    if yt_day >= now.date() and not force:
        log("Instagram: YouTube published it today, so Instagram posts it tomorrow.")
        return True
    if not force and not dry:
        if now < target - dt.timedelta(minutes=LOOKAHEAD_MIN):
            log(f"Instagram: too early, will post at {post_time:%H:%M}. Nothing to do now.")
            return True
        if now.time() > LATEST_IST:
            log("Instagram: too late for today. Will post tomorrow.")
            return True

    if match:
        row_no, row = match
        log(f"Instagram: using Instagram tab row {row_no} for the caption")
    else:
        row_no, row = None, dict(yt_row)
        if not Tab.get(row, "Instagram Title"):
            row["Instagram Title"] = yt_title
        if not Tab.get(row, "Instagram Hashtags"):
            row["Instagram Hashtags"] = Tab.get(yt_row, "Hashtags")
        log("Instagram: video not found in the Instagram tab - using the YouTube title and hashtags as caption")
    if dry:
        ig_call("GET", f"{IG_API}/me", env("IG_ACCESS_TOKEN"), params={"fields": "user_id,username"})
        log("Instagram: DRY RUN - would post this video now. Nothing posted.")
        return True

    post_at = max(target, dt.datetime.now(IST)) if not force else dt.datetime.now(IST)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "video.mp4")
        download(fid, sheet_token, path)
        link = instagram_post(path, row, post_at, env("IG_ACCESS_TOKEN"),
                              f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t")
    log(f"Instagram: published {link}")
    if row_no:
        ig.set(row_no, {"Status": "Uploaded", "Schedule Date": f"{post_at:%d/%m/%Y %H:%M}",
                        "Date Uploaded": f"{post_at.day} {post_at:%b}", "Post Link": link})
    if link.startswith("http"):
        record_link(sheet_token, "Instagram", row, link)
    return True


def rehearsal(sheet_token: str) -> None:
    """Instagram test: upload today's video exactly like a real post, but don't publish it."""
    now = dt.datetime.now(IST)
    tab = Tab(sheet_token, env("SHEET_ID"), "Instagram")
    pick = tab.next_pending(now.date())
    if not pick:
        sys.exit("No Pending Instagram row for today")
    row_no, row = pick
    log(f"Instagram rehearsal with row {row_no}: {Tab.get(row, 'Instagram Title')[:60]}")
    fid = drive_id(Tab.get(row, "Video Drive Link"))
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "video.mp4")
        download(fid, sheet_token, path)
        result = instagram_post(path, row, now, env("IG_ACCESS_TOKEN"),
                                f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t",
                                publish=False)
    log(f"Instagram: {result}. The sheet was not changed.")


def takeover(sheet_token: str) -> None:
    """One-time switch from Postiz: rows Postiz had *scheduled* for today or later go back to Pending."""
    today = dt.datetime.now(IST).date()
    for tab_name in ("YouTube", "Instagram"):
        tab = Tab(sheet_token, env("SHEET_ID"), tab_name)
        n = 0
        for row_no, row in tab.rows:
            if not re.match(r"^scheduled$", Tab.get(row, "Status"), re.I):
                continue
            m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", Tab.get(row, "Schedule Date"))
            if m and dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))) >= today:
                tab.set(row_no, {"Status": "Pending", "Schedule Date": ""})
                log(f"{tab_name}: row {row_no} back to Pending (was {Tab.get(row, 'Schedule Date')})")
                n += 1
        log(f"{tab_name}: {n} row(s) handed over from Postiz")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", default="both", choices=["both", "youtube", "instagram"])
    ap.add_argument("--mode", default="normal", choices=["normal", "post-now", "dry-run", "takeover", "rehearsal", "test-alert", "renew-ig-key"])
    a = ap.parse_args()

    if a.mode == "renew-ig-key":
        if not os.environ.get("GH_PAT"):
            sys.exit("GH_PAT secret is missing - can't save the renewed Instagram key")
        refresh_instagram_token(env("IG_ACCESS_TOKEN"))
        return
    if a.mode == "test-alert":
        sys.exit("TEST ALERT: this run failed on purpose to check that you get an email. Nothing is wrong.")
    if env("PAUSED", "false", required=False).lower() == "true":
        log("PAUSED is true - not posting anything.")
        return
    force, dry = a.mode == "post-now", a.mode == "dry-run"
    sheet_token = google_token(env("SHEETS_REFRESH_TOKEN"))
    if a.mode == "takeover":
        takeover(sheet_token)
        return
    if a.mode == "rehearsal":
        rehearsal(sheet_token)
        return
    failed = []
    jobs = [("YouTube", "YouTube", "YT_POST_TIME"), ("Instagram", "Instagram", "IG_POST_TIME")]
    for name, tab_name, time_var in jobs:
        if a.platform not in ("both", name.lower()):
            continue
        if env("YT_PAUSED" if name == "YouTube" else "IG_PAUSED", "false", required=False).lower() == "true":
            log(f"{name}: paused ({'YT' if name == 'YouTube' else 'IG'}_PAUSED is true) - not posting.")
            continue
        if name == "Instagram" and not os.environ.get("IG_ACCESS_TOKEN"):
            log("Instagram: IG_ACCESS_TOKEN not set yet - skipping Instagram.")
            continue
        try:
            post_time = parse_time(env(time_var, "18:00", required=False))
            if name == "Instagram" and env("IG_FOLLOW_YOUTUBE", "true", required=False).lower() != "false":
                run_instagram_follow(post_time, force, dry, sheet_token)
            else:
                run_platform(name, tab_name, post_time, force, dry, sheet_token)
        except Exception as e:  # keep going with the other platform
            log(f"ERROR on {name}: {e}")
            failed.append(name)

    now = dt.datetime.now(IST)
    if os.environ.get("IG_ACCESS_TOKEN") and now.weekday() == 0 and now.hour == 8 and not dry and a.platform in ("both", "instagram"):
        refresh_instagram_token(os.environ["IG_ACCESS_TOKEN"])
    if failed:
        sys.exit(f"Failed: {', '.join(failed)} (GitHub will email you about this run)")


if __name__ == "__main__":
    main()
