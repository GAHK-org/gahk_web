# Feature: Hjemmeside — what visitors see on gahk.dk

The public site of the kollegium. Every word and picture is edited on intern.gahk.dk (*Hjemmeside*);
gahk.dk only shows it. Each page is a finished file on the webhost, so the site stays up when
intern or its storage is down.

## Pages and menu

- The front page (`/`) shows the welcome text on its own picture, the next three upcoming events
  and the four latest news items.
- Every other page has an address of one or two parts (`/faciliteter`, `/faciliteter/vaerelse`), a
  title, a text, and a picture at the top: its own, or the site's default one. With no picture at
  all, the top is a plain green band.
- A section with two or more pages (`/faciliteter` and the pages under it) shows a sidebar with all
  of them.
- The menu is the one the editors set up on intern, in their order. Links may go to pages or to
  other sites.
- `/begivenheder/` lists the upcoming events, then up to 20 past events and the 40 latest news items.
  An event moves from upcoming to past on its day in Copenhagen time, even if nobody edits anything.
- `/optagelse/` is an ordinary page: the criteria and the rent. The two forms have their own pages
  (see *optagelse*).
- Each page has a description for search engines and link previews, either the editors' own or a
  general one. The site has a sitemap and a robots file.

## Addresses that keep working

- A page is served with or without a trailing slash, without a redirect. Its canonical address is
  the form the menu uses.
- A page that moved or was renamed on intern answers its old address with a permanent redirect.
- Old intranet addresses (`/intern/…`, `/nyintern/…`, `/admin`, `/wiki`, `/photo-album/…`,
  `/media/…`, the old admissions admin pages) redirect to the same place on intern.gahk.dk, keeping
  the method, so a form posted to an old address still arrives.
- Old picture addresses (`/static/legacy/image/…`, `/public/image/…`) redirect to the picture's new
  address, when intern has moved it into the image library.
- Residents' calendar feeds (`/kalender/<token>.ics`) keep working: gahk.dk fetches them from intern
  as they are, unchanged and uncached.
- `www.gahk.dk` and `http://` redirect to `https://gahk.dk`.
- An unknown address gets the site's own 404 page, with the menu. Before intern has published
  anything, every page says *"Siden opdateres"* (503).

## Speed and caching

- Pages, pictures and the stylesheet are plain files that the web server sends directly. Only forms,
  the visit count and redirects start a program.
- Browsers keep a page for a minute, so an edit shows within about a minute of saving it on intern.
- Pictures and the stylesheet carry a fingerprint in their address, so browsers keep them for a year
  and never see a stale one.

## The edit link

- Editors who are logged in to intern see **Rediger siden** on every page. It opens the page in
  intern's editor, or the Hjemmeside overview for pages without their own editor.
- Other visitors never see it. The link is a convenience, not a permission: intern decides who may
  edit.

## The old push notifications

- The old site registered a service worker for the intranet's push notifications on gahk.dk. gahk.dk
  now serves one that removes itself and its push subscription. Residents turn notifications on
  again at intern.gahk.dk.

## Decisions

- **Static files, not a CMS on the webhost.** The webhost runs Python only as CGI, a new process per
  request. Pre-rendering makes the site fast and lets it keep running while intern is down.
- **Editing happens only on intern.** One editor, one place for roles and the audit log. gahk.dk has
  no login of its own.
- **All pictures are content.** They live in intern's image library and are copied to the webhost
  when published; the site's code holds only the logo, icons and fonts. The old pictures were moved
  into the library once, and their old addresses redirect.
- **No trailing-slash redirects.** The old site served both forms, and links to both exist.
