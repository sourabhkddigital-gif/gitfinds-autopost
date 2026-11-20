"""GitFinds channel stats -> posting sheet, for the GitFinds dashboard.

Writes two tabs in the posting sheet (SHEET_ID):
  "Channel stats"  one row per platform per run: followers, total views, posts (kept as history)
  "Video stats"    latest numbers for each recent video on YouTube and Instagram (rewritten each run)

YouTube needs a key that may read the channel: YOUTUBE_COMMENTS_REFRESH_TOKEN (youtube.force-ssl)
is tried first, then YOUTUBE_REFRESH_TOKEN. Instagram uses IG_ACCESS_TOKEN; views need the
insights permission and stay empty without it. A platform that fails is noted, the other still runs.
"""

from __future__ import annotations

import datetime as dt
import os
import sys

import requests

import autopost as ap
import comment_bot as cb

YT = "https://www.googleapis.com/youtube/v3"
CH_TAB, VID_TAB = "Channel stats", "Video stats"
CH_HEAD = ["Time (IST)", "Platform", "Followers", "Total views", "Posts", "Note"]
VID_HEAD = ["Platform", "Title", "Link", "Published", "Views", "Likes", "Comments", "Updated (IST)"]


def now_s() -> str:
    return dt.datetime.now(ap.IST).strftime("%Y-%m-%d %H:%M")


def one_line(t: str, n: int = 90) -> str:
    t = " ".join(str(t or "").split())
    return t[:n] + ("…" if len(t) > n else "")


def youtube():
    tries = []
    if os.environ.get("YOUTUBE_COMMENTS_REFRESH_TOKEN"):
        tries.append(("comments key", os.environ["YOUTUBE_COMMENTS_REFRESH_TOKEN"],
                      os.environ.get("YOUTUBE_COMMENTS_CLIENT_ID", ""), os.environ.get("YOUTUBE_COMMENTS_CLIENT_SECRET", "")))
    if os.environ.get("YOUTUBE_REFRESH_TOKEN"):
        tries.append(("upload key", os.environ["YOUTUBE_REFRESH_TOKEN"], "", ""))
    errors = []
    for name, rt, cid, cs in tries:
        try:
            h = {"Authorization": f"Bearer {cb.google_token(rt, cid, cs)}"}
            r = requests.get(f"{YT}/channels", headers=h, timeout=60,
                             params={"part": "statistics,contentDetails,snippet", "mine": "true"})
            if r.status_code != 200:
                raise RuntimeError(f"YouTube {r.status_code}: {r.text[:200]}")
            ch = r.json()["items"][0]
            st = ch["statistics"]
            uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]
            items = requests.get(f"{YT}/playlistItems", headers=h, timeout=60,
                                 params={"part": "contentDetails", "playlistId": uploads, "maxResults": 50}).json().get("items", [])
            ids = [i["contentDetails"]["videoId"] for i in items]
            vids = []
            if ids:
                vr = requests.get(f"{YT}/videos", headers=h, timeout=60,
                                  params={"part": "statistics,snippet,status", "id": ",".join(ids)}).json().get("items", [])
                for v in vr:
                    if v.get("status", {}).get("privacyStatus") != "public":
                        continue
                    s = v.get("statistics", {})
                    vids.append(["YouTube", one_line(v["snippet"]["title"]), f"https://youtube.com/shorts/{v['id']}",
                                 v["snippet"]["publishedAt"][:10], int(s.get("viewCount", 0)), int(s.get("likeCount", 0)),
                                 int(s.get("commentCount", 0)), now_s()])
            ap.log(f"YouTube stats via {name}: {st.get('subscriberCount')} subscribers, {len(vids)} videos")
            return ["YouTube", int(st.get("subscriberCount", 0)), int(st.get("viewCount", 0)), int(st.get("videoCount", 0)), ""], vids
        except Exception as e:  # try the next key
            errors.append(f"{name}: {e}")
    note = "No YouTube key can read the channel yet (needs the YouTube comments key). " + " | ".join(errors)[:300]
    ap.log(note)
    return ["YouTube", "", "", "", note], []


