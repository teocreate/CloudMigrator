"""
Mail.ru Cloud (публичная ссылка) → Яндекс.Диск

Работает в двух режимах:
  Colab       — меняй значения прямо в форме ниже
  GitHub Actions — передаёт значения через secrets/inputs,
                   хардкод ниже используется как fallback
"""

import os as _os, re as _re

# ─── Colab: меняй здесь / GitHub Actions: подставляется из env автоматически ─
YA_LOGIN          = "Fyodor.02@yandex.ru"                          #@param {type:"string"}
YA_APP_PASSWORD   = ""                                             #@param {type:"string"}
YA_OAUTH_TOKEN    = ""                                             #@param {type:"string"}
MAILRU_PUBLIC_URL = "https://cloud.mail.ru/public/Rs3w/mCfhSqGXE"  #@param {type:"string"}
YA_DEST_FOLDER    = ""                                              #@param {type:"string"}
# Оставь YA_DEST_FOLDER пустым — имя папки возьмётся из ссылки автоматически

# env-переменные перекрывают хардкод (нужно для GitHub Actions)
YA_LOGIN          = _os.environ.get("YA_LOGIN",          YA_LOGIN)
YA_APP_PASSWORD   = _os.environ.get("YA_APP_PASSWORD",   YA_APP_PASSWORD)
YA_OAUTH_TOKEN    = _os.environ.get("YA_OAUTH_TOKEN",    YA_OAUTH_TOKEN).strip()
MAILRU_PUBLIC_URL = _os.environ.get("MAILRU_PUBLIC_URL", MAILRU_PUBLIC_URL).strip()
YA_DEST_FOLDER    = _os.environ.get("YA_DEST_FOLDER",    YA_DEST_FOLDER).strip()
# ─────────────────────────────────────────────────────────────────────────────

_match = _re.search(r"cloud\.mail\.ru/public/(.+?)/?$", MAILRU_PUBLIC_URL)
if not _match:
    raise SystemExit("❌ Неверный формат ссылки. Ожидается https://cloud.mail.ru/public/...")
MAILRU_WEBLINK = _match.group(1)
if not YA_DEST_FOLDER:
    YA_DEST_FOLDER = "mailru_" + MAILRU_WEBLINK.replace("/", "_")

# ── Зависимости ───────────────────────────────────────────────────────────────
import subprocess
subprocess.run(["pip", "install", "requests", "-q"])

import requests, os, time
from pathlib import PurePosixPath
from urllib.parse import quote
from requests.auth import HTTPBasicAuth

YA_AUTH        = HTTPBasicAuth(YA_LOGIN, YA_APP_PASSWORD)
YA_REST        = "https://cloud-api.yandex.net/v1/disk"
YA_WEBDAV      = "https://webdav.yandex.ru"
REST_HEADERS   = {"Authorization": f"OAuth {YA_OAUTH_TOKEN}"}

MR = requests.Session()
MR.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

MAX_RETRIES = 4
RETRY_WAIT  = 15
CONNECT_TMO = 30
READ_TMO    = 7200

USE_REST = bool(YA_OAUTH_TOKEN)

# ── Яндекс.Диск: создание папок ───────────────────────────────────────────────
def ya_mkdirs(path):
    parts = path.strip("/").split("/")
    for i in range(1, len(parts) + 1):
        partial = "/".join(parts[:i])
        if USE_REST:
            requests.put(f"{YA_REST}/resources",
                         headers=REST_HEADERS,
                         params={"path": f"disk:/{partial}"},
                         timeout=(CONNECT_TMO, 60))
        else:
            requests.request("MKCOL", f"{YA_WEBDAV}/{partial}",
                             auth=YA_AUTH, timeout=(CONNECT_TMO, 60))

# ── Яндекс.Диск: проверить, существует ли файл ───────────────────────────────
def ya_exists(remote_path):
    if USE_REST:
        r = requests.get(f"{YA_REST}/resources",
                         headers=REST_HEADERS,
                         params={"path": f"disk:/{remote_path.lstrip('/')}"},
                         timeout=(CONNECT_TMO, 30))
        return r.status_code == 200
    else:
        encoded = quote(remote_path.lstrip("/"), safe="/")
        r = requests.request("HEAD", f"{YA_WEBDAV}/{encoded}",
                             auth=YA_AUTH, timeout=(CONNECT_TMO, 30))
        return r.status_code in (200, 204)

# ── Яндекс.Диск: загрузить локальный файл ────────────────────────────────────
def ya_upload_file(remote_path, local_path):
    actual = os.path.getsize(local_path)
    if USE_REST:
        # Получаем presigned URL
        r = requests.get(f"{YA_REST}/resources/upload",
                         headers=REST_HEADERS,
                         params={"path": f"disk:/{remote_path.lstrip('/')}", "overwrite": "true"},
                         timeout=(CONNECT_TMO, 60))
        r.raise_for_status()
        upload_url = r.json()["href"]
        with open(local_path, "rb") as fh:
            r = requests.put(upload_url, data=fh,
                             headers={"Content-Length": str(actual)},
                             timeout=(60, None))  # None = без таймаута на запись
    else:
        encoded = quote(remote_path.lstrip("/"), safe="/")
        with open(local_path, "rb") as fh:
            r = requests.put(f"{YA_WEBDAV}/{encoded}",
                             auth=YA_AUTH, data=fh,
                             headers={"Content-Length": str(actual),
                                      "Content-Type": "application/octet-stream",
                                      "Overwrite": "T"},
                             timeout=(CONNECT_TMO, READ_TMO))
    return r.status_code

