# GitFinds auto-poster

Posts one video a day to the **GitFinds YouTube channel** and **@gitfinds Instagram**
from the Google Sheet "GitFinds | Social Media Automation". It runs on GitHub's servers,
so no PC, Docker, n8n or Postiz is needed, and power cuts don't matter.

## How it works

- GitHub runs `autopost.py` every hour between 8 AM and midnight IST.
- In the run shortly before the posting time (default **6:00 PM IST**), for each platform:
  1. it reads the platform's tab ("YouTube" / "Instagram") and takes the first row with **Status = Pending**;
  2. it downloads the video from the Drive link;
  3. it posts it:
     - **YouTube** is uploaded with YouTube's own "publish at" set to the exact posting time.
     - **Instagram** is uploaded, then published at the posting time.
  4. it sets that row to **Status = Posted**, **Schedule Date** = the date and time, and **Post Link** if you add that column.
- One video per platform per day. A run that finds today's video already posted does nothing.

## Everyday use

| You want to… | Do this |
|---|---|
| Add videos | Add rows to the sheet with Status **Pending** |
| Change the posting time | GitHub → Settings → Secrets and variables → Actions → **Variables** → edit `YT_POST_TIME` / `IG_POST_TIME` (e.g. `18:00`, `7:30 PM`; between 8 AM and 11:30 PM) |
| Pause everything | Set the variable `PAUSED` to `true` (set `false` to resume) |
| Post the next video right now | **Actions** tab → *GitFinds daily posting* → **Run workflow** → mode `post-now` |
| Check that everything is OK | **Actions** → Run workflow → mode `dry-run` (shows what it would post; posts nothing) |
| See what happened | **Actions** tab: green tick = OK, red cross = failed. GitHub also emails you when a run fails. |

## Settings (GitHub → Settings → Secrets and variables → Actions)

**Secrets**

| Name | What it is |
|---|---|
| `SHEET_ID` | `1sprLDUSxOtMy9G1xNZDx_GYGhTYdPYKrjdKZYSi1q24` |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | The Google OAuth client (Google Cloud → APIs & Services → Credentials) |
| `SHEETS_REFRESH_TOKEN` | Google sign-in for the account that can **edit the sheet and open the videos** |
| `YOUTUBE_REFRESH_TOKEN` | Google sign-in for the **GitFinds YouTube channel** |
| `IG_ACCESS_TOKEN` | Instagram token for **@gitfinds** (lasts 60 days) |
| `GH_PAT` *(optional)* | GitHub token with "Secrets: read and write" on this repo, so the Instagram token is renewed automatically every week. Without it, make a new Instagram token every ~50 days. |

**Variables:** `YT_POST_TIME`, `IG_POST_TIME`, `PAUSED`

## One-time setup notes

- **Google Cloud project.** It must have the YouTube Data API v3, Google Sheets API and Google Drive API enabled.
  - Its OAuth consent screen must be **In production**, not Testing. Otherwise sign-ins expire every 7 days.
  - Its OAuth client must allow the redirect URI `https://developers.google.com/oauthplayground`.
- **Refresh tokens.** They're made with the OAuth Playground (gear icon → "Use your own OAuth credentials"):
  - **Sheets token** scopes: `https://www.googleapis.com/auth/spreadsheets` and `https://www.googleapis.com/auth/drive.readonly`.
  - **YouTube token** scope: `https://www.googleapis.com/auth/youtube.upload`. Choose the **GitFinds** channel when asked.
- **Instagram token.** Meta app → Instagram → *API setup with Instagram login* → **Generate token** for gitfinds.
  - The Instagram account must be a Professional (Creator/Business) account.
  - It must also be a tester of the app.
