"""Logging in to Spotify from a phone, without a PC: first-time setup and renewing a revoked login.

When there is no refresh token or it is revoked, the Presto serves a page on the local network. The page links
to Spotify's login, which redirects to the app's Redirect URI (http://127.0.0.1:8080 by default).
Nothing answers there, but the failed page's address contains the authorization code; the user
pastes that address back into the Presto's page, and the Presto trades the code for a new
refresh token.
"""
import os
import uasyncio as asyncio

from applications.spotify.spotify_client import NO_LOGIN, unquote, urlencode

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
SCOPES = "user-read-playback-state user-modify-playback-state user-read-recently-played"
HTTP_PORT = 80

_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PrestoDeck Spotify login</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:32em;margin:0 auto;padding:16px;background:#121212;color:#eee;line-height:1.45}}
h1{{font-size:1.3em}} .err{{background:#5a1d1d;padding:10px;border-radius:6px}} .ok{{background:#1d4a2a;padding:10px;border-radius:6px}}
a.btn,button{{display:inline-block;background:#1db954;color:#000;font-weight:600;font-size:1em;padding:12px 18px;border:0;border-radius:24px;text-decoration:none}}
textarea{{width:100%;box-sizing:border-box;height:7em;font-size:.9em;background:#222;color:#eee;border:1px solid #555;border-radius:6px;padding:8px}}
ol li{{margin-bottom:1.2em}} small{{color:#aaa}}
</style></head><body>
<h1>PrestoDeck: Spotify login</h1>
{body}
</body></html>"""

_FORM = """{message}
<ol>
<li><a class="btn" href="{authorize_url}" target="_blank">Log in to Spotify</a><br>
Log in if asked, then tap <b>Agree</b>.</li>
<li>Spotify then sends you to a page that won't load ("This site can't be reached"). That's expected.
Copy the <b>whole address</b> from the address bar. It starts with <code>{redirect_uri}</code>.</li>
<li>Come back to this tab, paste the address below, and submit.
<form method="post" action="/submit">
<textarea name="address" placeholder="{redirect_uri}/?code=..." required></textarea><br><br>
<button type="submit">Submit</button>
</form></li>
</ol>
<small>The address works only once and expires about 10 minutes after you tap Agree.
If it fails, start again from step 1.</small>"""

_DONE = """<p class="ok">Logged in. The Presto is starting the player, so you can close this page.</p>"""


class Relogin:
    """Serves the re-login page and handles the pasted redirect address."""

    def __init__(self, session, on_success):
        self.session = session
        self.on_success = on_success
        # Ties the login to this Presto, so only addresses from logins started here are accepted
        self.state = ''.join('%02x' % b for b in os.urandom(8))
        self.busy = False
        self.done = False
        self._success_reported = False

    @property
    def authorize_url(self):
        return AUTHORIZE_URL + '?' + urlencode(dict(
            client_id=self.session.credentials['client_id'],
            response_type='code',
            redirect_uri=self.session.redirect_uri,
            scope=SCOPES,
            state=self.state,
        ))

    async def start(self):
        self.server = await asyncio.start_server(self._handle, '0.0.0.0', HTTP_PORT)
        print("Re-login page listening on port", HTTP_PORT)

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()
        print("Re-login page stopped")

    async def _handle(self, reader, writer):
        try:
            request_line = (await reader.readline()).decode()
            parts = request_line.split(' ')
            method, path = (parts[0], parts[1]) if len(parts) >= 2 else ('', '')
            length = 0
            while True:
                line = await reader.readline()
                if not line or line == b'\r\n':
                    break
                name, _, value = line.decode().partition(':')
                if name.strip().lower() == 'content-length':
                    length = int(value.strip())
            body = (await reader.readexactly(length)).decode() if length else ''

            if method == 'GET' and path == '/':
                status, html = '200 OK', self._form_page()
            elif method == 'POST' and path == '/submit':
                status, html = '200 OK', await self._submit(_form_value(body, 'address'))
            else:
                status, html = '404 Not Found', _PAGE.format(body='<p>Not found. <a href="/">Back</a></p>')

            data = html.encode()
            writer.write('HTTP/1.0 {}\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\n'
                         'Connection: close\r\n\r\n'.format(status, len(data)).encode())
            writer.write(data)
            await writer.drain()
        except Exception as e:
            print("Re-login request failed:", e)
        finally:
            writer.close()
            await writer.wait_closed()

        if self.done and not self._success_reported:
            self._success_reported = True
            self.on_success()

    def _form_page(self, error=None):
        if error:
            message = '<p class="err">{}</p>'.format(_escape(error))
        elif self.session.auth_error == NO_LOGIN:
            message = '<p>Connect this Presto to your Spotify account in three steps:</p>'
        else:
            message = '<p>Spotify no longer accepts this Presto\'s login. Renew it in three steps:</p>'
        return _PAGE.format(body=_FORM.format(
            message=message,
            authorize_url=_escape(self.authorize_url),
            redirect_uri=_escape(self.session.redirect_uri),
        ))

    async def _submit(self, address):
        if self.done:
            return _PAGE.format(body=_DONE)
        if self.busy:
            return self._form_page("Already checking a login, please wait a moment and reload.")

        query = _query_params(address.strip())
        if query.get('error'):
            return self._form_page("Spotify returned \"{}\". Tap Agree on the Spotify page to allow access."
                                   .format(query['error']))
        if not query.get('code'):
            return self._form_page("That doesn't look like the address Spotify sent you to. "
                                   "Copy the whole address, including the part after \"?code=\".")
        if query.get('state') != self.state:
            return self._form_page("That address is from a different login attempt. "
                                   "Tap \"Log in to Spotify\" below and use the new address.")

        self.busy = True
        try:
            error = await self.session.exchange_authorization_code(query['code'])
        finally:
            self.busy = False
        if error:
            return self._form_page("Spotify rejected the login: {}. "
                                   "The address may have expired or already been used, so start again from step 1."
                                   .format(error))
        self.done = True
        return _PAGE.format(body=_DONE)


def _form_value(body, name):
    """Extracts one field from an application/x-www-form-urlencoded body."""
    for pair in body.split('&'):
        key, _, value = pair.partition('=')
        if key == name:
            return unquote(value.replace('+', ' '))
    return ''


def _query_params(address):
    """Returns the query parameters of a URL as a dict."""
    query = address.split('?', 1)[1] if '?' in address else ''
    query = query.split('#', 1)[0]
    params = {}
    for pair in query.split('&'):
        key, _, value = pair.partition('=')
        if key:
            params[key] = unquote(value)
    return params


def _escape(text):
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')
