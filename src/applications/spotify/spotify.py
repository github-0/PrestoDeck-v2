import gc
import math
import time
import jpegdec
import pngdec
import uasyncio as asyncio
import aiohttp
import machine
import network

from touch import Button

from applications.spotify.spotify_client import NO_LOGIN, Session, SpotifyWebApiClient, SpotifyWebApiError
from base import BaseApp
from vectortext import VectorText
import wifi_setup
try:
    import secrets
except ImportError:
    secrets = None  # optional: Wi-Fi and Spotify details can both be entered on the Presto

# --- Spotify polling ---
SPOTIFY_POLL_INTERVAL = 10      # seconds between Spotify API state polls

# --- Overlay durations ---
TRACK_INFO_DURATION = 5         # seconds track name/artist is shown after a track change
BUTTON_FEEDBACK_DURATION = 1    # seconds the button press icon stays on screen after a tap
NOTICE_DURATION = 3             # seconds an error message is shown after a failed button press
CONTROLS_DURATION = 5           # seconds the music controls are shown once the first track name has gone after startup

# --- Wi-Fi button ---
WIFI_BUTTON_SIZE = 90           # px; the invisible square in the top right corner that opens Wi-Fi setup

# --- Frosted backdrops behind the controls ---
# The display can't blend colours, so a see-through dark tint is faked with a checkerboard of dark pixels.
FROST_COLOR = (10, 10, 10)      # not (0, 0, 0): black pixels on layer 1 are transparent
ZONE_GAP = 4                    # px of clear album art on each side of a border between frosted tap zones

# --- Connection problems ---
OFFLINE_AFTER_FAILED_POLLS = 2  # failed polls in a row before the "can't reach" message is shown
WIFI_RETRY_INTERVAL = 30        # seconds between Wi-Fi reconnect attempts while it's down
WIFI_CONNECT_TIMEOUT = 20       # seconds per Wi-Fi connect attempt at startup
ART_RETRY_INTERVAL = 10         # seconds between attempts to fetch album art that failed to load
IMAGE_TIMEOUT = 15              # seconds before an album art request to wsrv.nl is abandoned

# --- LED ambient lighting ---
# Set to False to disable minimum LED brightness (skips the extra 1×1 HTTP sample request).
LED_BRIGHTNESS_ENABLED = True
# Perceived brightness (0–255) of the 1×1 album art sample below which the LEDs stop following
# the screen and all 7 show the cover's average colour instead.
# Raise to make the override trigger more eagerly; lower to only override very dark albums.
LED_DARK_THRESHOLD = 60
# During that override the average colour is brightened to at least this perceived
# brightness (0–255). Raise to brighten, lower to dim.
LED_MIN_BRIGHTNESS = 34
# RGB colour used instead when the cover is black or has no art.
LED_MIN_COLOR = (50, 30, 10)

class State:
    """Tracks the current state of the Spotify app including playback and UI controls."""
    def __init__(self):
        self.toggle_leds = True
        self.is_playing = False
        self.repeat = False
        self.shuffle = False
        self.track = None
        self.exit = False

        self.latest_fetch = None
    
    def copy(self):
        state = State()
        state.toggle_leds = self.toggle_leds
        state.is_playing = self.is_playing
        state.repeat = self.repeat
        state.shuffle = self.shuffle
        state.exit = self.exit
        state.track = {'id': self.track['id']} if self.track else None # only care about track id
        return state
    
    def __eq__(self, other):
        if not isinstance(other, State) or other is None:
            return False
        return (
            self.toggle_leds == other.toggle_leds and
            self.is_playing == other.is_playing and
            self.repeat == other.repeat and
            self.shuffle == other.shuffle and
            self.exit == other.exit and
            (self.track or {}).get('id') == (other.track or {}).get('id')
        )

def frost_rect(display, x, y, w, h):
    """Frosts a rectangle with the current pen. Drawn as 45° lines, each one pixel per row, on every
    diagonal with an even x+y, so neighbouring frosted areas mesh into one checkerboard."""
    start = x + y + (x + y) % 2
    for s in range(start, x + y + w + h - 1, 2):
        x0 = max(x, s - (y + h - 1))
        x1 = min(x + w - 1, s - y)
        display.line(x0, s - x0, x1, s - x1)

def frost_rect_dark(display, x, y, w, h):
    """Like frost_rect(), but darker: every even row and column is filled, leaving one pixel in four clear."""
    for col in range(x + x % 2, x + w, 2):
        display.rectangle(col, y, 1, h)
    for row in range(y + y % 2, y + h, 2):
        display.rectangle(x, row, w, 1)

def frost_disc(display, cx, cy, r):
    """Frosts a disc with the current pen, meshing with frost_rect()."""
    m = int(math.sqrt(2) * r)  # furthest diagonal from the centre that touches the disc
    for k in range(-m + (cx + cy - m) % 2, m + 1, 2):
        # Points on the diagonal: (cx + t, cy + k - t), inside the disc where t² + (k - t)² <= r²
        s = math.sqrt(2 * r * r - k * k) / 2
        t0, t1 = math.ceil(k / 2 - s), math.floor(k / 2 + s)
        if t0 <= t1:
            display.line(cx + t0, cy + k - t0, cx + t1, cy + k - t1)

