"""Setting up Wi-Fi from Presto.

Started only from the Presto's screen. The Presto runs its own Wi-Fi network (AP_SSID, with a random
password shown on screen) and serves a page there for choosing the Wi-Fi network and entering its password.

It works like a hotel Wi-Fi sign-in: every DNS name resolves to the Presto and every page request gets the
setup page, so phones detect a sign-in network and open the page themselves. That matters because phones
otherwise send browser traffic over mobile data when the Wi-Fi network has no internet.

The details are saved in WIFI_FILE. They are used while secrets.py still holds the Wi-Fi details they
replaced, so editing the Wi-Fi details in secrets.py always takes precedence.
"""
import os
import network
import socket
import ujson as json
import uasyncio as asyncio

import http_page
from http_page import escape, form_value

WIFI_FILE = "wifi.json"
AP_SSID = "PrestoDeck setup"
HTTP_PORT = 80
_PASSWORD_CHARS = "abcdefghjkmnpqrstuvwxyz23456789"  # no look-alikes such as l/1 or o/0

_FORM = """{message}
<form method="post" action="/save">
Network<br><select name="ssid">{networks}<option value="">Other (type its name below)</option></select><br><br>
Other network<br><input name="other" value="{other}" autocomplete="off"><br>
<small>Only needed if your network isn't in the list above.</small><br><br>
Password<br><input name="password" value="{password}" autocomplete="off"><br>
<small>Leave empty for an open network.</small><br><br>
<button type="submit">Save and restart</button>
</form>"""

_SAVED = """<p class="ok">Saved. The Presto restarts now and connects to <b>{ssid}</b>.
You can switch your phone back to your usual Wi-Fi.</p>"""


def _secrets_credentials(secrets):
    return [getattr(secrets, 'WIFI_SSID', ''), getattr(secrets, 'WIFI_PASSWORD', '')]


def load_credentials(secrets):
    """Returns the Wi-Fi name and password: from WIFI_FILE if saved there, otherwise from secrets.py."""
    from_secrets = _secrets_credentials(secrets)
    try:
        with open(WIFI_FILE) as f:
            saved = json.load(f)
    except OSError:
        return from_secrets
    except ValueError:
        print("Ignoring unreadable", WIFI_FILE)
        return from_secrets
    if saved['replaces'] != from_secrets:
        return from_secrets
    print("Using Wi-Fi saved in", WIFI_FILE)
    return [saved['ssid'], saved['password']]


class WifiSetup:
    """Runs the setup network and serves the Wi-Fi setup page on it."""

    def __init__(self, secrets, on_saved):
        self.secrets = secrets
        self.on_saved = on_saved
        self.password = ''.join(_PASSWORD_CHARS[b % len(_PASSWORD_CHARS)] for b in os.urandom(8))
        self.saved_ssid = None

    async def start(self):
        """Scans for nearby networks, then starts the setup network and page."""
        sta = network.WLAN(network.STA_IF)
        sta.disconnect()
        sta.active(True)
        found = sorted(sta.scan(), key=lambda n: -n[3])  # strongest first
        self.networks = []
        for n in found:
            ssid = n[0].decode()
            if ssid and ssid not in self.networks:
                self.networks.append(ssid)
        print("Found networks:", self.networks)

        self.ap = network.WLAN(network.AP_IF)
        self.ap.config(ssid=AP_SSID, key=self.password)
        self.ap.active(True)
        ip = self.ap.ifconfig()[0]
        self.url = "http://{}/".format(ip)
        self.server = await asyncio.start_server(self._handle, '0.0.0.0', HTTP_PORT)
        asyncio.create_task(self._answer_dns(bytes(int(part) for part in ip.split('.'))))
        print("Wi-Fi setup page at", self.url)

    async def _answer_dns(self, ip):
        """Answers every DNS query with the Presto's address (the 4 bytes of ip)."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(('0.0.0.0', 53))
        sock.setblocking(False)
        while True:
            try:
                query, sender = sock.recvfrom(512)
                question_end = query.index(b'\x00', 12) + 5  # name, then 2 bytes type and 2 bytes class
                sock.sendto(query[:2] + b'\x81\x80'          # same id; flags: standard answer, no error
                            + b'\x00\x01\x00\x01\x00\x00\x00\x00'  # 1 question, 1 answer
                            + query[12:question_end]
                            + b'\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04' + ip,  # A record, 60 s
                            sender)
            except OSError:  # nothing received yet
                await asyncio.sleep_ms(50)
            except ValueError:  # not a DNS query
                pass

    @property
    def join_qr_text(self):
        """Text of a QR code that phones read as "join this Wi-Fi network"."""
        return "WIFI:T:WPA;S:{};P:{};;".format(AP_SSID, self.password)

    async def _handle(self, reader, writer):
        await http_page.handle(reader, writer, self._route)
        if self.saved_ssid:  # reported after the response is sent, so the phone gets it before the restart
            ssid, self.saved_ssid = self.saved_ssid, None
            self.on_saved(ssid)

    async def _route(self, method, path, body):
        if method == 'GET':  # any address: that's how phones find the page (see the top of this file)
            return '200 OK', self._form_page(load_credentials(self.secrets)[0], '')
        if method == 'POST' and path == '/save':
            ssid = form_value(body, 'other').strip() or form_value(body, 'ssid')
            return '200 OK', self._save(ssid, form_value(body, 'password'))
        return '404 Not Found', _page('<p>Not found. <a href="/">Back</a></p>')

    def _form_page(self, ssid, password, error=None):
        if error:
            message = '<p class="err">{}</p>'.format(escape(error))
        else:
            message = '<p>Choose the Wi-Fi network the Presto should use and enter its password.</p>'
        # ssid is selected in the list if it was found nearby, otherwise it goes in the Other field
        return _page(_FORM.format(
            message=message,
            other=escape('' if ssid in self.networks else ssid),
            password=escape(password),
            networks=''.join('<option value="{0}"{1}>{0}</option>'.format(escape(n), ' selected' if n == ssid else '')
                             for n in self.networks),
        ))

    def _save(self, ssid, password):
        if not 1 <= len(ssid.encode()) <= 32:
            return self._form_page(ssid, password, "Choose a network from the list, or type its name "
                                                   "(at most 32 characters).")
        if password and not 8 <= len(password) <= 63:
            return self._form_page(ssid, password, "Wi-Fi passwords are 8 to 63 characters long. "
                                                   "Leave it empty for an open network.")
        with open(WIFI_FILE, 'w') as f:
            json.dump({'ssid': ssid, 'password': password,
                       'replaces': _secrets_credentials(self.secrets)}, f)
        print("Saved Wi-Fi to", WIFI_FILE)
        self.saved_ssid = ssid
        return _page(_SAVED.format(ssid=escape(ssid)))


def _page(body):
    return http_page.page("Wi-Fi setup", body)