# ── Скачать с Mail.ru во временный файл ──────────────────────────────────────
def mr_download(url, tmp):
    with MR.get(url, stream=True, timeout=(CONNECT_TMO, READ_TMO)) as src:
        src.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in src.iter_content(8 * 1024 * 1024):
                fh.write(chunk)

# ── Полный цикл: скачать + загрузить (раздельные ретраи) ─────────────────────
def transfer(remote_path, mr_download_url, size):
    tmp = "/tmp/_mr_transfer"

    # 1. Скачиваем с Mail.ru (с ретраями)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            mr_download(mr_download_url, tmp)
            break
        except Exception as e:
            if attempt == MAX_RETRIES:
                raise
            print(f"\n    ⟳ download {attempt+1}/{MAX_RETRIES} через {RETRY_WAIT}с: {e}", flush=True)
            time.sleep(RETRY_WAIT)

    # 2. Загружаем на Яндекс (с ретраями, без повторного скачивания)
    try:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return ya_upload_file(remote_path, tmp)
            except Exception as e:
                if attempt == MAX_RETRIES:
                    raise
                print(f"\n    ⟳ upload {attempt+1}/{MAX_RETRIES} через {RETRY_WAIT}с: {e}", flush=True)
                time.sleep(RETRY_WAIT)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

# ── Mail.ru: получить сервер загрузки ─────────────────────────────────────────
def mr_dispatcher():
    from urllib.parse import urlparse
    r = MR.get("https://cloud.mail.ru/api/v2/dispatcher",
               params={"api": 2}, timeout=(CONNECT_TMO, 30))
    raw = r.json()["body"]["weblink_get"][0]["url"]
    p = urlparse(raw)
    return f"{p.scheme}://{p.netloc}/"

# ── Mail.ru: список файлов в папке ────────────────────────────────────────────
def mr_list_folder(weblink):
    items, offset = [], 0
    while True:
        r = MR.get("https://cloud.mail.ru/api/v2/folder", params={
            "weblink": weblink, "offset": offset, "limit": 500, "api": 2,
            "sort[type]": "name", "sort[order]": "asc",
        }, timeout=(CONNECT_TMO, 60))
        body  = r.json().get("body", {})
        batch = body.get("list", [])
        if not batch:
            break
        items  += batch
        offset += len(batch)
        count   = body.get("count", 0)
        if isinstance(count, dict):
            count = count.get("total", count.get("value", 0))
        if not batch or offset >= count:
            break
    return items

# ── Mail.ru: рекурсивный сбор всех файлов ────────────────────────────────────
def mr_collect_files(weblink, prefix=""):
    result = []
    for item in mr_list_folder(weblink):
        rel_path = prefix + "/" + item["name"]
        if item["type"] == "file":
            result.append({"weblink": item["weblink"],
                           "path": rel_path, "size": item.get("size", 0)})
        elif item["type"] == "folder":
            result += mr_collect_files(item["weblink"], rel_path)
    return result

# ── MAIN ──────────────────────────────────────────────────────────────────────
print("🔐 Проверка Яндекс.Диска...")
if USE_REST:
    probe = requests.get(f"{YA_REST}/", headers=REST_HEADERS,
                         timeout=(CONNECT_TMO, 30))
    ok = probe.status_code == 200
else:
    probe = requests.request("PROPFIND", f"{YA_WEBDAV}/",
                             auth=YA_AUTH, headers={"Depth": "0"},
                             timeout=(CONNECT_TMO, 60))
    ok = probe.status_code in (200, 207)

if not ok:
    raise SystemExit(f"❌ Яндекс.Диск: ошибка {probe.status_code} — проверь токен/логин")
mode = "REST API (OAuth)" if USE_REST else "WebDAV"
print(f"✅ Авторизация OK  [{mode}]")

print("📡 Получаем сервер загрузки Mail.ru...")
dispatcher = mr_dispatcher()
print(f"   {dispatcher}")

print("📂 Читаем список файлов...")
files = mr_collect_files(MAILRU_WEBLINK)
if not files:
    raise SystemExit("❌ Файлов не найдено — возможно ссылка уже закрыта или API изменился")

total_gb = sum(f["size"] for f in files) / 1024 ** 3
print(f"   Найдено: {len(files)} файлов, ~{total_gb:.2f} ГБ")

ya_mkdirs(YA_DEST_FOLDER)

errors, skipped = [], 0
for i, f in enumerate(files, 1):
    dest_path   = f"{YA_DEST_FOLDER}{f['path']}"
    parent_path = str(PurePosixPath(dest_path).parent)
    mb    = f["size"] / 1024 ** 2
    label = f"[{i:>3}/{len(files)}] {f['path']}  ({mb:.1f} МБ)"

    if ya_exists(dest_path):
        print(f"{label} … ⏭ уже есть")
        skipped += 1
        continue

    ya_mkdirs(parent_path)
    download_url = dispatcher + "weblink/view/" + quote(f["weblink"], safe="/+")

    print(label, end=" … ", flush=True)
    try:
        code = transfer(dest_path, download_url, f["size"])
        if code in (200, 201, 204):
            print("✅")
        else:
            print(f"⚠️  HTTP {code}")
            errors.append(f["path"])
    except Exception as e:
        print(f"❌ {e}")
        errors.append(f["path"])

print(f"\n{'─'*50}")
print(f"🎉 Готово. Успешно: {len(files)-len(errors)-skipped}/{len(files)}  "
      f"(пропущено/уже было: {skipped}, ошибок: {len(errors)})")
print(f"📁 Папка на Яндекс.Диске: /{YA_DEST_FOLDER}")

if errors:
    print(f"\n⚠️  Не перенеслось ({len(errors)}):")
    for e in errors:
        print(f"   {e}")
