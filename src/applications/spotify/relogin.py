"""Logging in to Spotify from a phone, without a PC: first-time setup and renewing a revoked login.

When there is no refresh token or it is revoked, the Presto serves a page on the local network. The page links
to Spotify's login, which redirects to the app's Redirect URI (http://127.0.0.1:8080 by default).
Nothing answers there, but the failed page's address contains the authorization code; the user
pastes that address back into the Presto's page, and the Presto trades the code for a new
refresh token.

If secrets.py has no Client ID and Secret, or Spotify rejects the ones saved earlier, the page
asks for them first. They are saved together with the refresh token.
"""
import os
import uasyncio as asyncio

import http_page
from applications.spotify.spotify_client import NO_LOGIN, unquote, urlencode
from http_page import escape as _escape, form_value as _form_value

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
SCOPES = "user-read-playback-state user-modify-playback-state user-read-recently-played"
HTTP_PORT = 80


def _page(body):
    return http_page.page("Spotify login", body)

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
If it fails, start again from step 1.</small>{change_app}"""

_CHANGE_APP = """<p><small><a href="/app" style="color:#aaa">Use a different Spotify app</a></small></p>"""

_APP_FORM = """{message}
<p>In the <a href="https://developer.spotify.com/dashboard" target="_blank" style="color:#1db954">Spotify developer dashboard</a>,
create an app with the Redirect URI <code>{redirect_uri}</code> and the <b>Web API</b> box ticked.
Then copy its <b>Client ID</b> and <b>Client Secret</b> from the app's settings.</p>
<form method="post" action="/app">
Client ID<br><input name="client_id" value="{client_id}" autocomplete="off" required><br><br>
Client Secret<br><input name="client_secret" value="{client_secret}" autocomplete="off" required><br><br>
<button type="submit">Next</button>
</form>"""

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
        await http_page.handle(reader, writer, self._route)
        if self.done and not self._success_reported:
            self._success_reported = True
            self.on_success()

    async def _route(self, method, path, body):
        if method == 'GET' and path == '/':
            return '200 OK', self._start_page()
        if method == 'GET' and path == '/app' and not self.session.client_in_secrets:
            return '200 OK', self._app_page()
        if method == 'POST' and path == '/app' and not self.session.client_in_secrets:
            return '200 OK', await self._submit_app(
                _form_value(body, 'client_id').strip(), _form_value(body, 'client_secret').strip())
        if method == 'POST' and path == '/submit':
            return '200 OK', await self._submit(_form_value(body, 'address'))
        return '404 Not Found', _page('<p>Not found. <a href="/">Back</a></p>')

    def _start_page(self):
        if not self.session.has_client:
            return self._app_page()
        if self.session.auth_error_code == 'invalid_client':
            return self._app_page("Spotify rejected the app's Client ID or Secret ({}). "
                                  "Check them in the dashboard and enter them again.".format(self.session.auth_error))
        return self._form_page()

    def _app_page(self, error=None, client_id='', client_secret=''):
        if error:
            message = '<p class="err">{}</p>'.format(_escape(error))
        else:
            message = '<p>Connect this Presto to your Spotify account. First, enter your Spotify app\'s details.</p>'
        return _page(_APP_FORM.format(
            message=message,
            redirect_uri=_escape(self.session.redirect_uri),
            client_id=_escape(client_id),
            client_secret=_escape(client_secret),
        ))

    async def _submit_app(self, client_id, client_secret):
        if self.done:
            return _page(_DONE)
        if self.busy:
            return self._app_page("Already checking, please wait a moment and reload.", client_id, client_secret)
        self.busy = True
        try:
            error = await self.session.set_client(client_id, client_secret)
        finally:
            self.busy = False
        if error:
            return self._app_page("Spotify rejected these details: {}. Check them and try again.".format(error),
                                  client_id, client_secret)
        return self._form_page(ok="Spotify accepted the app's details. Now log in:")

    def _form_page(self, error=None, ok=None):
        if error:
            message = '<p class="err">{}</p>'.format(_escape(error))
        elif ok:
            message = '<p class="ok">{}</p>'.format(_escape(ok))
        elif self.session.auth_error == NO_LOGIN:
            message = '<p>Connect this Presto to your Spotify account in three steps:</p>'
        else:
            message = '<p>Spotify no longer accepts this Presto\'s login. Renew it in three steps:</p>'
        return _page(_FORM.format(
            message=message,
            authorize_url=_escape(self.authorize_url),
            redirect_uri=_escape(self.session.redirect_uri),
            change_app='' if self.session.client_in_secrets else _CHANGE_APP,
        ))

    async def _submit(self, address):
        if self.done:
            return _page(_DONE)
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
        return _page(_DONE)


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
