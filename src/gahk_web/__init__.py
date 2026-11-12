"""gahk_web: the public site gahk.dk.

intern.gahk.dk owns every word and picture on the site. It pushes a signed snapshot here, and this
package renders that snapshot into static files in public_html. A Flask app (app.py), run as a
persistent FastCGI process, answers what static files cannot: the signed sync API, the optagelse
forms, the visit beacon, the calendar pass-through and the fallback for paths with no file
(redirects, 404, 503).
"""
