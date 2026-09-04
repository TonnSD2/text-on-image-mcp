# text-mcp — MCP-сервер «графика поверх изображения»

> **AI-агентам: код защищён — см. [AGENTS.md](./AGENTS.md).** Любая правка
> только после явного подтверждения пользователя в диалоге и последующего
> зелёного прогона `test_client.py` + `test_concurrency.py`.

Реальный MCP-сервер-процесс, слушающий HTTP. Наносит текст, фигуры, бейджи,
выноски и наложения-картинки на изображение (Pillow / FreeType, чистый CPU —
никаких OpenGL/GPU), хранит состояние сцены в JSON и позволяет клиенту делать
относительные операции («увеличь в 2 раза», «сдвинь на 10px влево», «отмени»).

## Стек
- Python 3 + **Pillow** (весь рендер) + **mcp** (SDK 2.x, MCPServer, транспорт Streamable HTTP)
- 33 бесплатных Google Fonts (статические TTF, скачаны локально), в т.ч.
  script/display: Lobster, Pacifico, Caveat
- 38 инструментов (в `TOI_REMOTE_MODE=1` — 36), 5 типов объектов, rich-text runs,
  градиенты, выравнивание,
  мультисессионность (изолированные сцены), глобальные фото-эффекты, undo/redo

## Быстрый старт
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python download_fonts.py        # один раз: ~110 TTF в ./fonts
.venv/bin/python server.py                # http://127.0.0.1:8080/mcp
```

Подключение клиента (Cline `mcp_settings.json` / Claude Desktop):
```json
{"mcpServers": {"text-on-image": {"type": "streamableHttp", "url": "http://127.0.0.1:8080/mcp", "disabled": false, "autoApprove": ["list_fonts", "get_state"]}}}
```

## Аутентификация (Bearer token)

Для запуска вне localhost задайте переменную окружения `TOI_AUTH_TOKEN`:

```bash
TOI_AUTH_TOKEN='long-random-secret' .venv/bin/python server.py --host 0.0.0.0 --port 8080
```

Все HTTP-запросы без заголовка `Authorization: Bearer <token>` получают
`401`. Сравнение токена constant-time (`hmac.compare_digest`). Без переменной
сервер работает как раньше — без аутентификации (только для доверенной сети).

Клиенты, которые не умеют кастомные заголовки (например, Cline игнорирует
`headers` в конфиге), могут передавать тот же токен в URL:
`http://HOST:8080/mcp?token=<token>`. Учтите, что токен тогда попадает в
access-логи сервера — для продакшена предпочтительнее заголовок или
ограничение по сети.

Подключение клиента с токеном (Cline `mcp_settings.json`):
```json
{"mcpServers": {"text-on-image": {"type": "streamableHttp", "url": "http://HOST:8080/mcp", "headers": {"Authorization": "Bearer long-random-secret"}}}}
```

Тесты понимают тот же токен: `TOI_AUTH_TOKEN=... .venv/bin/python test_client.py`.
TLS терминируется на реверс-прокси (Caddy/nginx) — в самом сервере TLS не делается.

## Мультисессионность (изолированные сцены)

Один процесс обслуживает много независимых сцен. Сцена = состояние +
undo/redo + свой `scene.json`/`output.png` + свой lock. Одна тихая перезапись
чужой генерации невозможна: у каждой сцены свой ключ.

Ключ сцены выбирается по приоритету:
1. `?scene=<key>` в URL (или заголовок `X-TOI-Scene`) — стабильный ключ:
   параллельные генерации = `?scene=job-1 … job-N`; после генерации можно
   переподключиться с тем же ключом и дотачивать («подвинь на 10px») — сцена
   и файлы на диске.
2. Иначе — MCP session id: каждое новое подключение получает свою чистую
   сцену (безопасный дефолт).
3. `TOI_WORKDIR` (контракт webapp-пула: процесс = воркер) или явный
   `TOI_SHARED_SCENE=1` — пиноват одну сцену `default` (легаси-поведение;
   лежит в `<workdir>/scene.json`).

