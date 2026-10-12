"""A minimal web server for the Presto's setup pages (Spotify login and Wi-Fi setup)."""
from applications.spotify.spotify_client import unquote

_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PrestoDeck: {heading}</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:32em;margin:0 auto;padding:16px;background:#121212;color:#eee;line-height:1.45}}
h1{{font-size:1.3em}} .err{{background:#5a1d1d;padding:10px;border-radius:6px}} .ok{{background:#1d4a2a;padding:10px;border-radius:6px}}
a.btn,button{{display:inline-block;background:#1db954;color:#000;font-weight:600;font-size:1em;padding:12px 18px;border:0;border-radius:24px;text-decoration:none}}
textarea,input,select{{width:100%;box-sizing:border-box;font-size:.9em;background:#222;color:#eee;border:1px solid #555;border-radius:6px;padding:8px}}
textarea{{height:7em}}
ol li{{margin-bottom:1.2em}} small{{color:#aaa}}
</style></head><body>
<h1>PrestoDeck: {heading}</h1>
{body}
</body></html>"""


def page(heading, body):
    return _PAGE.format(heading=heading, body=body)


async def handle(reader, writer, route):
    """Reads one HTTP request, answers it with `await route(method, path, body)` -> (status, html), and closes it."""
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

        status, html = await route(method, path, body)

        data = html.encode()
        writer.write('HTTP/1.0 {}\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\n'
                     'Connection: close\r\n\r\n'.format(status, len(data)).encode())
        writer.write(data)
        await writer.drain()
    except Exception as e:
        print("Web request failed:", e)
    finally:
        writer.close()
        await writer.wait_closed()


def form_value(body, name):
    """Extracts one field from an application/x-www-form-urlencoded body."""
    for pair in body.split('&'):
        key, _, value = pair.partition('=')
        if key == name:
            return unquote(value.replace('+', ' '))
    return ''


def escape(text):
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')