class ControlButton():
    """Represents a control button with an icon on a frosted disc, and a touch area."""
    def __init__(self, display, name, icons, bounds, disc_radius, on_press=None, update=None):
        self.name = name
        self.enabled = False
        self.display = display
        self.frost_pen = display.create_pen(*FROST_COLOR)
        self.disc_radius = disc_radius
        self.icon = icons[0] if icons else None
        self.pngs = {}
        if icons:
            for icon in icons:
                png = pngdec.PNG(display)
                png.open_file("applications/spotify/icons/" + icon)
                self.pngs[icon] = png

        self.button = Button(*bounds)
        self.on_press = on_press
        self.update = update

    def is_pressed(self, state):
        """Checks if the button is enabled and currently pressed."""
        return self.enabled and self.button.is_pressed()
    
    def draw(self, state):
        """Draws the button icon if enabled."""
        if self.enabled and self.icon:
            self.draw_icon()

    def draw_icon(self, icon=None, center_y=None):
        """Renders an icon (default: the button's current one) on a frosted disc centered inside its bounds,
        or at center_y if given."""
        png = self.pngs[icon or self.icon]
        x, y, width, height = self.button.bounds
        center_x = x + width // 2
        center_y = y + height // 2 if center_y is None else center_y
        self.display.set_pen(self.frost_pen)
        frost_disc(self.display, center_x, center_y, self.disc_radius)
        png.decode(center_x - png.get_width() // 2, center_y - png.get_height() // 2)

class Spotify(BaseApp):
    """Main Spotify app managing playback controls, track display, and UI interactions."""
    def __init__(self):
        super().__init__(ambient_light=True, full_res=True, layers=2)
        self.led_hold = None  # the colour hold_led_color() holds the LEDs at, or None while they follow the screen

        self.display.set_layer(0)
        icon = pngdec.PNG(self.display)
        icon.open_file("applications/spotify/icon.png")
        icon.decode(self.center_x - icon.get_width()//2, self.center_y - icon.get_height()//2 - 20)
        self.presto.update()
        # Startup texts are centered in the space between the icon and the bottom of the screen
        self.startup_text_y = (self.center_y - 20 + icon.get_height()//2 + self.height) // 2

        self.text = VectorText(self.display)
        self.track_text = (None, "", "")  # track_key() of the track whose name and artists are prepared, and them
        self.display.set_layer(1)
        self.draw_centered("Connecting to Wi-Fi", self.startup_text_y, 29, self.colors.WHITE)
        self.presto.update()

        asyncio.get_event_loop().run_until_complete(self.connect_wifi())

        self.clear(1)
        self.draw_centered("Loading Spotify", self.startup_text_y, 29, self.colors.WHITE)
        self.presto.update()
        self.spotify_client = self.get_spotify_client()
        # JPEG decoder
        self.j = jpegdec.JPEG(self.display)

        self.state = State()
        self.feedback_button_ref = None
        self.feedback_icon_name = None
        self.feedback_shown_at = None
        self.showing_controls = False  # the startup controls are on screen, set by display_loop
        self.wifi_retry_at = time.time()
        self.wifi_setup_requested = False  # set by the Wi-Fi button, handled by display_loop
        self.reset_status()
        self.setup_buttons()

    async def connect_wifi(self):
        """Connects to the saved Wi-Fi, retrying until it works and showing why it failed.

        Once a problem is shown, a tap on the screen opens Wi-Fi setup instead."""
        # Wi-Fi survives a soft reboot (e.g. stopping the program in Thonny during Wi-Fi setup). A setup
        # network left running takes the Presto's internet traffic, and just turning it off leaves no
        # route at all, so requests to Spotify fail. Only a hard reset brings the Wi-Fi back to normal.
        if network.WLAN(network.AP_IF).active():
            print("Wi-Fi setup network still running, restarting...")
            machine.reset()

        self.wifi_ssid, self.wifi_password = wifi_setup.load_credentials(secrets)
        if not self.wifi_ssid:
            self.show_startup_message("Wi-Fi not set up", "Tap the screen to set up Wi-Fi")
            await self.wait_for_tap()
            await self.run_wifi_setup()

        wlan = network.WLAN(network.STA_IF)
        if wlan.isconnected():
            wlan.disconnect()
            await asyncio.sleep(1)
        problem_shown = False
        failed_once = False
        while True:
            # presto.connect() would make 10 attempts of up to 60 s each before returning
            connecting = asyncio.create_task(self.presto.wifi.connect(
                self.wifi_ssid, self.wifi_password, timeout=WIFI_CONNECT_TIMEOUT, retries=1))
            if problem_shown and await self.wait_for_tap(until=connecting.done):
                connecting.cancel()
                await self.run_wifi_setup()
            try:
                await connecting
            except Exception as e:
                print("Wi-Fi connect failed:", e)
            if wlan.isconnected():
                return
            status = wlan.status()
            print("Wi-Fi status:", status)
            if not failed_once:
                # The first attempt after a restart sometimes fails although the details are right
                failed_once = True
                continue
            if status == network.STAT_WRONG_PASSWORD:
                reason = "Wrong Wi-Fi password"
            elif status == network.STAT_NO_AP_FOUND:
                reason = "Wi-Fi network not found"
            else:
                reason = "Can't connect to Wi-Fi"
            self.show_startup_message(reason, "Network: {}".format(self.wifi_ssid),
                                      "Retrying. Tap the screen to change Wi-Fi.")
            problem_shown = True
            retry_at = time.ticks_add(time.ticks_ms(), 5000)
            if await self.wait_for_tap(until=lambda: time.ticks_diff(time.ticks_ms(), retry_at) >= 0):
                await self.run_wifi_setup()

    def show_startup_message(self, title, *lines):
        """Shows a message below the PrestoDeck icon on the startup screen."""
        self.clear(1)
        self.display.set_layer(1)
        title_y = self.startup_text_y - 15 * len(lines)  # the title and lines centered together
        self.draw_centered(title, title_y, 24, self.colors.WHITE)
        for i, line in enumerate(lines):
            self.draw_centered(line, title_y + 35 + 30 * i, 18, self.colors.WHITE)
        self.presto.update()

    def show_message(self, title, subtitle):
        """Shows a short message in the middle of an otherwise empty screen."""
        self.clear()
        self.display.set_layer(1)
        self.draw_centered(title, self.center_y - 20, 29, self.colors.WHITE)
        self.draw_centered(subtitle, self.center_y + 25, 24, self.colors.GRAY)
        self.presto.update()

    async def wait_for_tap(self, until=None):
        """Waits for a tap on the screen and returns where it was, or returns None once until() is true."""
        while not (until and until()):
            self.touch.poll()
            if self.touch.state:
                where = self.touch.x, self.touch.y
                while self.touch.state:  # count the tap when the finger is lifted
                    await asyncio.sleep_ms(20)
                    self.touch.poll()
                return where
            await asyncio.sleep_ms(50)
        return None

    async def run_wifi_setup(self):
        """Runs Wi-Fi setup until the details are saved or it's cancelled, then restarts the Presto."""
        self.show_message("Starting Wi-Fi setup...", "Looking for Wi-Fi networks")
        setup = wifi_setup.WifiSetup(secrets, self.wifi_saved)
        await setup.start()
        self.show_wifi_setup_screen(setup)
        await self.wait_for_tap()
        machine.reset()

    def wifi_saved(self, ssid):
        self.show_message("Wi-Fi saved", "Restarting...")
        asyncio.create_task(self.restart_soon())

    async def restart_soon(self):
        await asyncio.sleep(2)  # let the phone receive its response and the message be read
        machine.reset()

    def show_wifi_setup_screen(self, setup):
        """Shows a QR code for joining the setup network, and how to open the setup page from there.

        No QR code for the page: a browser would load it over mobile data, as the setup network has no
        internet. The phone's sign-in window always uses the Wi-Fi."""
        self.clear()
        self.display.set_layer(1)
        self.draw_centered("Wi-Fi setup", 40, 29, self.colors.WHITE)

        left_x, right_x = self.width // 4, self.width * 3 // 4
        column = self.width // 2 - 20
        steps_y = 90  # first line of both steps' headings
        bottom = self.draw_qr_code(setup.join_qr_text, steps_y + 55, left_x, 170) or steps_y + 55
        self.draw_centered("Password:", bottom + 60, 18, self.colors.GRAY, left_x, column)
        self.draw_centered("or, with mobile", steps_y + 90, 18, self.colors.GRAY, right_x, column)
        self.draw_centered("data off, open", steps_y + 115, 18, self.colors.GRAY, right_x, column)
        self.draw_centered("Tap the screen to cancel", self.height - 22, 18, self.colors.GRAY)
        # The steps, and what has to be read or typed: larger
        self.draw_centered("1. Scan to join", steps_y, 24, self.colors.WHITE, left_x, column)
        self.draw_centered("the network", steps_y + 35, 24, self.colors.WHITE, left_x, column)
        self.draw_centered("2. Tap \"Sign in\"", steps_y, 24, self.colors.WHITE, right_x, column)
        self.draw_centered("on your phone", steps_y + 35, 24, self.colors.WHITE, right_x, column)
        self.draw_centered(setup.url, steps_y + 145, 21, self.colors.WHITE, right_x, column)
        self.draw_centered(wifi_setup.AP_SSID, bottom + 30, 24, self.colors.WHITE, left_x, column)
        self.draw_centered(setup.password, bottom + 90, 24, self.colors.WHITE, left_x, column)
        self.presto.update()

    async def confirm_wifi_setup(self):
        """Asks whether to start Wi-Fi setup. If not, goes back to the player."""
        self.clear()
        self.display.set_layer(1)
        # The question and the buttons below it, centered together on the screen
        title_y = self.center_y - 60
        button_y, button_h = title_y + 50, 80
        self.draw_centered("Start Wi-Fi setup?", title_y, 29, self.colors.WHITE)
        self.display.set_pen(self.display.create_pen(40, 40, 40))
        for x in (20, self.center_x + 10):
            self.display.rectangle(x, button_y, self.center_x - 30, button_h)
        self.draw_centered("Cancel", button_y + button_h // 2, 24, self.colors.WHITE,
                           self.width // 4, self.center_x - 40)
        self.draw_centered("Start setup", button_y + button_h // 2, 24, self.colors.WHITE,
                           self.width * 3 // 4, self.center_x - 40)
        self.presto.update()

        while True:
            x, y = await self.wait_for_tap()
            if button_y <= y < button_y + button_h:  # ignore taps outside the buttons
                break
        if x >= self.center_x:
            await self.run_wifi_setup()
        self.restart_player()

    def reset_status(self):
        """Forgets the shown album art and any connection problems, e.g. when restarting the player."""
        self.art_key = None          # track_key() of the track whose album art is on screen
        self.art_attempt_at = 0
        self.art_error = None        # why the current track's album art isn't shown
        self.failed_polls = 0        # polls in a row that couldn't reach Spotify
        self.nothing_playing = False
        self.notice_text = None
        self.notice_shown_at = None
        self.polled = False          # the first poll's result is on screen, so display_loop can start drawing
        self.art_changed = False     # new album art on layer 0 that display_loop hasn't shown yet
        self.track_shown_at = None   # when the current track's name started showing

    def get_spotify_client(self):
        credentials = getattr(secrets, 'SPOTIFY_CREDENTIALS', None) or {}
        session = Session(credentials)
        if session._needs_token and not session.auth_error:
            if asyncio.get_event_loop().run_until_complete(session._refresh_access_token()):
                session._needs_token = False
            gc.collect()
        return SpotifyWebApiClient(session)

    async def start_relogin(self, reason):
        """Shows the expired-login screen and serves the re-login page on the local network."""
        session = self.spotify_client.session
        if session.auth_error_code == 'invalid_client' and session.client_in_secrets:
            self.show_app_rejected_screen(reason)  # only fixable in secrets.py
            return
        from applications.spotify.relogin import Relogin
        gc.collect()
        url = None
        try:
            self.relogin = Relogin(session, self.relogin_succeeded)
            await self.relogin.start()
            url = "http://{}/".format(network.WLAN(network.STA_IF).ifconfig()[0])
        except Exception as e:
            print("Failed to start re-login page:", e)
        self.show_relogin_screen(reason, url)

    def draw_centered(self, text, y, px, color, center_x=None, max_width=None):
        """Draws text of em size px centered on center_x (default: the screen's center), the middle of
        its capitals at y, cropped to max_width."""
        center_x = self.center_x if center_x is None else center_x
        text = self.fit_text(self.text.clean(text), px, max_width or self.width - 40)
        self.display.set_pen(color)
        self.text.draw(text, center_x - self.text.width(text, px) // 2, y, px)

    def show_relogin_screen(self, reason, url):
        """Shows why Spotify stopped working and a QR code for the re-login page."""
        self.clear()
        self.display.set_layer(1)
        if reason == NO_LOGIN:
            title = "Connect to Spotify"
        elif self.spotify_client.session.auth_error_code == 'invalid_client':
            title = "Spotify app rejected"
        else:
            title = "Spotify login expired"
        self.draw_centered(title, 40, 29, self.colors.WHITE)
        self.draw_centered(reason, 75, 18, self.colors.GRAY)

        qr_bottom = self.draw_qr_code(url, 110) if url else None
        if qr_bottom:
            self.draw_centered("Scan to log in (phone on same Wi-Fi)", qr_bottom + 35, 21, self.colors.WHITE)
            self.draw_centered(url, qr_bottom + 70, 24, self.colors.WHITE)
        elif url:
            self.draw_centered("To log in, open this on a phone", 180, 24, self.colors.WHITE)
            self.draw_centered("on the same Wi-Fi:", 220, 24, self.colors.WHITE)
            self.draw_centered(url, 270, 29, self.colors.WHITE)
        else:
            self.draw_centered("Run adhoc/generate_token.sh", 200, 24, self.colors.WHITE)
            self.draw_centered("and update secrets.py", 240, 24, self.colors.WHITE)
        self.presto.update()

    def show_app_rejected_screen(self, reason):
        """Shown when Spotify rejects the app's Client ID or Client Secret."""
        self.clear()
        self.display.set_layer(1)
        self.draw_centered("Spotify app rejected", 40, 29, self.colors.WHITE)
        self.draw_centered(reason, 75, 18, self.colors.GRAY)
        self.draw_centered("Check client_id and client_secret", 180, 24, self.colors.WHITE)
        self.draw_centered("in secrets.py, and that the app is", 220, 24, self.colors.WHITE)
        self.draw_centered("enabled in the Spotify dashboard.", 260, 24, self.colors.WHITE)
        self.draw_centered("Then restart the Presto.", 320, 24, self.colors.WHITE)
        self.presto.update()

    def draw_qr_code(self, text, top, center_x=None, box=200):
        """Draws a QR code in a white square of size box on layer 0, centered on center_x (default: the
        screen's center). Longer texts get more, not bigger, squares, so equal boxes look the same size.

        Returns its bottom y, or None if unavailable."""
        try:
            import qrcode
        except ImportError:
            return None
        code = qrcode.QRCode()
        code.set_text(text)
        size, _ = code.get_size()
        quiet = 4  # white border modules required around a QR code
        module = max(1, box // (size + 2 * quiet))
        left = (self.center_x if center_x is None else center_x) - box // 2
        margin = (box - size * module) // 2  # quiet zone, plus what's left over from rounding

        self.display.set_layer(0)
        self.display.set_pen(self.colors.WHITE)
        self.display.rectangle(left, top, box, box)
        self.display.set_pen(self.colors.BLACK)
        for x in range(size):
            for y in range(size):
                if code.get_module(x, y):
                    self.display.rectangle(left + margin + x * module, top + margin + y * module, module, module)
        self.display.set_layer(1)
        return top + box

    def relogin_succeeded(self):
        self.show_message("Logged in", "Starting player...")
        asyncio.create_task(self.resume_after_relogin())

    async def resume_after_relogin(self):
        """Stops the re-login page and restarts the player loops, without rebooting."""
        await asyncio.sleep(2)  # let the phone receive its response and the message be read
        try:
            await self.relogin.stop()
        except Exception as e:
            print("Failed to stop re-login page:", e)
        self.relogin = None
        gc.collect()
        self.restart_player()

    def restart_player(self):
        """Restarts the player loops after they were stopped, without rebooting."""
        self.state = State()
        self.reset_status()
        self.clear()
        self.presto.update()
        loop = asyncio.get_event_loop()
        loop.create_task(self.touch_handler_loop())
        loop.create_task(self.poll_loop())
        loop.create_task(self.display_loop())

    def setup_buttons(self):
        """Initializes invisible touch zone buttons."""
        def update_always_enabled(state, button):
            button.enabled = True

        def update_play_pause(state, button):
            button.enabled = True
            button.icon = "pause_white.png" if state.is_playing else "play_white.png"

        async def play_pause(self):
            was_playing = self.state.is_playing
            self.state.is_playing = not was_playing
            try:
                if was_playing:
                    await self.spotify_client.pause()
                else:
                    await self.spotify_client.play()
            except Exception:
                self.state.is_playing = was_playing
                raise

        async def next_track(self):
            await self.spotify_client.next()
            self.state.latest_fetch = None  # trigger immediate poll

        async def previous_track(self):
            await self.spotify_client.previous()
            self.state.latest_fetch = None  # trigger immediate poll

        async def wifi(self):
            self.wifi_setup_requested = True  # display_loop stops the player and asks to confirm

        # Earlier buttons take priority in overlap zones. Previous and Next end where Play/Pause starts,
        # so their icons are centered in the part that can be tapped.
        self.play_top = int(self.height * 0.8)
        half = self.width // 2
        buttons_config = [
            ("Wi-Fi", ["wifi_white.png"], (self.width - WIFI_BUTTON_SIZE, 0, WIFI_BUTTON_SIZE, WIFI_BUTTON_SIZE), 36, wifi, update_always_enabled),
            ("Play/Pause", ["play_white.png", "pause_white.png"], (0, self.play_top, self.width, self.height - self.play_top), 42, play_pause, update_play_pause),
            ("Previous", ["previous_white.png"], (0, 0, half, self.play_top), 56, previous_track, update_always_enabled),
            ("Next", ["next_white.png"], (half, 0, self.width - half, self.play_top), 56, next_track, update_always_enabled),
        ]

        self.buttons = [
            ControlButton(self.display, name, icons, bounds, disc_radius, on_press, update)
            for name, icons, bounds, disc_radius, on_press, update in buttons_config
        ]

    def frost_tap_zones(self):
        """Frosts each tap zone, with a gap of clear album art between them, to show where to tap."""
        g, half, top, wifi = ZONE_GAP, self.width // 2, self.play_top, WIFI_BUTTON_SIZE
        wifi_left = self.width - wifi
        self.display.set_layer(1)
        self.display.set_pen(self.display.create_pen(*FROST_COLOR))
        # The two parts of Next overlap by a row: the Presto leaves out an end pixel of each frosting line,
        # which left a clear seam between them
        for zone in (
            (0, 0, half - g, top - g),                                         # Previous
            (half + g, 0, wifi_left - half - 2 * g, wifi + g),                 # Next, beside Wi-Fi
            (half + g, wifi + g - 1, self.width - half - g, top - wifi - 2 * g + 1),  # Next, below Wi-Fi
            (wifi_left + g, 0, wifi - g, wifi - g),                            # Wi-Fi
            (0, top + g, self.width, self.height - top - g),                   # Play/Pause
        ):
            frost_rect(self.display, *zone)

    def run(self):
        """Starts the app's event loops."""
        loop = asyncio.get_event_loop()
        loop.create_task(self.touch_handler_loop())
        loop.create_task(self.poll_loop())
        loop.create_task(self.display_loop())
        loop.run_forever()

    async def touch_handler_loop(self):
        """Handles touch input events and button presses."""
        while not self.state.exit:
            self.touch.poll()

            for button in self.buttons:
                button.update(self.state, button)
                if button.is_pressed(self.state):
                    print(f"{button.name} pressed")
                    if not self.showing_controls:  # the startup controls already show every icon
                        self.feedback_button_ref = button
                        self.feedback_icon_name = button.icon
                        self.feedback_shown_at = time.ticks_ms()
                        self.show_button_feedback()
                        self.presto.update()
                    asyncio.create_task(self.press_button(button))
                    break

            # Wait here until the user stops touching the screen, letting the other tasks run meanwhile
            while self.touch.state:
                await asyncio.sleep_ms(10)
                self.touch.poll()

            await asyncio.sleep_ms(1)

    async def press_button(self, button):
        """Runs a button's action and tells the user if it failed."""
        try:
            await button.on_press(self)
        except Exception as e:
            print("{} failed: {}".format(button.name, e))
            if isinstance(e, SpotifyWebApiError) and e.status == 404:
                self.show_notice("No active Spotify device")
            elif isinstance(e, SpotifyWebApiError) and e.status:
                self.show_notice("Spotify: {}".format(e))
            else:
                self.show_notice("Couldn't reach Spotify")
            self.state.latest_fetch = None  # poll now so the screen matches Spotify again

    def show_notice(self, text):
        self.notice_text = text
        self.notice_shown_at = time.time()

    def status_text(self, now):
        """Returns the message for the status bar at the top of the screen, or None."""
        if self.notice_text and now - self.notice_shown_at < NOTICE_DURATION:
            return self.notice_text
        if self.failed_polls >= OFFLINE_AFTER_FAILED_POLLS:
            if not network.WLAN(network.STA_IF).isconnected():
                return "No Wi-Fi, reconnecting..."
            return "Can't reach Spotify, retrying..."
        if self.art_error and self.state.track and track_key(self.state.track) != self.art_key:
            return self.art_error
        if self.nothing_playing and not self.state.track:
            return "Nothing playing on Spotify"
        return None

    def show_status(self, text):
        """Draws a message in a bar at the top of the screen."""
        self.display.set_layer(1)
        self.display.set_pen(self.display.create_pen(40, 40, 40))
        self.display.rectangle(0, 0, self.width, 44)
        self.draw_centered(text, 22, 21, self.colors.WHITE)

    def reconnect_wifi_if_down(self):
        """Starts a Wi-Fi reconnect if the connection is lost. Doesn't wait for it to finish."""
        wlan = network.WLAN(network.STA_IF)
        if wlan.isconnected() or time.time() - self.wifi_retry_at < WIFI_RETRY_INTERVAL:
            return
        self.wifi_retry_at = time.time()
        print("Wi-Fi is down, reconnecting...")
        try:
            wlan.connect(self.wifi_ssid, self.wifi_password)
        except Exception as e:
            print("Wi-Fi reconnect failed:", e)

    def hold_led_color(self, rgb):
        """Holds all LEDs at rgb, or lets them follow the screen again if rgb is None."""
        # Turning auto_ambient on again restarts its smoothing from black, which dims the LEDs for a moment
        if not LED_BRIGHTNESS_ENABLED or rgb == self.led_hold:
            return
        self.led_hold = rgb
        if rgb:
            # Pre-write the colour before disabling auto_ambient, so the LEDs don't blink. Then write it again:
            # the firmware may have been adding the screen's colours to these values (~2 ms per pass).
            for i in range(7):
                self.presto.set_led_rgb(i, *rgb)
            self.presto.auto_ambient_leds(False)
            time.sleep_ms(5)
            for i in range(7):
                self.presto.set_led_rgb(i, *rgb)
        else:
            self.presto.auto_ambient_leds(True)

    async def update_album_art(self, track, first_try):
        """Fetches and draws a track's album art, and sets art_changed if the screen changed."""
        self.art_attempt_at = time.time()
        if not album_image_url(track):
            self.art_error = "This track has no album art"
            img, rgb = None, None
        else:
            self.art_error = "Can't load album art, retrying..."
            img, rgb = await get_album_cover(track)
            if self.state.exit:
                return  # the player stopped meanwhile, e.g. for Wi-Fi setup, so leave the screen alone
        if self.show_image(img):
            self.art_key = track_key(track)
            self.art_error = None
            if rgb is not None:
                led_rgb = dark_led_color(rgb)
                print(f"LED sample RGB={rgb} br={perceived_brightness(*rgb)} hold={led_rgb}")
                self.hold_led_color(led_rgb)
            self.art_changed = True
        elif first_try:
            # Don't leave the previous track's cover behind the new track's name.
            # The screen is now black, so keep the LEDs at their minimum.
            self.clear(0)
            self.hold_led_color(LED_MIN_COLOR)
            self.art_changed = True

    def show_image(self, img, minimized=False):
        """Displays an album cover image on the screen. Returns True if it was drawn."""
        if not img:
            return False
        try:
            self.j.open_RAM(memoryview(img))

            img_width, img_height = self.j.get_width(), self.j.get_height()
            img_x, img_y = (self.width - img_width) // 2, (self.height - img_height) // 2

            self.clear(0)
            self.display.set_layer(0)
            self.j.decode(img_x, img_y, jpegdec.JPEG_SCALE_FULL, dither=True)
            return True
        except Exception as e:
            print("Failed to load image:", e)
            return False
        
    def fit_text(self, text, px, max_width):
        """Trims a cleaned text to fit within max_width at em size px, appending '...' if cropped."""
        if self.text.width(text, px) <= max_width:
            return text
        while len(text) > 0:
            text = text[:-1]
            if self.text.width(text + "...", px) <= max_width:
                return text + "..."
        return "..."

    def show_track_info(self):
        """Draws track name and artist centered on screen on a darkly frosted band."""
        if not self.state.track:
            return

        name_px = 44
        artist_px = 29
        padding = 16

        # Cleaning and fitting take a while for some scripts, and the band is redrawn on every tap
        key = track_key(self.state.track)
        if self.track_text[0] != key:
            max_width = self.width - 40  # 20px margin each side within the full-width box
            track_name = self.text.clean(self.state.track.get("name", ""))
            artists = self.text.clean(", ".join([a.get("name", "") for a in self.state.track.get("artists", [])]))
            self.text.prepare(track_name, artists)
            self.track_text = (key, self.fit_text(track_name, name_px, max_width),
                               self.fit_text(artists, artist_px, max_width))
        _, track_name, artists = self.track_text
        self.text.prepare(track_name, artists)  # another screen's text may have replaced the font meanwhile

        self.display.set_layer(1)

        name_x = (self.width - self.text.width(track_name, name_px)) // 2
        name_y = self.center_y - 30
        artist_x = (self.width - self.text.width(artists, artist_px)) // 2
        artist_y = self.center_y + 15

        # Full-width background band. The y's are the middle of the capitals: allow 0.55 em above for
        # accents and CJK characters, and below for descenders.
        box_y = name_y - int(0.55 * name_px) - padding
        box_h = artist_y + int(0.55 * artist_px) + padding - box_y
        self.display.set_pen(self.display.create_pen(*FROST_COLOR))
        frost_rect_dark(self.display, 0, box_y, self.width, box_h)

        # Text with a shadow on top of the band
        for text, x, y, px in ((track_name, name_x, name_y, name_px), (artists, artist_x, artist_y, artist_px)):
            self.display.set_pen(self.colors._BLACK)
            self.text.draw(text, x + 2, y + 2, px)
            self.display.set_pen(self.colors.WHITE)
            self.text.draw(text, x, y, px)

    def show_button_feedback(self):
        """Draws the feedback icon centered in the pressed button's zone."""
        if not self.feedback_button_ref or not self.feedback_icon_name:
            return
        self.display.set_layer(1)
        button = self.feedback_button_ref
        # Previous and Next show their icon in the middle of the screen, where it looks balanced
        # on its own, rather than in the middle of their tap zone as in the startup controls
        center_y = self.center_y if button.name in ("Previous", "Next") else None
        button.draw_icon(self.feedback_icon_name, center_y)

    async def poll_loop(self):
        """Polls Spotify and draws new album art on layer 0. Kept apart from display_loop, which would
        otherwise wait for every request and leave e.g. a tapped button's icon up until it finished."""
        state = self.state  # restart_player() replaces self.state, so a stopped loop can't come back to life
        while not state.exit:
            if not state.latest_fetch or time.time() - state.latest_fetch > SPOTIFY_POLL_INTERVAL:
                state.latest_fetch = time.time()
                result, reached = await fetch_state(self.spotify_client)
                gc.collect()  # free SSL context before opening next connection
                if state.exit:
                    return
                auth_error = self.spotify_client.session.auth_error
                if auth_error:
                    state.exit = True  # stop the other loops so nothing redraws over the message
                    await self.start_relogin(auth_error)
                    return
                self.failed_polls = 0 if reached else self.failed_polls + 1
                if not reached:
                    self.reconnect_wifi_if_down()
                self.nothing_playing = reached and not result
                if result:
                    device_id, track, is_playing, shuffle, repeat = result
                    if device_id:
                        self.spotify_client.session.device_id = device_id
                    # Load the new track's art before switching to it, so its name appears together with its art
                    if track.get('id') != (state.track or {}).get('id'):
                        await self.update_album_art(track, first_try=True)
                        if state.exit:
                            return
                        self.track_shown_at = time.time()
                    state.track, state.is_playing, state.shuffle, state.repeat = track, is_playing, shuffle, repeat
                self.polled = True

            elif (state.track and track_key(state.track) != self.art_key and
                    time.time() - self.art_attempt_at >= ART_RETRY_INTERVAL):
                await self.update_album_art(state.track, first_try=False)

            gc.collect()
            await asyncio.sleep_ms(200)

    async def display_loop(self):
        """Redraws layer 1 (track name, controls, tap icon, status) whenever what it shows changes.
        Never waits for the network, so things disappear on time."""
        prev_state = None
        was_showing_info = False
        was_showing_feedback = False
        controls_pending = True  # show the controls once the first track name has gone
        controls_shown_at = None
        was_showing_controls = False
        prev_status = None

        while not self.state.exit:
            if self.wifi_setup_requested:
                self.wifi_setup_requested = False
                self.state.exit = True  # stop the other loops so nothing redraws over the question
                await self.confirm_wifi_setup()
                return

            if not self.polled:  # keep the startup screen until there's something to show
                await asyncio.sleep_ms(50)
                continue

            now = time.time()
            showing_info = self.track_shown_at is not None and now - self.track_shown_at < TRACK_INFO_DURATION
            showing_feedback = (self.feedback_shown_at is not None and
                                time.ticks_diff(time.ticks_ms(), self.feedback_shown_at) < BUTTON_FEEDBACK_DURATION * 1000)
            if controls_pending and was_showing_info and not showing_info:
                controls_pending = False
                controls_shown_at = now
            showing_controls = controls_shown_at is not None and now - controls_shown_at < CONTROLS_DURATION
            self.showing_controls = showing_controls
            status = self.status_text(now)

            if (self.art_changed or prev_state != self.state or showing_info != was_showing_info or
                    showing_feedback != was_showing_feedback or showing_controls != was_showing_controls or
                    status != prev_status):
                self.art_changed = False
                self.clear(1)
                if showing_info:
                    self.show_track_info()
                if showing_controls:
                    self.frost_tap_zones()  # the buttons' frosted discs fall on the same pixels
                    for button in self.buttons:
                        button.update(self.state, button)
                        button.draw(self.state)
                if showing_feedback:
                    self.show_button_feedback()
                if status:
                    self.show_status(status)
                self.presto.update()
                prev_state = self.state.copy()
                was_showing_info = showing_info
                was_showing_feedback = showing_feedback
                was_showing_controls = showing_controls
                prev_status = status

            await asyncio.sleep_ms(50)

def track_key(track):
    """Identifies a track. Local files have no id, so fall back to the URI."""
    return track.get('id') or track.get('uri') if track else None

async def fetch_state(spotify_client):
    """Fetches the current playback state from Spotify.
    Returns (state or None, whether Spotify could be reached)."""

    current_track = None
    is_playing = False
    shuffle = False
    repeat = False
    device_id = None
    reached = False
    try:
        resp = await spotify_client.current_playing()
        reached = True
        if resp and resp.get("item"):
            current_track = resp["item"]
            is_playing = resp.get("is_playing")
            shuffle = resp.get("shuffle_state")
            repeat = resp.get("repeat_state", "off") != "off" 
            device_id = resp["device"]["id"]
            print("Got current playing track: " + current_track.get("name"))
    except Exception as e:
        print("Failed to get current playing track:", e)
        # Keep showing what's on screen. Falling back to the recently played track here
        # would flash the previous album art until the next successful poll.
        return None, False

    if not current_track:
        try:
            resp = await spotify_client.recently_played()
            if resp and resp.get("items"):
                current_track = resp["items"][0]["track"]
                print("Got recently playing track: " + current_track.get("name"))
        except Exception as e:
            print("Failed to get recently played track:", e)

    if not current_track:
        return None, reached

    return (device_id, current_track, is_playing, shuffle, repeat), reached

def parse_1x1_png(data):
    """Extracts (r, g, b) from a 1x1 PNG byte string."""
    if not data or len(data) < 33:
        return None
    try:
        import struct
        pos = 8  # skip signature
        while pos + 12 <= len(data):
            chunk_len = struct.unpack('>I', data[pos:pos+4])[0]
            chunk_type = data[pos+4:pos+8]
            chunk_data = bytes(data[pos+8:pos+8+chunk_len])
            pos += 12 + chunk_len
            if chunk_type == b'IDAT':
                try:
                    import deflate, io
                    with deflate.DeflateIO(io.BytesIO(chunk_data), deflate.ZLIB) as d:
                        raw = d.read()
                except ImportError:
                    try:
                        import uzlib
                        raw = uzlib.decompress(chunk_data, 15)
                    except ImportError:
                        import zlib
                        raw = zlib.decompress(chunk_data, 15)
                if len(raw) >= 4:
                    return raw[1], raw[2], raw[3]  # skip filter byte at [0]
            elif chunk_type == b'IEND':
                break
    except Exception as e:
        print("PNG parse error:", e)
    return None

def perceived_brightness(r, g, b):
    return (r * 299 + g * 587 + b * 114) // 1000

def dark_led_color(rgb):
    """Returns the colour to hold the LEDs at for a cover of average colour rgb, or None if
    the cover is bright enough for the LEDs to follow the screen."""
    br = perceived_brightness(*rgb)
    if br >= LED_DARK_THRESHOLD:
        return None
    if br == 0:
        return LED_MIN_COLOR
    if br >= LED_MIN_BRIGHTNESS:
        return rgb
    # Scale all channels by the same factor to keep the hue
    return tuple(min(255, c * LED_MIN_BRIGHTNESS // br) for c in rgb)

async def fetch_image(session, url):
    """GETs an image, giving up after IMAGE_TIMEOUT. Returns its bytes, or None."""
    async def get():
        async with session.get(url) as r:
            if r.status == 200:
                return await r.read()
            print("Image fetch failed:", r.status, url)
            return None
    try:
        return await asyncio.wait_for(get(), IMAGE_TIMEOUT)
    except Exception as e:
        print("Image fetch error:", e, url)
        return None

def album_image_url(track):
    """Returns the URL of the track's medium-sized album art, or None if it has none."""
    # Spotify lists the images largest first (640, 300, 64 px); local files have none
    images = (track.get("album") or {}).get("images") or []
    if not images:
        return None
    return images[1 if len(images) > 1 else 0]["url"]

async def get_album_cover(track):
    """Fetches full album art from wsrv.nl, and optionally a 1×1 colour sample for LED brightness."""

    img_url = album_image_url(track)
    if not img_url:
        return None, None
    art_url = f"https://wsrv.nl/?url={img_url}&w=480&h=480"
    img = None
    rgb = None
    try:
        async with aiohttp.ClientSession() as session:
            if LED_BRIGHTNESS_ENABLED:
                sample_url = f"https://wsrv.nl/?url={img_url}&w=1&h=1&output=png"
                rgb = parse_1x1_png(await fetch_image(session, sample_url))
            img = await fetch_image(session, art_url)
    except Exception as e:
        print("Fetch image error:", e)
    return img, rgb

def launch():
    """Launches the Spotify app and starts the event loop."""
    app = Spotify()
    app.run()

    app.clear()
    del app
    gc.collect()