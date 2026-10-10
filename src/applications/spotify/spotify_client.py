import sys

import aiohttp
import uasyncio as asyncio
import ujson as json

# Seconds before a Spotify request is abandoned. MicroPython's aiohttp has no timeout of its own,
# so without this a stalled connection would block the caller forever.
REQUEST_TIMEOUT = 10

class SpotifyWebApiClient:
    def __init__(self, session):
        self.session = session

    async def play(self, context_uri=None, uris=None, offset=None, position_ms=None):
        body = {}
        if context_uri is not None: body['context_uri'] = context_uri
        if uris is not None: body['uris'] = list(uris)
        if offset is not None: body['offset'] = offset
        if position_ms is not None: body['position_ms'] = position_ms
        await self.session.put('https://api.spotify.com/v1/me/player/play', json=body)

    async def pause(self):
        await self.session.put('https://api.spotify.com/v1/me/player/pause')

    async def toggle_shuffle(self, state):
        value = "true" if state else "false"
        await self.session.put('https://api.spotify.com/v1/me/player/shuffle?state={}'.format(value))

    async def toggle_repeat(self, state):
        value = "track" if state else 'off'
        await self.session.put('https://api.spotify.com/v1/me/player/repeat?state={}'.format(value))

    async def next(self):
        await self.session.post('https://api.spotify.com/v1/me/player/next')

    async def previous(self):
        await self.session.post('https://api.spotify.com/v1/me/player/previous')

    async def current_playing(self):
        return await self.session.get('https://api.spotify.com/v1/me/player')

    async def recently_played(self):
        return await self.session.get('https://api.spotify.com/v1/me/player/recently-played?limit=1')


class Device:
    def __init__(self, id, is_active, is_private_session, is_restricted, name, type, volume_percent, **kwargs):
        self.id = id
        self.is_active = is_active
        self.is_private_session = is_private_session
        self.is_restricted = is_restricted
        self.name = name
        self.type = type
        self.volume_percent = volume_percent

    def __repr__(self):
        return 'Device(name={}, type={}, id={})'.format(self.name, self.type, self.id)


TOKEN_ENDPOINT = "https://accounts.spotify.com/api/token"
# Refresh token obtained on the device (re-login or rotation); overrides the one in secrets.py
TOKEN_FILE = "spotify_token.json"
# Must match a Redirect URI registered for the app in the Spotify developer dashboard
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8080"
# auth_error when there is no refresh token at all, i.e. first-time setup
NO_LOGIN = "No Spotify login yet"


