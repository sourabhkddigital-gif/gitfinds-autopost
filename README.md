# GitFinds auto-poster

Posts **one video every day** to the **GitFinds YouTube channel** and **@gitfinds Instagram**, **at the same minute**
(default **7:00 PM IST**), from the Google Sheet "GitFinds | Social Media Automation". It runs on GitHub's servers,
so no PC is needed.

## How it works (every day)

| Time (IST) | What happens |
|---|---|
| 6:05 PM | The Apps Script helper tells GitHub to start (GitHub's own timer is a backup) |
| ~6:15 PM | The robot takes the first row with **Status = Pending** in the **YouTube** tab and downloads the video from the Drive link |
| | **YouTube**: it uploads the video, which goes public automatically at 7:00 PM. The row is marked *Scheduled* |
| | **Instagram**: it uploads the same video and waits until Instagram has processed it. The caption comes from the matching row in the **Instagram** tab (same Drive link), otherwise from the YouTube row |
| **7:00 PM** | The video goes live on **YouTube and Instagram together** |
| 7:01 PM | The sheet rows are marked **Uploaded** with the date and both **Post Links**, and one **email** with both live links (plus how many videos are left) goes out |
| 7:15, 8:00, 9:30 PM | Retry runs, only if something failed. A video is **never posted twice** |

If one platform fails, the other still posts, and you get an email saying what failed. If nothing can be posted by 11:30 PM, the video waits until the next day.

## Everyday use

| You want to… | Do this |
|---|---|
| Add videos | Add rows to the **YouTube** tab with the Drive link, title, tags, description, hashtags and Status **Pending**. Optionally add the same video, with its Instagram caption, to the **Instagram** tab. Keep at least 7 ready. You get a warning when fewer than 7 are left. |
| Post a video on a specific day | Put that date in its **Schedule Date** (e.g. `26/11/2026`) and keep Status **Pending** |
| Change the posting time | GitHub → Settings → Secrets and variables → Actions → **Variables** → `POST_TIME` (e.g. `19:00`) |
| Post every 2 days instead | Variable `POST_EVERY_DAYS` = `2` (default `1` = daily) |
| Pause everything / one platform | Variable `PAUSED` = `true`, or `YT_PAUSED` / `IG_PAUSED` = `true` |
| Check everything works (posts nothing) | **Actions → GitFinds daily posting → Run workflow →** mode `dry-run` |
| Test the email | Same, mode `test-email` |
| Post the next video right now | Same, mode `post-now` |

## Settings (GitHub → Settings → Secrets and variables → Actions)

**Secrets:** `SHEET_ID`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `SHEETS_REFRESH_TOKEN` (sheet + Drive),
`YOUTUBE_REFRESH_TOKEN` (YouTube upload), `IG_ACCESS_TOKEN` (Instagram, 60 days), `GH_PAT` (optional: renews the Instagram key every Monday),
`EMAIL_HOOK_URL` + `EMAIL_HOOK_SECRET` (emails, see [apps_script/README.md](apps_script/README.md)).

**Variables:** `POST_TIME`, `POST_EVERY_DAYS`, `PAUSED`, `YT_PAUSED`, `IG_PAUSED`, `COMMENTS_PAUSED`.

## One-time setup notes

- **Google Cloud project.** It needs the YouTube Data API v3, Google Sheets API and Google Drive API. Its OAuth consent screen must be **In production**, or sign-ins expire after 7 days.
- **Refresh tokens** are made with the OAuth Playground. The Sheets token needs the `spreadsheets` and `drive.readonly` scopes. The YouTube token needs `youtube.upload`, signed in as the GitFinds channel.
- **Instagram token.** Meta app → Instagram → *API setup with Instagram login* → **Generate token** for gitfinds. The account must be Professional.

## Emails instead of the old daily report
The old GitHub "daily report" no longer runs on its own. Instead you get:
- an **email right after posting**, with both live links and how many videos are left,
- an **email when something fails**, saying what to do,
- a **daily comment summary email** from the comment bot.
You can still run the old report by hand: **Actions → GitFinds daily report → Run workflow**.

---

# GitFinds comment bot

**New:** the comment bot now runs in the Google Sheet (Apps Script). It replies within seconds (Instagram) or 1–2 minutes (YouTube), never replies to itself and emails you about problems. Setup: [apps_script/README.md](apps_script/README.md). The GitHub version below stays as a backup and can be paused with `COMMENTS_PAUSED=true`.

Replies to **every new comment** on GitFinds videos with that video's **GitHub repo + user setup guide**.
Runs on GitHub every 10 minutes — no PC, no n8n, no Docker. Same secrets as the auto-poster.

- **Instagram**: the commenter gets a **DM** with repo + guide, plus a short public reply ("Sent you the repo + setup guide in your DMs 📩").
- **YouTube**: a public reply with repo + guide (YouTube has no DMs).
- Sheet: [repo shared in zoom group](https://docs.google.com/spreadsheets/d/176CJln8W2-mol-2FoBz1OJ786UINrFteR48QjPUO3pA) — tabs **Sheet1** (no. | github repo | user setup guide), **youtube** / **Instagram** (same + *posted video url*), **Log** (every comment + what the bot did).
- Safety: one reply per person per post, spam/abuse skipped, max 20 replies per run, a comment is never handled twice (the Log tab is the memory).
- A comment on a video that is **not in the sheet** is logged as `flag-no-post` and not answered — add the video's URL to the platform tab with its repo no.

| You want to… | Do this |
|---|---|
| Test without replying | **Actions** → *GitFinds comment bot* → **Run workflow** → mode `dry-run` → check the Log tab |
| Go live | It is live automatically every 10 min. Manual: Run workflow → mode `live` |
| Pause | Variables → `COMMENTS_PAUSED` = `true` |
| Change reply text | Variables → `DM_TEXT`, `PUBLIC_TEXT_IG`, `PUBLIC_TEXT_YT` (placeholders `{name}` `{repo}` `{guide}`) |

**Extra secret for YouTube**: `YOUTUBE_COMMENTS_REFRESH_TOKEN` — a YouTube refresh token with scope
`https://www.googleapis.com/auth/youtube.force-ssl` (OAuth Playground, sign in as the GitFinds channel).
The upload-only token used by the auto-poster can't read or post comments. Instagram needs nothing extra.
