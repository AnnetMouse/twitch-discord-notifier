import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


STATE_FILE = Path("state.json")
USER_AGENT = "TwitchDiscordNotifier/1.0"


def required_env(name):
    value = os.environ.get(name, "").strip()

    if not value:
        print(f"Ошибка: не задана переменная {name}")
        sys.exit(1)

    return value


TWITCH_CLIENT_ID = required_env("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = required_env("TWITCH_CLIENT_SECRET")
TWITCH_LOGIN = required_env("TWITCH_LOGIN").lower()
DISCORD_WEBHOOK_URL = required_env("DISCORD_WEBHOOK_URL")

mention_setting = os.environ.get("DISCORD_MENTION", "@everyone").strip()

if mention_setting.lower() == "none":
    DISCORD_MENTION = ""
else:
    DISCORD_MENTION = mention_setting or "@everyone"


def trim(text, max_length):
    text = str(text or "")

    if len(text) <= max_length:
        return text

    return text[: max_length - 1] + "…"


def request_json(url, method="GET", headers=None, body=None, timeout=30):
    request_headers = {
        "User-Agent": USER_AGENT,
        **(headers or {}),
    }

    data = None

    if body is not None:
        if isinstance(body, dict):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        else:
            data = body

    request = urllib.request.Request(
        url=url,
        data=data,
        headers=request_headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content = response.read().decode("utf-8")

            if not content:
                return {}

            return json.loads(content)

    except urllib.error.HTTPError as error:
        response_text = error.read().decode("utf-8", errors="replace")

        print(f"HTTP ошибка {error.code}: {url}")
        print(response_text)

        raise

    except urllib.error.URLError as error:
        print(f"Ошибка соединения: {error}")
        raise


def get_twitch_token():
    form_data = urllib.parse.urlencode(
        {
            "client_id": TWITCH_CLIENT_ID,
            "client_secret": TWITCH_CLIENT_SECRET,
            "grant_type": "client_credentials",
        }
    ).encode("utf-8")

    result = request_json(
        "https://id.twitch.tv/oauth2/token",
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
        },
        body=form_data,
    )

    token = result.get("access_token")

    if not token:
        raise RuntimeError("Twitch не вернул access_token")

    return token


def get_stream(token):
    login = urllib.parse.quote(TWITCH_LOGIN)

    result = request_json(
        f"https://api.twitch.tv/helix/streams?user_login={login}",
        headers={
            "Authorization": f"Bearer {token}",
            "Client-Id": TWITCH_CLIENT_ID,
        },
    )

    streams = result.get("data", [])

    if not streams:
        return None

    return streams[0]


def load_state():
    if not STATE_FILE.exists():
        return {
            "live": False,
            "stream_id": None,
        }

    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            state = json.load(file)

        return {
            "live": bool(state.get("live", False)),
            "stream_id": state.get("stream_id"),
        }

    except (json.JSONDecodeError, OSError):
        return {
            "live": False,
            "stream_id": None,
        }


def save_state(live, stream_id):
    new_state = {
        "live": live,
        "stream_id": stream_id,
    }

    with STATE_FILE.open("w", encoding="utf-8") as file:
        json.dump(
            new_state,
            file,
            ensure_ascii=False,
            indent=2,
        )

        file.write("\n")


def send_discord_notification(stream):
    stream_url = f"https://www.twitch.tv/{TWITCH_LOGIN}"

    thumbnail = stream.get("thumbnail_url", "")
    thumbnail = thumbnail.replace("{width}", "1280")
    thumbnail = thumbnail.replace("{height}", "720")

    # Не даём Discord использовать старое изображение из кэша.
    if thumbnail:
        separator = "&" if "?" in thumbnail else "?"
        thumbnail = f"{thumbnail}{separator}stream={stream.get('id', '')}"

    user_name = stream.get("user_name") or TWITCH_LOGIN
    stream_title = stream.get("title") or "Трансляция уже началась!"
    game_name = stream.get("game_name") or "Категория не указана"
    viewer_count = stream.get("viewer_count", 0)
    started_at = stream.get("started_at")

    embed = {
        "title": trim(f"🔴 {user_name} начинает стрим!", 256),
        "description": trim(stream_title, 4096),
        "url": stream_url,
        "color": 9144575,
        "fields": [
            {
                "name": "🎮 Категория",
                "value": trim(game_name, 1024),
                "inline": True,
            },
            {
                "name": "👥 Зрителей",
                "value": str(viewer_count),
                "inline": True,
            },
            {
                "name": "🔗 Twitch",
                "value": f"[Перейти на трансляцию]({stream_url})",
                "inline": False,
            },
        ],
        "footer": {
            "text": "Twitch уведомление",
        },
    }

    if started_at:
        embed["timestamp"] = started_at

    if thumbnail:
        embed["image"] = {
            "url": thumbnail,
        }

    payload = {
        "username": "Twitch Оповещения",
        "content": DISCORD_MENTION,
        "allowed_mentions": {
            "parse": ["everyone", "roles"],
        },
        "embeds": [embed],
    }

    separator = "&" if "?" in DISCORD_WEBHOOK_URL else "?"
    webhook_url = f"{DISCORD_WEBHOOK_URL}{separator}wait=true"

    request_json(
        webhook_url,
        method="POST",
        body=payload,
    )

    print("Уведомление успешно отправлено в Discord.")


def main():
    previous_state = load_state()

    print(f"Проверяем Twitch-канал: {TWITCH_LOGIN}")

    token = get_twitch_token()
    stream = get_stream(token)

    if stream is None:
        print("Канал сейчас не в эфире.")

        if previous_state["live"] or previous_state["stream_id"] is not None:
            save_state(
                live=False,
                stream_id=None,
            )

            print("Состояние изменено на offline.")

        return

    current_stream_id = str(stream.get("id"))

    print(f"Канал в эфире. Stream ID: {current_stream_id}")

    already_notified = (
        previous_state["live"]
        and str(previous_state["stream_id"]) == current_stream_id
    )

    if already_notified:
        print("Уведомление об этой трансляции уже отправлялось.")
        return

    # Сначала отправляем сообщение.
    # Только после успешной отправки сохраняем состояние.
    send_discord_notification(stream)

    save_state(
        live=True,
        stream_id=current_stream_id,
    )

    print("Новое состояние сохранено.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Критическая ошибка: {error}")
        sys.exit(1)
