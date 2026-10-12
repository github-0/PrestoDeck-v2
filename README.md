
Originally based on [fatihak/PrestoDeck](https://github.com/fatihak/PrestoDeck).

**New features:**

- Minimum LED brightness for dark album art
- Large invisible buttons covering the whole screen
- Track info shown briefly when the track changes
- Added error handling and UI messages
- Wi-Fi and Spotify setup is done with on-screen instructions and AP

Icons by [Icons8](https://icons8.com).

# PrestoDeck

PrestoDeck is a Spotify music controller for the [Pimoroni Presto](https://collabs.shop/xbvgb2). It displays album cover art and the currently playing track, with touch controls for playback.

**Contents**
1. [First-time setup](#1-first-time-setup)
2. [Usage](#2-usage)
3. [Maintenance](#3-maintenance)
4. [Technical details](#4-technical-details)

## 1. First-time setup

You need:
- A [Pimoroni Presto](https://collabs.shop/xbvgb2) and a USB-C cable
- A computer
- A Spotify Premium account
- A Wi-Fi network with internet access

### Step 1: Install Thonny
Download and install [Thonny](https://thonny.org/). You'll use it to copy files to the Presto.

### Step 2: Copy PrestoDeck to the Presto
1. Download PrestoDeck, either:
   - clone it: `git clone https://github.com/github-0/PrestoDeck-v2.git`, or
   - download the repository from GitHub.
2. Connect the Presto to your computer with the USB-C cable.
3. In Thonny, set the interpreter to **MicroPython (RP2040)**.
4. In Thonny's Files panel, open the `src` folder, select everything in it, right-click and choose **Upload to /**.

### Step 3: Create a Spotify app
1. Go to [Spotify for Developers](https://developer.spotify.com/dashboard/applications) and sign in.
2. Click **Create an App** and give it any name.
3. Set Redirect URI to `http://127.0.0.1:8080`
4. Tick **Web API** box and save.
5. Open the app's settings and note its **Client ID** and **Client Secret**. 

### Step 4: Start PrestoDeck, set up Wi-Fi and Spotify
1. Start PrestoDeck, either:
   - open `main.py` in Thonny and click **Run**, or
   - unplug the Presto from the computer and power it from any USB power supply. 
2. Presto shows **Wi-Fi not set up**. Follow on-screen instructions to set up Wi-Fi credentials. 
3. Presto restarts, connects to your Wi-Fi and shows **Connect to Spotify**. Follow on-screen instructions to set up Spotify credentials.

Done. Start playing something on Spotify on any device and it appears on the Presto.

## 2. Usage

### Touch controls
The whole screen is a touch area; there are no visible buttons.

| Tap | Does |
|---|---|
| Left half | Previous track |
| Right half | Next track |
| Bottom strip (lowest 20 %) | Play / pause |
| Top right corner | Wi-Fi setup |

For 5 seconds after startup, each touch area is shown with its icon, so you can see where to tap.

### Limitations

The display's text rendering can only support basic Latin letters (A–Z), so track and artist names are simplified:
- Common accented letters lose their accents (ä → a, é → e, ñ → n, ß → ss, ...).
- Anything else (Japanese, Cyrillic, emoji, and less common accented letters such as č or ł) is shown as a space, so a name written entirely in another alphabet appears blank.

## 3. Maintenance

PrestoDeck runs on its own, but a few things need attention now and then.

### Renewing the Spotify login
Spotify's refresh tokens expire after 180 days (as of 10/2026). When that happens the Presto shows **Spotify login expired** with a QR code and a web address. Follow instructions to renew.

| Presto screen | Refresh page (hosted on Presto) |
|---|---|
| <img src="screenshots/presto-relogin-screen.png" width="300"> | <img src="screenshots/relogin-web-page.png" width="250"> |


### Other things which can break it
- **Wi-Fi name or password changed** — the Presto shows why it can't connect. Tap the screen to open Wi-Fi setup, as in [Step 4](#step-4-start-prestodeck-set-up-wi-fi-and-spotify).
- **Spotify app disabled or deleted** — the Presto shows **Spotify app rejected**. Make sure the app still exists in the [Spotify developer dashboard](https://developer.spotify.com/dashboard) and that the Client ID and Secret match.
- **Album art stops loading** — the free image service wsrv.nl may be down. See [Online dependencies](#online-dependencies).

### Updating PrestoDeck
Download the new version and upload the contents of `src` again as in [Step 2](#step-2-copy-prestodeck-to-the-presto). The Wi-Fi details and Spotify login saved on the Presto are kept.

---

## 4. Technical details

### Online dependencies

The PrestoDeck connects to these services:

| Service | Used for |
|---|---|
| `api.spotify.com` | Reading what's playing (every 10s, `SPOTIFY_POLL_INTERVAL`) and the playback buttons |
| `accounts.spotify.com` | Getting a new access token at startup and about once an hour. Also used to get a refresh token. |
| `wsrv.nl` (free third-party image proxy, also known as images.weserv.nl) | Album art: it downloads the cover from Spotify's image CDN and resizes it to 480×480. Also returns the 1×1 sample of the cover used for the minimum LED brightness. |

Every request gives up after 10s (Spotify) or 15s (album art, `IMAGE_TIMEOUT`), so a stalled connection can't freeze the screen. If Wi-Fi drops, the Presto tries to reconnect every 30s (`WIFI_RETRY_INTERVAL`).

wsrv.nl receives the Spotify image URL of every track you play, but no Spotify login or account data. The Presto never contacts Spotify's image CDN directly.

**Setup only:**
- [Spotify for Developers](https://developer.spotify.com/dashboard) to create the app and its Client ID/Secret. Spotify must keep the app enabled.
- The `aiohttp` MicroPython package, if your firmware doesn't include it: `mpremote mip install aiohttp` (downloads it from micropython-lib).

### How the LED colours work

- **Ambient colours are computed on the Presto.** The firmware's `auto_ambient_leds` derives the LED colours from what's on screen. No online service is involved.
- **The minimum brightness check (which guarantess that LEDS are never completely dark even with dark album art) uses wsrv.nl.** On each track change the Presto fetches a 1×1 PNG of the cover from wsrv.nl and computes its perceived brightness. Below `LED_DARK_THRESHOLD` it turns off the ambient mode and sets all LEDs to the cover's average colour, brightened to at least `LED_MIN_BRIGHTNESS` while keeping its hue (a black cover gets `LED_MIN_COLOR`).
- If only the sample fetch fails, the check is skipped and the LEDs keep whatever the previous track set. If the cover itself fails, the LEDs switch to `LED_MIN_COLOR`.

Settings in `src/applications/spotify/spotify.py`:

| Setting | Default | Effect |
|---|---|---|
| `LED_BRIGHTNESS_ENABLED` | `True` | `False` disables the minimum brightness and removes the extra request. Ambient colours keep working. |
| `LED_DARK_THRESHOLD` | `60` | Brightness (0–255) below which the LEDs show the cover's brightened average colour. Raise to trigger more often. |
| `LED_MIN_BRIGHTNESS` | `34` | Minimum brightness (0–255) of that colour. Raise to brighten. |
| `LED_MIN_COLOR` | `(50, 30, 10)` | RGB colour of all 7 LEDs for black covers and when the cover fails to load. |

### Wi-Fi setup

Wi-Fi setup starts when PrestoDeck can't connect during startup, or through the top right corner of the player. The Presto then runs its own WPA2 network, `PrestoDeck setup`, with a new random password each time, and serves the setup page on it.

The setup network has no internet, so phones might keep sending browser traffic over mobile data, where the setup page can't be reached. To avoid that, the Presto acts as a sign-in (captive) portal: it answers every DNS lookup with its own address and every page request with the setup page. The phone's internet check then gets the setup page instead of its expected answer, so the phone opens the page in its sign-in window, which always uses the Wi-Fi. 

Wi-Fi credentials are saved in `wifi.json` on the Presto.

### Spotify tokens

The app uses three kinds of Spotify tokens:

| Token | Lifetime | Where it lives | Purpose |
|---|---|---|---|
| Authorization code | ~10 minutes, single use | The address you copy during login. Not stored. | Exchanged once for a refresh token. |
| Refresh token | 180 days (as of 10/2026) | `spotify_token.json` | Used to get access tokens. When it's rejected, the Presto shows the re-login screen. |
| Access token | 1 hour | Presto memory only | Sent with every Spotify API request (`Authorization: Bearer ...`). Renewed automatically. |


## Additional resources

- [Pimoroni Presto GitHub](https://github.com/pimoroni/presto)
- [Getting started with Pimoroni Presto](https://learn.pimoroni.com/article/getting-started-with-presto)
- Spotify client adapted from [MicroPython Spotify Web API](https://github.com/tltx/micropython-spotify-web-api)