def instagram():
    tok = os.environ.get("IG_ACCESS_TOKEN", "")
    try:
        me = ap.ig_call("GET", f"{ap.IG_API}/me", tok, params={"fields": "username,followers_count,media_count"})
        media = ap.ig_call("GET", f"{ap.IG_API}/me/media", tok, params={
            "fields": "id,caption,permalink,timestamp,like_count,comments_count,media_type", "limit": 50}).get("data", [])
        vids, total, insights_ok = [], 0, True
        for m in media:
            views = ""
            if insights_ok:
                try:
                    ins = ap.ig_call("GET", f"{ap.IG_API}/{m['id']}/insights", tok, params={"metric": "views"}).get("data", [])
                    views = int(ins[0]["values"][0]["value"]) if ins else ""
                    total += views or 0
                except Exception as e:
                    insights_ok = False
                    ap.log(f"Instagram views not available: {str(e)[:200]}")
            vids.append(["Instagram", one_line((m.get("caption") or "").split("\n")[0]), m.get("permalink", ""),
                         (m.get("timestamp") or "")[:10], views, int(m.get("like_count") or 0),
                         int(m.get("comments_count") or 0), now_s()])
        note = "" if insights_ok else "Views need the Instagram insights permission; likes and comments are shown."
        ap.log(f"Instagram stats: {me.get('followers_count')} followers, {len(vids)} posts")
        return ["Instagram", int(me.get("followers_count") or 0), total if insights_ok else "",
                int(me.get("media_count") or 0), note], vids
    except Exception as e:
        note = f"Instagram stats failed: {str(e)[:250]}"
        ap.log(note)
        return ["Instagram", "", "", "", note], []


def ensure_tab(h: dict, sid: str, title: str, head: list) -> None:
    rng = requests.utils.quote(f"'{title}'!A1:Z1")
    r = requests.get(f"{ap.SHEETS}/{sid}/values/{rng}", headers=h, timeout=60)
    if r.status_code == 400:
        requests.post(f"{ap.SHEETS}/{sid}:batchUpdate", headers=h, timeout=60,
                      json={"requests": [{"addSheet": {"properties": {"title": title}}}]}).raise_for_status()
    if r.status_code != 200 or not r.json().get("values"):
        requests.put(f"{ap.SHEETS}/{sid}/values/{rng}?valueInputOption=RAW", headers=h, timeout=60,
                     json={"values": [head]}).raise_for_status()


def main() -> None:
    sid = ap.env("SHEET_ID")
    h = {"Authorization": f"Bearer {ap.google_token(ap.env('SHEETS_REFRESH_TOKEN'))}"}
    (y_ch, y_v), (i_ch, i_v) = youtube(), instagram()
    ensure_tab(h, sid, CH_TAB, CH_HEAD)
    ensure_tab(h, sid, VID_TAB, VID_HEAD)
    t = now_s()
    requests.post(f"{ap.SHEETS}/{sid}/values/" + requests.utils.quote(f"'{CH_TAB}'!A1") + ":append?valueInputOption=RAW",
                  headers=h, timeout=60, json={"values": [[t] + y_ch, [t] + i_ch]}).raise_for_status()
    rows = sorted(y_v + i_v, key=lambda r: r[3], reverse=True)
    requests.post(f"{ap.SHEETS}/{sid}/values/" + requests.utils.quote(f"'{VID_TAB}'!A2:H1000") + ":clear",
                  headers=h, timeout=60).raise_for_status()
    if rows:
        requests.put(f"{ap.SHEETS}/{sid}/values/" + requests.utils.quote(f"'{VID_TAB}'!A2") + "?valueInputOption=RAW",
                     headers=h, timeout=60, json={"values": rows}).raise_for_status()
    ap.log(f"Wrote {len(rows)} video rows and 2 channel rows.")
    if not y_v and not i_v:
        sys.exit("No stats could be read from YouTube or Instagram.")


if __name__ == "__main__":
    main()
