import sys
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from spotipy.cache_handler import MemoryCacheHandler

def prompt_credentials():
    client_id = input("Enter Spotify Client ID: ").strip()
    client_secret = input("Enter Spotify Client Secret: ").strip()
    redirect_uri = input("Enter Redirect URI: ").strip()
    return client_id, client_secret, redirect_uri

def get_spotify_token(client_id, client_secret, redirect_uri):
    cache_handler = MemoryCacheHandler()
    auth = SpotifyOAuth(
        scope='user-read-playback-state,user-modify-playback-state,user-read-recently-played',
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        open_browser=False,
        cache_handler = cache_handler
    )
    auth.get_access_token(as_dict=False)
    token_info = cache_handler.get_cached_token()
    if not token_info or not token_info.get('refresh_token'):
        sys.exit("Error: Unable to get Spotify refresh token.")
    return auth, token_info['refresh_token']

def choose_device(spotify):
    devices = spotify.devices().get("devices", [])
    if not devices:
        print("No active Spotify devices found.")
        return None
    for idx, device in enumerate(devices):
        print(f"{idx}: {device.get('name')}")
    while True:
        choice = input("Select default device by its number: ").strip()
        if choice.isdigit() and int(choice) in range(len(devices)):
            print(devices[int(choice)].get("name"))
            return devices[int(choice)].get("id")
        print("Invalid device number. Try again.")

def main():
    client_id, client_secret, redirect_uri = prompt_credentials()
    auth, token = get_spotify_token(client_id, client_secret, redirect_uri)
    spotify = spotipy.Spotify(oauth_manager=auth)
    device_id = choose_device(spotify)
    
    credentials = {
        "refresh_token": token,
        "client_id": client_id,
        "client_secret": client_secret,
        "device_id": device_id,
        "redirect_uri": redirect_uri,
    }
    print("\nCopy the following line to secrets.py:")
    print(f"SPOTIFY_CREDENTIALS={credentials}")

if __name__ == "__main__":
    main()