# GitFinds helper (Google Apps Script): setup

This one script, attached to the **"repo shared in zoom group"** sheet, does four jobs:

1. **Comment bot.** On Instagram it sends a DM with the repo + setup guide and a short public reply, within seconds. On YouTube it posts a public reply with the repo + setup guide, within 1–2 minutes.
2. **Emails.** Every GitFinds notification goes to one email address: the daily "both posts live" email with links, problems, missing repos and a daily comment summary.
3. **Starts the daily posting.** At 6:05 PM it tells GitHub to start the daily posting run, with a backup at 6:35 PM.
4. **Renews its Instagram key** every 7 days.

It never replies to itself, never replies twice to the same person on the same video, skips spam and abuse, and writes everything in the **Log** tab.
Likes are not possible: Instagram and YouTube do not let robots like comments. YouTube has no DMs, so the links go in the public reply.

---

## Step 1: Paste the code (2 minutes)
1. Open the **"repo shared in zoom group"** Google Sheet.
2. Click **Extensions → Apps Script**.
3. Delete everything in the editor, paste all of [`Code.gs`](Code.gs) and press **Ctrl+S**.

## Step 2: Add the settings (5 minutes)
In Apps Script, click the **gear (Project Settings)**, scroll to **Script properties** and click **Add script property** for each of these:

| Property | Value |
|---|---|
| `ALERT_EMAIL` | the email address that should get every notification |
| `IG_ACCESS_TOKEN` | the Instagram key for @gitfinds (the same value as the GitHub secret `IG_ACCESS_TOKEN`) |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | the same values as the GitHub secrets |
| `YOUTUBE_COMMENTS_REFRESH_TOKEN` | a new YouTube login for comments (see Step 5) |
| `DRY_RUN` | `true` for the first test, then `false` |
| `GH_DISPATCH_TOKEN` | *(optional, recommended)* a GitHub token that may start the posting (see Step 6) |

## Step 3: Switch it on (1 minute)
At the top, choose the function **setup** and click **Run**. Allow the permissions it asks for: it needs to read and write the sheet, send email and connect to Instagram, YouTube and GitHub.
The log shows a line `HOOK_SECRET = ...`. **Copy that value**, because you need it in Step 4.

## Step 4: Connect emails from GitHub (3 minutes)
1. In Apps Script, click **Deploy → New deployment**, then the gear icon → **Web app**.
   Set *Execute as* to **Me** and *Who has access* to **Anyone**. Click **Deploy** and **copy the Web app URL**.
2. In GitHub, go to the repo → **Settings → Secrets and variables → Actions → New repository secret**:
   - `EMAIL_HOOK_URL` = the Web app URL
   - `EMAIL_HOOK_SECRET` = the `HOOK_SECRET` value from Step 3
3. Test it: in GitHub, go to **Actions → GitFinds daily posting → Run workflow**, choose mode `test-email` and click **Run**. A test email should arrive.

## Step 5: YouTube comments login (5 minutes)
1. Open https://developers.google.com/oauthplayground and click the gear icon. Tick **Use your own OAuth credentials** and paste the Client ID and Secret.
2. On the left, type the scope `https://www.googleapis.com/auth/youtube.force-ssl` and click **Authorize APIs**. Sign in and **choose the GitFinds channel**.
3. Click **Exchange authorization code for tokens** and copy the **Refresh token** into the Script property `YOUTUBE_COMMENTS_REFRESH_TOKEN`.

## Step 6: Reliable daily start (optional, 3 minutes)
GitHub's own timer often skips runs. With this token the helper starts the posting itself at 6:05 PM and 6:35 PM.
1. In GitHub, click your photo → **Settings → Developer settings → Fine-grained tokens → Generate new token**.
2. Under *Repository access*, choose **Only select repositories → gitfinds-autopost**. Under *Permissions → Actions*, choose **Read and write**. Set the expiry to 1 year.
3. Copy the token into the Script property `GH_DISPATCH_TOKEN`.

## Step 7: Instant Instagram replies (optional, 5 minutes)
Without this step, Instagram comments are still answered within about 1 minute. With it, they're answered within seconds.
1. Open the Meta app: **Instagram → API setup with Instagram login → Configure webhooks**.
2. Set *Callback URL* to the Web app URL from Step 4, and *Verify token* to `gitfinds`. Click **Verify and save**.
3. Subscribe to the **comments** field and switch the webhook on for **gitfinds**. The app must be in **Live** mode.

## Step 8: Test, then go live
1. With `DRY_RUN = true`, comment "test" on a GitFinds video. Within about 1 minute a row appears in the **Test Log** tab showing what the bot *would* send. Nothing is sent in test mode.
2. If the row looks right, set `DRY_RUN` to `false`. The bot is now live.
3. Turn off the old GitHub comment bot so the two don't both reply: in GitHub, go to **Settings → Secrets and variables → Actions → Variables** and add `COMMENTS_PAUSED` = `true`.

---

**Stop the bot:** run **stop**. **Start it again:** run **setup**.
**When a new video's repo is missing:** you get an email. Add the repo + setup guide + video link to the sheet, and the waiting comments are answered automatically within a minute.
