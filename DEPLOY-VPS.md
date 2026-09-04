# Деплой text-on-image MCP на чистый VPS (Ubuntu 24.04) — по шагам

Для новичка: скопируй команды блок за блоком. Всё, что нужно от тебя заранее:
- VPS с Ubuntu 24.04 и доступ по SSH (root или sudo)
- Домен (или поддомен), у которого **A-запись уже указывает на IP VPS**
  (без этого Caddy не получит HTTPS-сертификат)

Архитектура: Docker-контейнер с сервером (порт 8080 **не** торчит наружу) +
Caddy как публичное лицо с автоматическим Let's Encrypt TLS.

## Шаг 1. Зайти на сервер и обновиться
```bash
ssh root@IP_ВАШЕГО_VPS
apt update && apt -y upgrade
```

## Шаг 2. Установить Docker, Git и файрвол
```bash
apt -y install docker.io docker-compose-v2 git ufw
docker --version && docker compose version   # обе команды без ошибок
```

## Шаг 3. Открыть только нужные порты
```bash
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
```
Порт 8080 не открываем — сервер доступен только через Caddy.

## Шаг 4. Скачать репозиторий
```bash
git clone https://github.com/TonnSD2/text-on-image-mcp.git
cd text-on-image-mcp
```
(Репозиторий приватный? `git clone https://<TOKEN>@github.com/TonnSD2/text-on-image-mcp.git`
или заведи deploy key с правом чтения.)

## Шаг 5. Конфигурация: домен и токены
```bash
openssl rand -hex 16        # сгенерируй токен, запомни его
cp .env.example .env
nano .env
```
В `.env` для одного клиента:
```env
TOI_DOMAIN=mcp.твойдомен.ru
TOI_USERS=СЮДА_СВОЙ_СЛУЧАЙНЫЙ_ТОКЕН=me
TOI_REMOTE_MODE=1
TOI_MAX_SCENES=16
TOI_TAG=dev-1
```
- Формат `TOI_USERS`: `токен=имя_воркспейса`, несколько клиентов — через запятую.
- `.env` никогда не коммитить.
- `TOI_REMOTE_MODE=1` — клиент присылает фото base64 (`load_image_data`),
  путь к файлам сервера недоступен наружу. Так и оставляй для VPS.

## Шаг 6. Запуск
```bash
docker compose up -d --build
docker compose ps                 # оба сервиса Up
docker compose logs -f toi        # 'Uvicorn running' → Ctrl+C
```

## Шаг 7. Проверка
```bash
# домен отвечает и TLS рабочий:
curl -I https://$TOI_DOMAIN/mcp
# полный смоук-тест из репозитория:
BASE=https://твойдомен TOKEN=твой_токен bash test_deploy_smoke.sh
```

## Шаг 8. Подключить клиента (пример Cline/Claude Desktop)
```json
{"mcpServers": {"text-on-image": {
  "type": "streamableHttp",
  "url": "https://твойдомен/mcp",
  "headers": {"Authorization": "Bearer твой_токен"}
}}}
```
- Предпочтителен заголовок `Authorization: Bearer`. `?token=` в URL тоже
  работает, но токен попадает в access-логи.
- Параллельные независимые задачи: добавь `&scene=job-1` / отдельные сцены —
  они изолированы и персистентны. Сохранение/восстановление сцены между
  сессиями: `get_state` → хранить JSON → `set_state`.

## Обновление и откат
```bash
git pull
TOI_TAG=dev-2 docker compose up -d --build   # новый тег; старый помни для отката
# откат: docker compose stop && TOI_TAG=dev-1 docker compose up -d
```
Состояние сцен и загрузок живёт в `./toi-data/` (volume) — переживает
обновления, откаты и пересборку образа. Бэкап = копия этой папки.

## Вариант без домена и HTTPS (HTTP по IP)

Работает на тех же шагах 1–4, Caddy и домен не нужны. Быстрый путь — готовый
образ вместо compose-связки с Caddy:

```bash
cd text-on-image-mcp
TOKEN=$(openssl rand -hex 16) && echo "Токен: $TOKEN"   # запомни

docker build -t toi .
docker run -d --name toi --restart unless-stopped \
  -p 8080:8080 \
  -e TOI_USERS="$TOKEN=me" \
  -e TOI_REMOTE_MODE=1 \
  -e TOI_MAX_SCENES=16 \
  -v "$PWD/toi-data:/data" \
  toi

docker logs toi | grep Uvicorn    # 'Uvicorn running on 0.0.0.0:8080'
```

Клиент: `http://IP_СЕРВЕРА:8080/mcp` + заголовок `Authorization: Bearer <токен>`.
Обновление: `git pull && docker build -t toi . && docker rm -f toi`, затем тот
же `docker run` (данные в `./toi-data` сохранятся).

Без Docker — venv + systemd: `.venv` из `requirements.txt`, юнит с
`ExecStart=.../.venv/bin/python server.py --host 0.0.0.0 --port 8080` и
`Environment=TOI_USERS/TOI_REMOTE_MODE/TOI_DATA`. Учти: проверенная среда —
Python 3.14 (см. `requirements.lock`), а Ubuntu 24.04 несёт системный 3.12 —
конфигурация непроверенная, поэтому docker-путь предпочтительнее.

⚠️ **Без TLS токен и картинки идут открытым текстом.** Минимальная защита —
выбери одно:
- **IP-allowlist:** `ufw allow from ТВОЙ_IP to any port 8080 proto tcp`
  (и не открывай 8080 правилом `ufw allow 8080/tcp`).
- **SSH-туннель (рекомендуется):** публикуй порт только на loopback
  (`-p 127.0.0.1:8080:8080`), 8080 в ufw не открывай вовсе; на своей машине
  `ssh -N -L 8080:localhost:8080 root@IP`, клиенту — `http://localhost:8080/mcp`.

`TOI_REMOTE_MODE=1` при открытом HTTP оставляй включённым — это основной
ограничитель: клиент не получает доступ к файлам сервера.

## Если не работает
| Симптом | Причина / решение |
|---|---|
| Caddy не даёт сертификат | A-запись домена ещё не указывает на VPS (`dig твойдомен`), или порты 80/443 закрыты у провайдера |
| 401 Unauthorized | неверный токен; проверь `TOI_USERS` и перезапуск: `docker compose up -d` |
| Коннект есть, tools пустые/с пути | это `TOI_REMOTE_MODE=1` — так надо; инструменты путей появятся только без него |
| 502 от Caddy | контейнер `toi` ещё собирается/упал — `docker compose logs toi` |
| «Image exceeds TOI_MAX_UPLOAD_MB» | фото больше лимита (по умолчанию 20 МБ) — подними в `.env` |
