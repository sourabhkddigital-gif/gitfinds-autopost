"""GitFinds daily posting report.

Runs every evening after the 6 PM post. For YouTube and Instagram it:
  - checks the posting sheet: was today's video due, and was it posted?
  - checks the platform itself that the post is really live,
  - lists the videos still waiting, with the date each one will go out,
  - warns when videos are about to run out.
The report is posted as a comment on one GitHub issue ("Daily posting report"), so
GitHub emails / notifies the owner, and also shown on the run's summary page.
Optional: set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID secrets to also get it on Telegram.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys

import requests

import autopost as ap
from autopost import IST, Tab, env

REPORT_ISSUE_TITLE = "📋 Daily posting report"
LOW_STOCK = 3  # warn when this many or fewer videos are left


def row_date(row: dict) -> dt.date | None:
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", Tab.get(row, "Schedule Date"))
    return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None


def title_of(row: dict) -> str:
    t = Tab.get(row, "YouTube Title") or Tab.get(row, "Instagram Title")
    t = re.sub(r"\s+", " ", t).strip()
    return (t[:70] + "…") if len(t) > 70 else t


def is_status(row: dict, *names: str) -> bool:
    return bool(re.match(rf"^({'|'.join(names)})$", Tab.get(row, "Status"), re.I))


def plan(tab: Tab, today: dt.date, every: int, today_done: bool, today_open: bool) -> list:
    """Same rules as autopost.py: a Pending row dated for a day posts that day; otherwise the
    next undated Pending row posts once `every` days have passed since the last post."""
    last = tab.last_post_date(today)
    pending = [(rn, r) for rn, r in tab.rows
               if is_status(r, "pending") and ap.drive_id(Tab.get(r, "Video Drive Link"))]
    out, d = [], today
    cur_last = last or (today - dt.timedelta(days=every))
    while pending and d <= today + dt.timedelta(days=366):
        if d == today and (today_done or not today_open):
            if today_done:
                cur_last = today
            d += dt.timedelta(days=1)
            continue
        dated = next(((rn, r) for rn, r in pending if row_date(r) == d), None)
        pick = dated
        if not pick and (d - cur_last).days >= every:
            pick = next(((rn, r) for rn, r in pending if not row_date(r) or row_date(r) < d), None)
        if pick:
            out.append((d, pick[0], pick[1]))
            pending.remove(pick)
            cur_last = d
        d += dt.timedelta(days=1)
    return out


def youtube_live(link: str) -> bool | None:
    if "youtu" not in link:
        return None
    try:
        r = requests.get("https://www.youtube.com/oembed", params={"url": link, "format": "json"}, timeout=30)
        return r.status_code == 200
    except requests.RequestException:
        return None


def instagram_recent() -> list:
    tok = os.environ.get("IG_ACCESS_TOKEN")
    if not tok:
        return []
    try:
        return ap.ig_call("GET", f"{ap.IG_API}/me/media", tok,
                          params={"fields": "caption,permalink,timestamp", "limit": 15}).get("data", [])
    except RuntimeError as e:
        ap.log(f"couldn't read Instagram posts: {e}")
        return []


def find_link(sheet_token: str, platform: str, row: dict) -> str:
    """The link saved by the poster: posting sheet 'Post Link' column, else the repo sheet."""
    link = Tab.get(row, "Post Link")
    if link.startswith("http"):
        return link
    try:
        sid = env("REPO_SHEET_ID", ap.REPO_SHEET_DEFAULT, required=False)
        meta = requests.get(f"{ap.SHEETS}/{sid}?fields=sheets.properties.title",
                            headers={"Authorization": f"Bearer {sheet_token}"}, timeout=60).json()
        title = next((x["properties"]["title"] for x in meta.get("sheets", [])
                      if x["properties"]["title"].strip().lower() == platform.lower()), None)
        if not title:
            return ""
        rt = Tab(sheet_token, sid, title)
        col = next((h for h in rt.header if "posted video" in h.lower()), None)
        repos = [(rn, Tab.get(r, "github repo")) for rn, r in rt.rows if Tab.get(r, "github repo")]
        hit = ap.match_repo(" ".join(v for k, v in row.items() if "drive" not in k.lower()), repos)
        if hit and col:
            return next((Tab.get(r, col) for rn, r in rt.rows if rn == hit[0]), "")
    except Exception as e:  # report must never crash on this
        ap.log(f"couldn't look up link: {e}")
    return ""


def platform_report(name: str, tab_name: str, time_var: str, sheet_token: str, now: dt.datetime,
                    ig_media: list) -> tuple[list, list]:
    today = now.date()
    today_s = now.strftime("%d/%m/%Y")
    every = max(1, int(env("POST_EVERY_DAYS", "2", required=False) or 2))
    post_time = ap.parse_time(env(time_var, "18:00", required=False))
    post_at = dt.datetime.combine(today, post_time, tzinfo=IST)
    paused = env("PAUSED", "false", required=False).lower() == "true" or \
        env("YT_PAUSED" if name == "YouTube" else "IG_PAUSED", "false", required=False).lower() == "true"
    tab = Tab(sheet_token, env("SHEET_ID"), tab_name)
    lines, problems = [f"### {'▶️' if name == 'YouTube' else '📸'} {name}"], []

    done_rows = [(rn, r) for rn, r in tab.rows
                 if is_status(r, "uploaded", "posted") and Tab.get(r, "Schedule Date").startswith(today_s)]
    last = tab.last_post_date(today)
    due = tab.dated_pending_today(today) or (last is None) or ((today - last).days >= every)
    still_open = now.time() < ap.LATEST_IST

    if paused:
        lines.append("⏸️ **Paused** (PAUSED / " + ("YT_PAUSED" if name == "YouTube" else "IG_PAUSED") + " is true). Nothing will post.")
    if done_rows:
        for rn, r in done_rows:
            link = find_link(sheet_token, name, r)
            live = None
            if name == "YouTube" and link:
                live = youtube_live(link) if now >= post_at else None
            elif name == "Instagram":
                cap = (Tab.get(r, "Instagram Title") or "")[:60]
                m = next((x for x in ig_media if cap and (x.get("caption") or "")[:60] == cap), None)
                live = bool(m)
                link = link or (m or {}).get("permalink", "")
            status = {True: "✅ live", False: "⚠️ marked uploaded but **not found live**", None: "✅ uploaded"}[live]
            if live is False:
                problems.append(f"{name}: row {rn} is marked Uploaded but isn't live on {name}")
            lines.append(f"**Today:** {status} — row {rn}: {title_of(r)}" + (f"  \n{link}" if link else ""))
    elif due and not paused:
        if now < post_at:
            lines.append(f"**Today:** ⏳ due at {post_time:%H:%M}, not posted yet")
        else:
            lines.append(f"**Today:** ❌ **due but NOT posted yet** (it keeps retrying every hour until 11:30 PM)")
            problems.append(f"{name}: today's video was due at {post_time:%H:%M} but isn't posted yet")
    else:
        nxt = (last + dt.timedelta(days=every)) if last else today
        lines.append(f"**Today:** no post due (posts every {every} days; last post {last:%d %b}" +
                     (f", next {nxt:%a %d %b})" if last else ")"))

    schedule = plan(tab, today, every, bool(done_rows), still_open and not paused)
    pending_total = sum(1 for _, r in tab.rows if is_status(r, "pending"))
    if schedule:
        lines.append(f"\n**Still to upload: {len(schedule)}**")
        lines.append("| Date | Row | Video |\n|---|---|---|")
        for d, rn, r in schedule:
            lines.append(f"| {d:%a %d %b} | {rn} | {title_of(r)} |")
        if len(schedule) <= LOW_STOCK:
            lines.append(f"\n⚠️ Only **{len(schedule)}** video(s) left — the last one goes out on "
                         f"**{schedule[-1][0]:%a %d %b}**. Add new rows with Status Pending.")
            problems.append(f"{name}: only {len(schedule)} video(s) left (until {schedule[-1][0]:%d %b})")
    else:
        lines.append("\n❗ **No videos waiting.** Add new rows with Status Pending, or nothing will post.")
        problems.append(f"{name}: no Pending videos left")
    if pending_total > len(schedule):
        lines.append(f"\n_({pending_total - len(schedule)} Pending row(s) have no usable Drive link and will be skipped.)_")
        problems.append(f"{name}: {pending_total - len(schedule)} Pending row(s) without a usable Drive link")

    posted_all = sorted(((row_date(r) or dt.date.min, rn, r) for rn, r in tab.rows if is_status(r, "uploaded", "posted")),
                        key=lambda x: x[0], reverse=True)[:3]
    if posted_all:
        lines.append("\n**Last uploads:** " + "; ".join(
            f"{d:%d %b} – row {rn}" if d != dt.date.min else f"row {rn}" for d, rn, _ in posted_all))
    return lines, problems


def latest_run_failures() -> list:
    """Any posting workflow whose most recent run today failed."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        return []
    out = []
    for wf, label in (("youtube.yml", "YouTube"), ("instagram.yml", "Instagram")):
        try:
            r = requests.get(f"https://api.github.com/repos/{repo}/actions/workflows/{wf}/runs",
                             params={"per_page": 1},
                             headers={"Authorization": f"Bearer {os.environ.get('GITHUB_TOKEN', '')}"}, timeout=30)
            run = (r.json().get("workflow_runs") or [None])[0]
            if run and run.get("conclusion") == "failure":
                out.append(f"{label}: the latest automation run failed — {run.get('html_url')}")
        except requests.RequestException:
            pass
    return out