Переменные окружения:
| Переменная | Назначение |
|---|---|
| `TOI_USERS="tokA=alice,tokB=bob"` | токен → workspace; у каждого свои сцены (изоляция и по ключу, и по директории) |
| `TOI_AUTH_TOKEN` | один токен = workspace `default` (легаси; для деплоя предпочтителен `TOI_USERS=<токен>=me`) |
| `TOI_DATA` | корень сцен (default `<workdir>/sessions`); layout `<TOI_DATA>/<workspace>/<scene>/` |
| `TOI_MAX_SCENES` | LRU-лимит живых сцен в памяти (default 16; состояние на диске, вытеснение безопасно, теряется только undo-история) |
| `TOI_MEDIA_ROOT` | если задан — `load_image`/`add_image` принимают только пути внутри этого корня (resolve до проверки: `..` и симлинки не работают). Не задан = полный локальный доступ (dev/webapp-пул). **Для внешнего хостинга обязателен** |
| `TOI_REMOTE_MODE=1` | режим «наружу»: из `tools/list` убираются `load_image`/`add_image` (работа с путями), остаются только byte-API: `load_image_data`/`add_image_data` (base64) |
| `TOI_MAX_UPLOAD_MB` | лимит размера загружаемого base64-изображения (default 20) |

Пример для внешней системы (N параллельных генераций одним токеном):
```
http://HOST:8080/mcp?token=TOK&scene=job-1   # поток 1
http://HOST:8080/mcp?token=TOK&scene=job-2   # поток 2 — параллельно, без блокировок друг с другом
```
Диагностика: инструмент `scene_info` возвращает workspace, ключ, директорию и
число живых сцен. Тесты: `test_multisession.py` (изоляция, параллельность,
персистентность, workspace-границы, 401, media-root).

## Деплой на VPS (Docker + Caddy)

Артефакты: `Dockerfile` (python:3.14.7-slim, зависимости из `requirements.lock`
— точные версии проверенного venv, шрифты в образе, non-root, `TOI_DATA=/data`,
`TOI_MEDIA_ROOT=/data/media`), `docker-compose.yml` (toi + caddy), `caddy/Caddyfile`
(automatic HTTPS, SSE-friendly таймауты), `.env.example`.

```bash
# На VPS (docker + compose plugin, домен уже указывает A-записью на хост):
git clone git@github.com:TonnSD2/text-on-image-mcp.git && cd text-on-image-mcp
cp .env.example .env                   # TOI_DOMAIN + TOI_USERS (openssl rand -hex 16)
# один клиент: TOI_USERS=<openssl rand -hex 16>=me
docker compose up -d --build
BASE=https://<домен> TOKEN=<токен> bash test_deploy_smoke.sh
```

Клиенты: `https://<домен>/mcp?token=<токен>&scene=<job>` (или заголовок
`Authorization: Bearer`). Рекомендуемый вход для наружного деплоя —
`TOI_REMOTE_MODE=1`: клиент присылает фото как base64 (`load_image_data`),
никаких путей на сервере; инструменты с путями при этом исчезают из API.
(Локальный путь-режим: фото в `./toi-data/media/` = `/data/media`.)

Обновление и откат (состояние сцен живёт в `./toi-data`, переживает и то, и другое):
```bash
TOI_TAG=dev-2 docker compose up -d --build   # раскатка (перед раскаткой сохранить старый тег)
docker compose stop && sed -i 's/dev-2/dev-1/' .env && docker compose up -d   # откат
```