class Session:
    def __init__(self, credentials):
        self.credentials = dict(credentials)
        self.device_id = credentials.get('device_id')
        self._needs_token = 'access_token' not in credentials
        # Set when Spotify permanently rejects the credentials (e.g. revoked refresh token)
        self.auth_error = None
        # Spotify's error code for auth_error, e.g. 'invalid_grant' or 'invalid_client'
        self.auth_error_code = None
        self._secrets_refresh_token = credentials.get('refresh_token') or None
        self.credentials['refresh_token'] = self._secrets_refresh_token
        self._load_saved_refresh_token()
        if not self.credentials['refresh_token']:
            self.auth_error = NO_LOGIN

    @property
    def redirect_uri(self):
        return self.credentials.get('redirect_uri', DEFAULT_REDIRECT_URI)

    def _load_saved_refresh_token(self):
        # The saved token is only used while secrets.py still holds the token it replaced,
        # so pasting fresh credentials into secrets.py always takes precedence.
        try:
            with open(TOKEN_FILE) as f:
                saved = json.load(f)
        except OSError:
            return
        except ValueError:
            print("Ignoring unreadable", TOKEN_FILE)
            return
        if saved.get('replaces') == self._secrets_refresh_token and saved.get('refresh_token'):
            self.credentials['refresh_token'] = saved['refresh_token']
            print("Using refresh token saved in", TOKEN_FILE)

    def save_refresh_token(self, refresh_token):
        self.credentials['refresh_token'] = refresh_token
        try:
            with open(TOKEN_FILE, 'w') as f:
                json.dump({'refresh_token': refresh_token, 'replaces': self._secrets_refresh_token}, f)
            print("Saved refresh token to", TOKEN_FILE)
        except OSError as e:
            print("Failed to save refresh token:", e)

    async def get(self, url):
        return await self._execute('get', url)

    async def put(self, url, json=None):
        if json is None:
            json = {}
        return await self._execute('put', self._add_device_id(url), json_body=json)

    async def post(self, url, json=None):
        if json is None:
            json = {}
        return await self._execute('post', self._add_device_id(url), json_body=json)

    def _headers(self):
        return {'Authorization': 'Bearer {access_token}'.format(**self.credentials)}

    async def _execute(self, method, url, json_body=None, **kwargs):
        if self.auth_error:
            raise SpotifyWebApiError("No access token", reason=self.auth_error)
        if self._needs_token:
            if not await self._refresh_access_token():
                raise SpotifyWebApiError("No access token", reason=self.auth_error)
            self._needs_token = False
        if json_body is not None:
            body = json.dumps(json_body)
            kwargs['data'] = body
            content_headers = {'Content-Type': 'application/json', 'Content-Length': str(len(body))}
        else:
            content_headers = {}
        for attempt in range(2):
            headers = self._headers()
            headers.update(content_headers)
            status, body = await asyncio.wait_for(self._request(method, url, headers, kwargs), REQUEST_TIMEOUT)
            if status == 401 and attempt == 0:
                print("Access token rejected (401), refreshing...")
                if not await self._refresh_access_token():
                    raise SpotifyWebApiError("No access token", status=401, reason=self.auth_error)
                continue
            if status >= 400:
                try:
                    error = body.get('error', {})
                    msg = error.get('message', str(status))
                    reason = error.get('reason')
                except Exception:
                    msg = str(status)
                    reason = None
                raise SpotifyWebApiError(msg, status=status, reason=reason)
            return body
        return None

    async def _request(self, method, url, headers, kwargs):
        """Sends one request. Returns (status, parsed JSON body or None)."""
        async with aiohttp.ClientSession() as client:
            async with getattr(client, method)(url, headers=headers, **kwargs) as response:
                body = None
                content_type = response.headers.get("content-type", "")
                if response.status >= 400 or content_type.startswith("application/json"):
                    try:
                        body = await response.json()
                    except Exception:
                        pass
                return response.status, body

    def _add_device_id(self, url):
        if not self.device_id:
            return url
        join = '&' if '?' in url else '?'
        return '{}{}{}'.format(url, join, 'device_id={}'.format(self.device_id))

    async def _post_token(self, params):
        """POSTs to Spotify's token endpoint. Returns (status, response body text)."""
        params['client_id'] = self.credentials['client_id']
        params['client_secret'] = self.credentials['client_secret']
        return await asyncio.wait_for(self._post_token_request(params), REQUEST_TIMEOUT)

    async def _post_token_request(self, params):
        async with aiohttp.ClientSession() as client:
            async with client.post(
                TOKEN_ENDPOINT,
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                data=urlencode(params),
            ) as response:
                try:
                    text = await response.text()
                except Exception:
                    text = ''
                return response.status, text

    async def _refresh_access_token(self):
        params = dict(grant_type="refresh_token", refresh_token=self.credentials['refresh_token'])
        for attempt in range(3):
            try:
                status, text = await self._post_token(params)
                if status >= 400:
                    print("Failed to refresh access token ({}/3): HTTP {} {}".format(attempt + 1, status, text))
                    # invalid_grant / invalid_client won't fix themselves, so don't retry
                    error, description = _token_error(text)
                    if status == 400 and error in ('invalid_grant', 'invalid_client'):
                        self.auth_error = description or error
                        self.auth_error_code = error
                        return False
                    continue
                tokens = json.loads(text)
                self.credentials['access_token'] = tokens['access_token']
                rotated = 'refresh_token' in tokens
                if rotated:
                    self.save_refresh_token(tokens['refresh_token'])
                print("Access token refreshed (expires_in={}s, refresh_token_rotated={})".format(
                    tokens.get('expires_in', '?'), rotated))
                return True
            except Exception as e:
                print("Failed to refresh access token ({}/3): {}".format(attempt + 1, e))
        print("Failed to refresh access token after 3 tries")
        return False

    async def exchange_authorization_code(self, code):
        """Trades the code from a Spotify login for a new refresh token and saves it.
        Returns None on success, otherwise an error message."""
        params = dict(grant_type="authorization_code", code=code, redirect_uri=self.redirect_uri)
        try:
            status, text = await self._post_token(params)
        except Exception as e:
            return "Could not reach Spotify: {}".format(e)
        if status >= 400:
            print("Failed to exchange authorization code: HTTP {} {}".format(status, text))
            error, description = _token_error(text)
            return description or error or "HTTP {}".format(status)
        tokens = json.loads(text)
        if 'refresh_token' not in tokens:
            return "Spotify did not return a refresh token"
        self.save_refresh_token(tokens['refresh_token'])
        # The session is usable again: the next request fetches an access token with the new refresh token
        self.auth_error = None
        self.auth_error_code = None
        self._needs_token = True
        return None


def _token_error(text):
    """Returns (error, error_description) from a token endpoint error body."""
    try:
        body = json.loads(text)
        return body.get('error'), body.get('error_description')
    except Exception:
        return None, None


class SpotifyWebApiError(Exception):
    def __init__(self, message, status=None, reason=None):
        super().__init__(message)
        self.status = status
        self.reason = reason


def quote(s):
    always_safe = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' 'abcdefghijklmnopqrstuvwxyz' '0123456789' '_.-'
    res = []
    for c in s:
        if c in always_safe:
            res.append(c)
            continue
        res.append('%%%x' % ord(c))
    return ''.join(res)

def quote_plus(s):
    s = quote(s)
    if ' ' in s:
        s = s.replace(' ', '+')
    return s

def unquote(s):
    res = s.split('%')
    for i in range(1, len(res)):
        item = res[i]
        try:
            res[i] = chr(int(item[:2], 16)) + item[2:]
        except ValueError:
            res[i] = '%' + item
    return "".join(res)

def urlencode(query):
    if isinstance(query, dict):
        query = query.items()
    li = []
    for k, v in query:
        if not isinstance(v, list):
            v = [v]
        for value in v:
            k = quote_plus(str(k))
            v = quote_plus(str(value))
            li.append(k + '=' + v)
    return '&'.join(li)
