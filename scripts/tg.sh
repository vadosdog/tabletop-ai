#!/bin/sh
# Отправка сообщения в личку через телеграм-бота.
#
# Настройка (один раз):
#   1. напиши своему боту /start в личку
#   2. TG_BOT_TOKEN=123:AAA ./scripts/tg.sh --whoami   -> покажет твой chat_id
#   3. пропиши токен и id в ~/.config/tg-notify.env:
#        TG_BOT_TOKEN=123:AAA
#        TG_CHAT_ID=987654321
#
# Использование:
#   ./scripts/tg.sh "прогон закончился"
#   echo "текст" | ./scripts/tg.sh
#   ./scripts/tg.sh --whoami

set -eu

CONFIG="${TG_NOTIFY_ENV:-$HOME/.config/tg-notify.env}"
if [ -f "$CONFIG" ]; then
    # shellcheck disable=SC1090
    . "$CONFIG"
fi

if [ -z "${TG_BOT_TOKEN:-}" ]; then
    echo "tg.sh: не задан TG_BOT_TOKEN (ни в окружении, ни в $CONFIG)" >&2
    exit 1
fi

API="https://api.telegram.org/bot$TG_BOT_TOKEN"

if [ "${1:-}" = "--whoami" ]; then
    echo "Ищу chat_id в свежих апдейтах бота..." >&2
    echo "(если пусто — напиши боту любое сообщение в личку и повтори)" >&2
    curl -sS "$API/getUpdates" |
        grep -o '"chat":{"id":-\?[0-9]*,"first_name":"[^"]*"' |
        sed 's/.*"id":\(-\?[0-9]*\),"first_name":"\([^"]*\)".*/\1\t\2/' |
        sort -u
    exit 0
fi

if [ -z "${TG_CHAT_ID:-}" ]; then
    echo "tg.sh: не задан TG_CHAT_ID — получи его через: $0 --whoami" >&2
    exit 1
fi

if [ $# -gt 0 ]; then
    TEXT="$*"
else
    TEXT=$(cat)
fi

if [ -z "$TEXT" ]; then
    echo "tg.sh: пустое сообщение" >&2
    exit 1
fi

RESPONSE=$(curl -sS -X POST "$API/sendMessage" \
    --data-urlencode "chat_id=$TG_CHAT_ID" \
    --data-urlencode "text=$TEXT" \
    -d "disable_web_page_preview=true")

case "$RESPONSE" in
    '{"ok":true'*) exit 0 ;;
    *)
        echo "tg.sh: телеграм вернул ошибку:" >&2
        echo "$RESPONSE" >&2
        exit 1
        ;;
esac