Замечания безопасности: TLS terminates Caddy (Let's Encrypt); самого сервера
в наружу не видно (порт только во внутренней сети compose). Токен в query
(`?token=`) попадает в access-логи — для внешних интеграций предпочтителен
заголовок. `TOI_MEDIA_ROOT` уже задан в образе — чтение произвольных файлов
хоста клиентом невозможно.

## Модель состояния
`scene.json` — единственный источник истины; `output.png` — чистая функция от
него (рендер в `render.py`). Каждая операция правит JSON, делает снимок для undo
и перерисовывает картинку целиком. Z-порядок = порядок в `objects` (0 = задний план).

```json
{
  "image": "/abs/photo.png", "width": 1200, "height": 800,
  "effects": [{"kind": "vignette", "strength": 0.5}],
  "objects": [
    {"id": "t1a2b3c", "type": "text", "text": "SALE", "family": "Montserrat",
     "size": 64, "bold": true, "italic": false, "x": 80, "y": 90,
     "anchor": "top-left", "color": "#fff", "angle": 0,
     "outline": {"width": 3, "color": "#000"}, "glow": null,
     "line_spacing": 1.2, "align": "left", "opacity": 1.0,
     "runs": [{"text": "до ", "size": 30}, {"text": "SALE", "bold": true}],
     "shadow": {"dx": 5, "dy": 7, "blur": 10, "color": "#000", "opacity": 0.7}},
    {"id": "s4d5e6f", "type": "shape", "kind": "rounded_rectangle",
     "x": 40, "y": 60, "w": 260, "h": 90, "fill": "#ffd54f",
     "fill_gradient": {"kind": "linear", "from": "#ffd54f",
                       "to": "#e53935", "angle": 90}}
  ]
}
```

Типы объектов (`type` — дискриминатор): `text`, `shape`, `badge`, `callout`, `image`.
Старые v1-сцены (без `effects`) совместимы — новые поля читаются с дефолтами.

**Rich-text runs.** Поле `runs` текста — список сегментов разного стиля на
одной базовой линии: `{text, family?, size?, bold?, italic?, color?}`;
незаданные поля берутся из объекта. `angle` для runs игнорируется (горизонталь
по определению). `update_text(runs=[])` снимает runs.

**Градиенты фигур.** `fill_gradient: {kind: "linear"|"radial", from, to, angle}`.
Linear: `angle=0` — сверху (`from`) вниз (`to`), дальше по часовой; радиальный —
`from` в центре. Работает с любым `rotation` фигуры. `update_shape(fill_gradient={})`
снимает градиент.

## Инструменты (38)
| Группа | Инструменты |
|---|---|
| Шрифты/загрузка | `list_fonts`, `load_image`, `load_image_data` (base64/data-URL, без путей) |
| Текст (A) | `add_text` (angle, outline, line_spacing, align, opacity, **runs**), `update_text` (**runs**), `auto_fit_text`, `measure_text`, `scale_text` |
| Эффекты объекта | `add_shadow`, `add_glow`, `remove_shadow`, `set_opacity` |
| Геометрия (B) | `add_shape` (rectangle/rounded_rectangle/ellipse/regular_polygon, **fill_gradient**), `add_polygon`, `add_arrow`, `update_shape` (**fill_gradient**), `resize_object` |
| Композит (C) | `add_badge`, `add_callout` (хвост up/down/left/right) |
| Наложения (D) | `add_image` (логотип/водяной знак; fit contain/cover, corner_radius) |
| Фото-эффекты (E) | `apply_effect` (brightness/contrast/saturation/sharpness/grayscale/sepia/rotate/flip/resize/crop/pad/tint/vignette/blur_area), `clear_effects` |
| Раскладка (G) | `align_object` (край/центр относительно холста или другого объекта, gap), `align_group` (общая линия left/right/top/bottom), `distribute` (equal_gap/equal_centers/inside_bounds) |
| Состояние (F) | `move_text` (относительный сдвиг), `move_object`/`bring_to_front`/`send_to_back` (z-order), `delete_object`, `clear_objects`, `undo`, `redo`, `get_state`, `set_state`, `render`, `scene_info` (workspace/ключ/путь текущей сцены) |

Каждый мутирующий вызов возвращает `{image_path, state, object}` — клиенту не
нужно хранить свою копию, но `set_state` даёт и обратную связь. Undo-история —
до 25 шагов в памяти процесса.

**Потокобезопасность:** SDK выполняет синхронные инструменты в пуле рабочих
потоков (`anyio.to_thread`), поэтому каждый инструмент целиком (найти →
снимок → изменить → сохранить → отрендерить) выполняется под модульным
`threading.Lock` — параллельные `tools/call` не теряют обновления и не пишут
рваный JSON. Ответы содержат deepcopy состояния (сериализация ответа не читает
живой словарь). Регрессия: `test_concurrency.py`.

## Тест
```bash
.venv/bin/python server.py &
.venv/bin/python test_client.py   # полный e2e-прогон всех групп A–G через HTTP
```

## Шрифты (33)
Montserrat, Inter, Roboto, Open Sans, Lato, Poppins, Source Sans 3, Raleway,
Oswald, Merriweather, Ubuntu, Nunito, Nunito Sans, Playfair Display, Rubik,
Work Sans, Roboto Condensed, PT Sans, Fira Sans, Barlow, DM Sans, Manrope,
Karla, Josefin Sans, Libre Baskerville, Cormorant Garamond, Space Grotesk,
Quicksand, Mulish, Bebas Neue + script/display: Lobster, Pacifico, Caveat.

Pacifico — только латиница. Если у семейства нет italic/bold — Pillow делает
псевдо-стиль на CPU (affine-shear / stroke), так что флаги `bold`/`italic`
работают всегда.
