# Feature: Statistics — visits to gahk.dk

How many people read the public site, and which pages, shown on intern's statistics page. Counting
happens on gahk.dk, so it works while intern is down. The numbers move to intern within a couple of
minutes.

## What is counted

- Every page view of a page that exists, per Copenhagen day and page: the **views** and the
  **unique visitors**. A page counts under its own address, whichever it was opened at: either
  slash form, and `/velkommen` as the front page.
- The whole site is counted once more as a total, so a visitor who reads five pages is one visitor
  of the site that day, not five.
- Not counted: unknown addresses (404s), redirects, previews on intern, visitors without JavaScript,
  and anything that identifies itself as a bot, crawler, monitor or script.
- A day's numbers grow during the day. intern shows today's numbers as they are so far, and keeps
  the final count once the day is over.

## Privacy

- No IP address is stored, and no cookie is set for counting.
- A unique visitor is a keyed hash of the day, the address and the browser identification. The
  hash changes every day, so nobody can be followed from one day to the next, and it is deleted after
  two days.
- Only the totals (day, page, views, visitors) leave the webhost. It deletes a day only once intern
  has stored it and the day is over.
- No third-party analytics run on the site.

## Decisions

- **A beacon, not the server log.** Each page sends one small request when it opens. The pages are
  static files, so the only other source would be the web server's access log, which the webhost
  hands out once a day with the addresses blurred.
- **A visitor is a browser for one day.** It is good enough for "how many people looked" without
  tracking anyone. One person on two devices counts twice, and a household behind one address with
  identical browsers counts once.
- **Lost at most minutes, never days.** If the webhost is lost, only the counts not yet pulled are
  gone.
