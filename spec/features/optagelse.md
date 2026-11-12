# Feature: Optagelse — the public forms on gahk.dk

Two forms let people get in touch with the indstilling: **Ansøg om rundvisning** (a tour, the first
step towards a room) at `/optagelse/ansoeg`, and **Ansøg om fremleje** (a sublet) at
`/optagelse/fremlej`. Both work while intern.gahk.dk is down.

## The forms

- The tour form asks for the full name, e-mail, gender, age, where they heard about the kollegium,
  their motivation, study year, the year they leave, university and field of study. The sublet form
  asks for the same personal fields plus occupation.
- The fields, choices and length limits are the same as intern's admissions forms, because intern
  checks every submission again when it pulls it.
- Next to the tour form, a **Før du søger** card links to the *optagelse* page with the criteria and
  the rent.
- A mistake shows the form again with the visitor's values and a Danish message at each field.
- A valid submission leads to a thank-you page. Reloading it sends nothing twice.
- The old form addresses (`/optagelse/send_rundvisning`, `/optagelse/send_fremleje`) still accept
  posts.

## Spam protection

- The form must be sent from a page on gahk.dk. A post from anywhere else is refused, with a message
  asking the visitor to go back and try again.
- A hidden field that people never see must stay empty. Bots fill it in and get a generic error.
- At most five submissions an hour from one visitor. The sixth gets *"prøv igen om en time"*.
- With a Cloudflare Turnstile key configured, the captcha must pass. If Cloudflare cannot be reached,
  the submission is refused rather than let through.

## What happens to a submission

- It waits on the webhost until intern collects it, at most a couple of minutes later. Then it is
  deleted from the webhost.
- intern turns it into an application, exactly as if the form had been sent on intern. The
  indstilling sees it in the usual list, and the applicant gets the usual automatic reply from
  intern.
- If intern is down, submissions wait on the webhost and arrive when intern is back. A single
  submission is never stored twice.

## Decisions

- **Collected by intern, not sent by gahk.dk.** The webhost has no access to intern's database or
  mail, and it should keep none. The cost is a delay of a couple of minutes before the automatic
  reply.
- **Checked twice.** The webhost checks the form so the visitor gets errors at once. intern checks
  again because the data crosses from a less trusted host.
- **The rate limit forgets addresses.** It counts a keyed hash of the visitor's address, kept for a
  day, never the address itself.
- **Fail closed on the captcha**, as intern did before the split.