def send(report: str, ok: bool) -> None:
    # 1) run summary page
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(report + "\n")
    # 2) one issue, one comment per day -> GitHub email / app notification
    repo, owner = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    if repo and os.environ.get("GH_TOKEN"):
        q = subprocess.run(["gh", "issue", "list", "--repo", repo, "--state", "open", "--search",
                            f'"{REPORT_ISSUE_TITLE}" in:title', "--json", "number,title"],
                           capture_output=True, text=True)
        num = next((i["number"] for i in json.loads(q.stdout or "[]") if i["title"] == REPORT_ISSUE_TITLE), None)
        body = (f"@{owner} " if owner else "") + report
        if num:
            subprocess.run(["gh", "issue", "comment", str(num), "--repo", repo, "--body", body], check=False)
        else:
            subprocess.run(["gh", "issue", "create", "--repo", repo, "--title", REPORT_ISSUE_TITLE,
                            "--body", "Every evening the GitFinds automation posts its daily report here as a comment.\n\n"
                            + body, "--assignee", owner], check=False)
    # 3) optional Telegram
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        text = re.sub(r"[*_`]", "", report)[:4000]
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      json={"chat_id": chat, "text": text, "disable_web_page_preview": True}, timeout=30)


def main() -> None:
    now = dt.datetime.now(IST)
    sheet_token = ap.google_token(env("SHEETS_REFRESH_TOKEN"))
    ig_media = instagram_recent()
    sections, problems = [], []
    for name, tab_name, time_var in (("YouTube", "YouTube", "YT_POST_TIME"), ("Instagram", "Instagram", "IG_POST_TIME")):
        try:
            lines, probs = platform_report(name, tab_name, time_var, sheet_token, now, ig_media)
        except Exception as e:
            lines, probs = [f"### {name}", f"❌ Couldn't build the report: {e}"], [f"{name}: report error: {e}"]
        sections.append("\n".join(lines))
        problems += probs
    problems += latest_run_failures()
    head = f"## GitFinds daily report — {now:%a %d %b %Y, %H:%M} IST\n"
    head += ("✅ **All good.**" if not problems else
             "⚠️ **Needs attention:**\n" + "\n".join(f"- {p}" for p in problems))
    report = head + "\n\n" + "\n\n".join(sections)
    print(report)
    if "--print-only" not in sys.argv:
        send(report, not problems)


if __name__ == "__main__":
    main()
