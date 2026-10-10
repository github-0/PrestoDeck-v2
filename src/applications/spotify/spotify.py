import gc
import time
import jpegdec
import pngdec
import uasyncio as asyncio
import aiohttp
import network

from touch import Button

from applications.spotify.spotify_client import NO_LOGIN, Session, SpotifyWebApiClient, SpotifyWebApiError
from base import BaseApp
import secrets

# --- Spotify polling ---
SPOTIFY_POLL_INTERVAL = 10      # seconds between Spotify API state polls

# --- Overlay durations ---
TRACK_INFO_DURATION = 5         # seconds track name/artist is shown after a track change
BUTTON_FEEDBACK_DURATION = 1    # seconds the button press icon stays on screen after a tap
NOTICE_DURATION = 3             # seconds an error message is shown after a failed button press
CONTROLS_DURATION = 5           # seconds the music controls are shown once the first track name has gone after startup

# --- Connection problems ---
OFFLINE_AFTER_FAILED_POLLS = 2  # failed polls in a row before the "can't reach" message is shown
WIFI_RETRY_INTERVAL = 30        # seconds between Wi-Fi reconnect attempts while it's down
WIFI_CONNECT_TIMEOUT = 20       # seconds per Wi-Fi connect attempt at startup
ART_RETRY_INTERVAL = 10         # seconds between attempts to fetch album art that failed to load
IMAGE_TIMEOUT = 15              # seconds before an album art request to wsrv.nl is abandoned

# --- LED ambient lighting ---
# Set to False to disable minimum LED brightness (skips the extra 1×1 HTTP sample request).
LED_BRIGHTNESS_ENABLED = True
# Perceived brightness (0–255) of the 1×1 album art sample below which dark mode activates.
# Raise to make the override trigger more eagerly; lower to only override very dark albums.
LED_DARK_THRESHOLD = 60
# RGB colour applied to all 7 LEDs in dark mode. Raise values to brighten, lower to dim.
LED_MIN_COLOR = (50, 30, 10)

_TRANSLITERATE = {
    'ä':'a','ö':'o','å':'a','ü':'u','ë':'e','ï':'i','ÿ':'y',
    'Ä':'A','Ö':'O','Å':'A','Ü':'U','Ë':'E','Ï':'I',
    'á':'a','à':'a','â':'a','ã':'a','Á':'A','À':'A','Â':'A','Ã':'A',
    'é':'e','è':'e','ê':'e','É':'E','È':'E','Ê':'E',
    'í':'i','ì':'i','î':'i','Í':'I','Ì':'I','Î':'I',
    'ó':'o','ò':'o','ô':'o','õ':'o','ø':'o','Ó':'O','Ò':'O','Ô':'O','Õ':'O','Ø':'O',
    'ú':'u','ù':'u','û':'u','Ú':'U','Ù':'U','Û':'U',
    'ñ':'n','Ñ':'N','ç':'c','Ç':'C','ß':'ss','æ':'ae','Æ':'AE','œ':'oe','Œ':'OE',
}

def transliterate(text):
    result = []
    for ch in text:
        result.append(_TRANSLITERATE.get(ch, ch if ord(ch) < 128 else ' '))
    return ''.join(result)

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

class ControlButton():
    """Represents a control button with an icon and touch area."""
    def __init__(self, display, name, icons, bounds, on_press=None, update=None):
        self.name = name
        self.enabled = False
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

    def draw_icon(self):
        """Renders the button's icon centered inside its bounds."""
        png = self.pngs[self.icon]
        x, y, width, height = self.button.bounds
        png_width, png_height = png.get_width(), png.get_height()
        x_offset = (width-png_width)//2
        y_offset = (height-png_height)//2

        png.decode(x+x_offset, y+y_offset)

