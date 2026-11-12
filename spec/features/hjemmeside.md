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
- `/velkommen`, where the old site also showed the front page, still shows the front page (its
  canonical address stays `/`).
- Old intranet addresses (`/intern/…`, `/nyintern/…`, `/admin`, `/wiki`, `/photo-album/…`,
  `/media/…`, the old admissions admin pages) redirect to the same path on intern.gahk.dk, keeping
  the query and the method, so a form posted to an old address still arrives. `/intern/…` keeps
  its prefix: intern.gahk.dk drops it itself, so the link works before and after intern's own move
  to the root. The old intranet's static files (`/static/manifest.json`, its icons, `/static/dist/…`,
  the pictures its templates show) redirect to the same path on intern.gahk.dk when gahk.dk has no
  file of that name.
- Old picture addresses (`/static/legacy/image/…`, `/public/image/…`) redirect to the picture's new
  address, when intern has moved it into the image library. So do the fingerprinted names the old
  site also served them under (`gang.b8dc291a4e43.jpg`), and names with Danish letters, however the
  link spells them.
- Residents' calendar feeds (`/kalender/<token>.ics`) keep working: gahk.dk fetches them from intern
  as they are, unchanged and uncached.
- `www.gahk.dk` and `http://` redirect to `https://gahk.dk`.
- An unknown address gets the site's own 404 page, with the menu. Before intern has published
  anything, every page says *"Siden opdateres"* (503).

## Pictures

- Every picture on a page is shown in a size that fits the screen: gahk.dk makes smaller copies of
  each photo (480, 960, 1600 and 2400 px wide, never larger than the photo itself) in the WebP
  format, and the browser picks the smallest one that is sharp on its screen. A phone loads a
  fraction of what the full photo weighs.
- The copies carry no hidden information from the camera: no GPS position, no camera details, no
  colour profile (the colours are converted instead). Photos taken on their side are shown upright.
  Transparent PNGs stay transparent.
- Pictures further down a page load only when the visitor scrolls near them; the first picture on
  the front page and the big picture at the top load at once.
- The page keeps its layout: a picture takes the size the editor gave it, and its space is reserved
  before it has loaded.
- GIFs, animations and files that cannot be read are shown as they are. So are new pictures for the
  few minutes until their copies are ready.
- The original picture keeps its address, so old links to it keep working.
- A picture's address keeps the letters of its file name, Danish and others
  (`/billeder/…/tovtrækning-2019.jpg`), in lower case.
- A picture the editors take off the site disappears with the next publication, its smaller copies
  included.

## Speed and caching

- Pages, pictures and the stylesheet are plain files that the web server sends directly. Only forms,
  the visit count and redirects need a program, which keeps running between requests (see *drift*).
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

- **Static files, not a CMS on the webhost.** Apache sends pages and pictures as files; Python only
  answers forms, the visit count, redirects and intern's requests. Pre-rendering makes the site fast
  and lets it keep running while intern is down.
- **Editing happens only on intern.** One editor, one place for roles and the audit log. gahk.dk has
  no login of its own.
- **All pictures are content.** They live in intern's image library and are copied to the webhost
  when published; the site's code holds only the logo, icons and fonts. The old pictures were moved
  into the library once, and their old addresses redirect.
- **No trailing-slash redirects.** The old site served both forms, and links to both exist.
- **Picture names keep their letters.** Names are UTF-8 everywhere, so there is no reason to spell
  "æ", "ø" and "å" out as "ae", "oe" and "aa" (as an earlier version did). A name is stored in one
  normalized form (NFC), so the same letters always give the same address, and pages write every
  picture address percent-encoded (`tovtr%C3%A6kning-2019.jpg`), which is what a browser asks for
  anyway. Names already spelled out stay as they are.
- **Smaller copies are made on the webhost, not on intern.** They are cache: gahk.dk can make them
  again from the picture it already has, and intern needs no knowledge of how the site shows
  pictures. They are made when intern sends a picture, one picture per request, so even the first
  publish with every old picture stays within a web request; anything left over is made a few
  seconds at a time afterwards.
- **WebP only, quality 80, four widths.** Every browser in use shows WebP, and four steps cover
  phones to large high-resolution screens.
- **Removing camera data stays intern's job.** Pages link the copies wherever they exist, and the
  copies carry none of it. The original is still served at its address, link previews point at it
  (not every link-preview service reads WebP), and pictures without copies show it. So a picture
  that slipped through intern with its GPS position still shows it there.
- **Texts are cleaned on gahk.dk too.** intern removes scripts and the like before it saves a text;
  gahk.dk removes them again, with exactly the same rules, before it shows one. Nothing an editor
  can do on intern is lost by it.
