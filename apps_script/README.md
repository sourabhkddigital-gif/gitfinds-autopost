# Apps Script version (replies in under a minute; instant with webhook)

1. Open the comment-bot sheet → **Extensions → Apps Script**.
2. Delete the default code, paste `Code.gs`, save (Ctrl+S).
3. Left menu **Project Settings** (gear) → **Script properties** → add:
   - `IG_ACCESS_TOKEN` = the Instagram token for @gitfinds (same value as the GitHub secret)
   - `DRY_RUN` = `true` for the first test, then `false`
   - optional for YouTube: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `YOUTUBE_COMMENTS_REFRESH_TOKEN`
   - optional: `VERIFY_TOKEN` = any word (webhook verification; default `gitfinds`)
4. Editor → pick function **setup** → **Run** → allow the permissions. `poll()` now runs every minute.
5. Check the **Log** tab. Set `DRY_RUN` to `false` to go live.

**Instant mode (webhook):** Deploy → New deployment → type *Web app* → execute as *Me*, access *Anyone* → copy the URL.
Meta app → Instagram → API setup → *Configure webhooks*: Callback URL = that URL, Verify token = your `VERIFY_TOKEN`,
subscribe to `comments`, switch Webhook Subscription for gitfinds to On, and set the app to Live.

Pause: run **stop**. Resume: run **setup**.