class Spotify(BaseApp):
    """Main Spotify app managing playback controls, track display, and UI interactions."""
    def __init__(self):
        super().__init__(ambient_light=True, full_res=True, layers=2)
        self.dark_mode = False

        self.display.set_layer(0)
        icon = pngdec.PNG(self.display)
        icon.open_file("applications/spotify/icon.png")
        icon.decode(self.center_x - icon.get_width()//2, self.center_y - icon.get_height()//2 - 20)
        self.presto.update()

        self.display.set_font("sans")
        self.display.set_layer(1)
        text = "Ladataan bitit"
        self.display_text(text, (self.center_x - self.display.measure_text(text, scale=1)//2, self.height - 80), thickness=2)
        self.presto.update()

        wlan = network.WLAN(network.STA_IF)
        if wlan.isconnected():
            wlan.disconnect()
            time.sleep(1)
        self.connect_wifi(wlan)

        self.clear(1)
        text = "Haetaan biisit"
        self.display_text(text, (self.center_x - self.display.measure_text(text, scale=1)//2, self.height - 80), thickness=2)
        self.spotify_client = self.get_spotify_client()
        # JPEG decoder
        self.j = jpegdec.JPEG(self.display)

        self.state = State()
        self.feedback_button_ref = None
        self.feedback_icon_name = None
        self.feedback_shown_at = None
        self.wifi_retry_at = time.time()
        self.reset_status()
        self.setup_buttons()

    def connect_wifi(self, wlan):
        """Connects to the Wi-Fi in secrets.py, retrying until it works and showing why it failed."""
        ssid = getattr(secrets, 'WIFI_SSID', '')
        while True:
            try:
                # presto.connect() would make 10 attempts of up to 60 s each before returning
                asyncio.get_event_loop().run_until_complete(
                    self.presto.wifi.connect(timeout=WIFI_CONNECT_TIMEOUT, retries=1))
            except Exception as e:
                print("Wi-Fi connect failed:", e)
            if wlan.isconnected():
                return
            status = wlan.status()
            print("Wi-Fi status:", status)
            if status == network.STAT_WRONG_PASSWORD:
                reason = "Wrong Wi-Fi password"
            elif status == network.STAT_NO_AP_FOUND:
                reason = "Wi-Fi network not found"
            else:
                reason = "Can't connect to Wi-Fi"
            self.clear(1)
            self.display.set_layer(1)
            self.display.set_thickness(2)
            self.draw_centered(reason, self.height - 110, 0.8, self.colors.WHITE)
            self.display.set_thickness(1)
            self.draw_centered("Network: {}".format(ssid), self.height - 75, 0.6, self.colors.WHITE)
            self.draw_centered("Check secrets.py. Retrying...", self.height - 45, 0.6, self.colors.WHITE)
            self.presto.update()
            time.sleep(5)

    def reset_status(self):
        """Forgets the shown album art and any connection problems, e.g. when restarting the player."""
        self.art_key = None          # track_key() of the track whose album art is on screen
        self.art_attempt_at = 0
        self.art_error = None        # why the current track's album art isn't shown
        self.failed_polls = 0        # polls in a row that couldn't reach Spotify
        self.nothing_playing = False
        self.notice_text = None
        self.notice_shown_at = None
    
    def display_text(self, text, position, color=65535, scale=1, thickness=None):
        if thickness:
            self.display.set_thickness(2)
        x,y = position
        self.display.set_pen(color)
        self.display.text(text, x, y, scale=scale)
        self.presto.update()

    def get_spotify_client(self):
        credentials = getattr(secrets, 'SPOTIFY_CREDENTIALS', None) or {}
        if not credentials.get('client_id') or not credentials.get('client_secret'):
            while True:
                self.clear(1)
                self.display.set_pen(self.colors.WHITE)
                self.display.text("Spotify credentials not found", 40, self.height - 80, scale=.9)
                self.presto.update()
                time.sleep(2)

        session = Session(credentials)
        if session._needs_token and not session.auth_error:
            if asyncio.get_event_loop().run_until_complete(session._refresh_access_token()):
                session._needs_token = False
            gc.collect()
        return SpotifyWebApiClient(session)

    async def start_relogin(self, reason):
        """Shows the expired-login screen and serves the re-login page on the local network."""
        if self.spotify_client.session.auth_error_code == 'invalid_client':
            self.show_app_rejected_screen(reason)  # logging in again can't fix this
            return
        from applications.spotify.relogin import Relogin
        gc.collect()
        url = None
        try:
            self.relogin = Relogin(self.spotify_client.session, self.relogin_succeeded)
            await self.relogin.start()
            url = "http://{}/".format(network.WLAN(network.STA_IF).ifconfig()[0])
        except Exception as e:
            print("Failed to start re-login page:", e)
        self.show_relogin_screen(reason, url)

    def draw_centered(self, text, y, scale, color):
        text = self.fit_text(text, scale, self.width - 40)
        self.display.set_pen(color)
        self.display.text(text, self.center_x - self.display.measure_text(text, scale=scale)//2, y, scale=scale)

    def show_relogin_screen(self, reason, url):
        """Shows why Spotify stopped working and a QR code for the re-login page."""
        self.clear()
        self.display.set_layer(1)
        self.display.set_thickness(2)
        title = "Connect to Spotify" if reason == NO_LOGIN else "Spotify login expired"
        self.draw_centered(title, 40, 1, self.colors.WHITE)
        self.display.set_thickness(1)
        self.draw_centered(reason, 75, 0.6, self.colors.GRAY)

        qr_bottom = self.draw_qr_code(url, 110) if url else None
        if qr_bottom:
            self.draw_centered("Scan to log in (phone on same Wi-Fi)", qr_bottom + 35, 0.7, self.colors.WHITE)
            self.draw_centered(url, qr_bottom + 70, 0.8, self.colors.WHITE)
        elif url:
            self.draw_centered("To log in, open this on a phone", 180, 0.8, self.colors.WHITE)
            self.draw_centered("on the same Wi-Fi:", 220, 0.8, self.colors.WHITE)
            self.draw_centered(url, 270, 1, self.colors.WHITE)
        else:
            self.draw_centered("Run adhoc/generate_token.sh", 200, 0.8, self.colors.WHITE)
            self.draw_centered("and update secrets.py", 240, 0.8, self.colors.WHITE)
        self.presto.update()

    def show_app_rejected_screen(self, reason):
        """Shown when Spotify rejects the app's Client ID or Client Secret."""
        self.clear()
        self.display.set_layer(1)
        self.display.set_thickness(2)
        self.draw_centered("Spotify app rejected", 40, 1, self.colors.WHITE)
        self.display.set_thickness(1)
        self.draw_centered(reason, 75, 0.6, self.colors.GRAY)
        self.draw_centered("Check client_id and client_secret", 180, 0.8, self.colors.WHITE)
        self.draw_centered("in secrets.py, and that the app is", 220, 0.8, self.colors.WHITE)
        self.draw_centered("enabled in the Spotify dashboard.", 260, 0.8, self.colors.WHITE)
        self.draw_centered("Then restart the Presto.", 320, 0.8, self.colors.WHITE)
        self.presto.update()

    def draw_qr_code(self, text, top):
        """Draws a QR code on layer 0, centered horizontally. Returns its bottom y, or None if unavailable."""
        try:
            import qrcode
        except ImportError:
            return None
        code = qrcode.QRCode()
        code.set_text(text)
        size, _ = code.get_size()
        quiet = 4  # white border modules required around a QR code
        module = max(1, 200 // (size + 2 * quiet))
        box = (size + 2 * quiet) * module
        left = self.center_x - box // 2

        self.display.set_layer(0)
        self.display.set_pen(self.colors.WHITE)
        self.display.rectangle(left, top, box, box)
        self.display.set_pen(self.colors.BLACK)
        for x in range(size):
            for y in range(size):
                if code.get_module(x, y):
                    self.display.rectangle(left + (x + quiet) * module, top + (y + quiet) * module, module, module)
        self.display.set_layer(1)
        return top + box

    def relogin_succeeded(self):
        self.clear()
        self.display.set_layer(1)
        self.display.set_thickness(2)
        self.draw_centered("Logged in", self.center_y - 20, 1, self.colors.WHITE)
        self.display.set_thickness(1)
        self.draw_centered("Starting player...", self.center_y + 25, 0.8, self.colors.GRAY)
        self.presto.update()
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
        self.state = State()
        self.reset_status()
        self.clear()
        self.presto.update()
        loop = asyncio.get_event_loop()
        loop.create_task(self.touch_handler_loop())
        loop.create_task(self.display_loop())

    def setup_buttons(self):
        """Initializes invisible touch zone buttons."""
        def update_always_enabled(state, button):
            button.enabled = True

        def update_play_pause(state, button):
            button.enabled = True
            button.icon = "pause.png" if state.is_playing else "play.png"

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

        # Play/Pause is listed first so it takes priority in overlap zones
        buttons_config = [
            ("Play/Pause", ["play.png", "pause.png"], (0, int(self.height * 0.8), self.width, int(self.height * 0.2)), play_pause, update_play_pause),
            ("Previous", ["previous.png"], (0, 0, self.width // 2, self.height), previous_track, update_always_enabled),
            ("Next", ["next.png"], (self.width // 2, 0, self.width // 2, self.height), next_track, update_always_enabled),
        ]

        self.buttons = [
            ControlButton(self.display, name, icons, bounds, on_press, update)
            for name, icons, bounds, on_press, update in buttons_config
        ]

    def run(self):
        """Starts the app's event loops."""
        loop = asyncio.get_event_loop()
        loop.create_task(self.touch_handler_loop())
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
                    self.feedback_button_ref = button
                    self.feedback_icon_name = button.icon
                    self.feedback_shown_at = time.time()
                    self.show_button_feedback()
                    self.presto.update()
                    asyncio.create_task(self.press_button(button))
                    break
            
            # Wait here until the user stops touching the screen
            while self.touch.state:
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
        scale = 0.7
        text = self.fit_text(transliterate(text), scale, self.width - 40)
        self.display.set_layer(1)
        self.display.set_font("sans")
        self.display.set_pen(self.display.create_pen(40, 40, 40))
        self.display.rectangle(0, 0, self.width, 44)
        self.display.set_thickness(2)
        self.display.set_pen(self.colors.WHITE)
        self.display.text(text, (self.width - self.display.measure_text(text, scale=scale)) // 2, 22, scale=scale)

    def reconnect_wifi_if_down(self):
        """Starts a Wi-Fi reconnect if the connection is lost. Doesn't wait for it to finish."""
        wlan = network.WLAN(network.STA_IF)
        if wlan.isconnected() or time.time() - self.wifi_retry_at < WIFI_RETRY_INTERVAL:
            return
        self.wifi_retry_at = time.time()
        print("Wi-Fi is down, reconnecting...")
        try:
            wlan.connect(secrets.WIFI_SSID, secrets.WIFI_PASSWORD)
        except Exception as e:
            print("Wi-Fi reconnect failed:", e)

    def set_led_dark_mode(self, dark):
        """Holds the LEDs at LED_MIN_COLOR (dark) or lets them follow the screen (not dark)."""
        if not LED_BRIGHTNESS_ENABLED or dark == self.dark_mode:
            return
        if dark:
            # Pre-write the colour before disabling auto_ambient, so the LEDs don't blink
            r, g, b = LED_MIN_COLOR
            for i in range(7):
                self.presto.set_led_rgb(i, r, g, b)
            self.presto.auto_ambient_leds(False)
        else:
            self.presto.auto_ambient_leds(True)
        self.dark_mode = dark

    async def update_album_art(self, first_try):
        """Fetches and shows the current track's album art. Returns True if the screen changed."""
        self.art_attempt_at = time.time()
        if not album_image_url(self.state.track):
            self.art_error = "This track has no album art"
            img, rgb = None, None
        else:
            self.art_error = "Can't load album art, retrying..."
            img, rgb = await get_album_cover(self.state.track)
        if self.show_image(img):
            self.art_key = track_key(self.state.track)
            self.art_error = None
            if rgb is not None:
                br = perceived_brightness(*rgb)
                print(f"LED sample RGB={rgb} br={br} dark={br < LED_DARK_THRESHOLD}")
                self.set_led_dark_mode(br < LED_DARK_THRESHOLD)
            return True
        if first_try:
            # Don't leave the previous track's cover behind the new track's name.
            # The screen is now black, so keep the LEDs at their minimum.
            self.clear(0)
            self.set_led_dark_mode(True)
            return True
        return False

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
        
    def fit_text(self, text, scale, max_width):
        """Trims text to fit within max_width at the given scale, appending '...' if cropped."""
        if self.display.measure_text(text, scale=scale) <= max_width:
            return text
        while len(text) > 0:
            text = text[:-1]
            if self.display.measure_text(text + "...", scale=scale) <= max_width:
                return text + "..."
        return "..."

    def show_track_info(self):
        """Draws track name and artist centered on screen with a dark background box."""
        if not self.state.track:
            return

        max_width = self.width - 40  # 20px margin each side within the full-width box

        track_name = transliterate(self.state.track.get("name", ""))
        track_name = self.fit_text(track_name, 1.5, max_width)

        artists = transliterate(", ".join([a.get("name", "") for a in self.state.track.get("artists", [])]))
        artists = self.fit_text(artists, 1.0, max_width)

        self.display.set_layer(1)
        self.display.set_font("sans")

        name_scale = 1.5
        artist_scale = 1.0
        padding = 16
        font_height = 16  # approximate px height of font at scale=1

        name_w = self.display.measure_text(track_name, scale=name_scale)
        name_x = (self.width - name_w) // 2
        name_y = self.center_y - 30

        artist_w = self.display.measure_text(artists, scale=artist_scale)
        artist_x = (self.width - artist_w) // 2
        artist_y = self.center_y + 15

        # Draw full-width background box
        # Top accounts for font height above the y baseline
        box_y = name_y - int(font_height * name_scale) - padding
        box_h = artist_y + int(font_height * artist_scale) + padding - box_y
        self.display.set_pen(self.display.create_pen(40, 40, 40))
        self.display.rectangle(0, box_y, self.width, box_h)

        # Draw text on top of box
        self.display.set_thickness(3)
        self.display.set_pen(self.colors._BLACK)
        self.display.text(track_name, name_x + 2, name_y + 2, scale=name_scale)
        self.display.set_pen(self.colors.WHITE)
        self.display.text(track_name, name_x, name_y, scale=name_scale)

        self.display.set_thickness(2)
        self.display.set_pen(self.colors._BLACK)
        self.display.text(artists, artist_x + 2, artist_y + 2, scale=artist_scale)
        self.display.set_pen(self.colors.WHITE)
        self.display.text(artists, artist_x, artist_y, scale=artist_scale)

    def show_button_feedback(self):
        """Draws the feedback icon centered in the pressed button's zone."""
        if not self.feedback_button_ref or not self.feedback_icon_name:
            return
        png = self.feedback_button_ref.pngs.get(self.feedback_icon_name)
        if not png:
            return
        self.display.set_layer(1)
        x, y, w, h = self.feedback_button_ref.button.bounds
        png.decode(x + (w - png.get_width()) // 2, y + (h - png.get_height()) // 2)

    async def display_loop(self):
        """Periodically updates the display with the latest track info."""
        prev_state = None
        track_shown_at = None
        was_showing_info = False
        was_showing_feedback = False
        controls_pending = True  # show the controls once the first track name has gone
        controls_shown_at = None
        was_showing_controls = False
        prev_status = None

        while not self.state.exit:
            await asyncio.sleep(0)  # yield before fetch so touch events are processed first

            if not self.state.latest_fetch or time.time() - self.state.latest_fetch > SPOTIFY_POLL_INTERVAL:
                self.state.latest_fetch = time.time()
                result, reached = await fetch_state(self.spotify_client)
                gc.collect()  # free SSL context before opening next connection
                auth_error = self.spotify_client.session.auth_error
                if auth_error:
                    self.state.exit = True  # stop both loops so nothing redraws over the message
                    await self.start_relogin(auth_error)
                    return
                self.failed_polls = 0 if reached else self.failed_polls + 1
                if not reached:
                    self.reconnect_wifi_if_down()
                self.nothing_playing = reached and not result
                if result:
                    device_id, self.state.track, self.state.is_playing, self.state.shuffle, self.state.repeat = result
                    if device_id:
                        self.spotify_client.session.device_id = device_id

            await asyncio.sleep(0)

            track_changed = (
                not prev_state or
                (prev_state.track or {}).get('id') != (self.state.track or {}).get('id')
            )

            art_changed = False
            if track_changed and self.state.track:
                art_changed = await self.update_album_art(first_try=True)
                track_shown_at = time.time()
            elif (self.state.track and track_key(self.state.track) != self.art_key and
                    time.time() - self.art_attempt_at >= ART_RETRY_INTERVAL):
                art_changed = await self.update_album_art(first_try=False)

            await asyncio.sleep(0)

            now = time.time()
            showing_info = track_shown_at is not None and now - track_shown_at < TRACK_INFO_DURATION
            showing_feedback = self.feedback_shown_at is not None and now - self.feedback_shown_at < BUTTON_FEEDBACK_DURATION
            if controls_pending and was_showing_info and not showing_info:
                controls_pending = False
                controls_shown_at = now
            showing_controls = controls_shown_at is not None and now - controls_shown_at < CONTROLS_DURATION
            status = self.status_text(now)

            if (art_changed or prev_state != self.state or showing_info != was_showing_info or
                    showing_feedback != was_showing_feedback or showing_controls != was_showing_controls or
                    status != prev_status):
                self.clear(1)
                if showing_info:
                    self.show_track_info()
                if showing_controls:
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

            gc.collect()
            await asyncio.sleep_ms(200)

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