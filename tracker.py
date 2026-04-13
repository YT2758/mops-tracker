"""
MOPS Tracker — 公開資訊觀測站重大訊息追蹤機器人
每次執行掃描四類公司最新公告，過濾已見過的，
用 Gemma 摘要後推播到 Telegram。

環境變數（GitHub Secrets）:
  TELEGRAM_BOT_TOKEN  — Telegram bot token
  TELEGRAM_CHAT_ID    — 目標 chat/channel ID
  GEMINI_API_KEY      — Google AI Studio API key（可用 Gemma 模型）
"""

import json
import os
import time

import requests
from bs4 import BeautifulSoup
from google import genai
from google.genai import types

# ─── Configuration ────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_API_KEY     = os.environ["GEMINI_API_KEY"]

SEEN_IDS_FILE = "seen_ids.json"
MAX_SEEN_IDS  = 10000

COMPANY_TYPES = {
    "sii":  "上市公司",
    "otc":  "上櫃公司",
    "rotc": "興櫃公司",
    "pub":  "公開發行公司",
}

MOPS_BASE = "https://mops.twse.com.tw"
LIST_URL  = f"{MOPS_BASE}/mops/web/ajax_t05sr01"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer":         "https://mops.twse.com.tw/mops/web/index",
    "Origin":          "https://mops.twse.com.tw",
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

# ─── Seen IDs ─────────────────────────────────────────────────────────────────
def load_seen() -> set:
    if os.path.exists(SEEN_IDS_FILE):
        with open(SEEN_IDS_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen: set) -> None:
    lst = list(seen)
    if len(lst) > MAX_SEEN_IDS:
        lst = lst[-MAX_SEEN_IDS:]
    with open(SEEN_IDS_FILE, "w", encoding="utf-8") as f:
        json.dump(lst, f, ensure_ascii=False)


# ─── MOPS Fetch & Parse ───────────────────────────────────────────────────────
def get_session() -> requests.Session:
    """建立帶有 MOPS session cookie 的 requests.Session。"""
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        session.get(f"{MOPS_BASE}/mops/web/index", timeout=15)
    except Exception:
        pass
    return session


def fetch_list(session: requests.Session, typek: str) -> str:
    payload = {
        "encodeURIComponent": "1",
        "step":     "1",
        "firstin":  "1",
        "off":      "1",
        "keyword4": "",
        "code1":    "",
        "TYPEK":    typek,
        "year":     "",
        "month":    "",
        "day":      "",
        "seq_no":   "",
        "b_date":   "",
        "e_date":   "",
        "id":       "",
        "KEY_WORD": "",
    }
    try:
        r = session.post(LIST_URL, data=payload, timeout=30)
        r.encoding = "utf-8"
        html = r.text

        # 偵測是否被擋
        if "PAGE CANNOT BE ACCESSED" in html:
            print(f"[{typek}] ❌ MOPS 封鎖此 IP（security error）")
            print(f"[{typek}] HTML preview: {html[:300]}")
            return ""

        return html
    except Exception as e:
        print(f"[{typek}] fetch error: {e}")
        return ""


def _clean(tag) -> str:
    return tag.get_text(" ", strip=True) if tag else ""


def parse_list(html: str, typek: str) -> list[dict]:
    items = []
    if not html:
        return items

    soup = BeautifulSoup(html, "lxml")

    # 印出前 300 字幫助診斷（只在找不到表格時）
    table = soup.find("table", class_=lambda c: c and "hasBorder" in c)
    if not table:
        tables = soup.find_all("table")
        table = max(tables, key=lambda t: len(t.find_all("tr")), default=None)
    if not table:
        print(f"[{typek}] no table found. HTML preview: {html[:300]}")
        return items

    rows = table.find_all("tr")
    for row in rows[1:]:
        cols = row.find_all("td")
        if len(cols) < 4:
            continue
        try:
            date     = _clean(cols[0])
            time_s   = _clean(cols[1]) if len(cols) > 1 else ""
            code     = _clean(cols[2]) if len(cols) > 2 else ""
            name     = _clean(cols[3]) if len(cols) > 3 else ""
            title_td = cols[4] if len(cols) > 4 else cols[3]
            title    = _clean(title_td)

            if not date or not code:
                continue

            a    = title_td.find("a") or row.find("a")
            href = a["href"] if a and a.get("href") else ""
            if href and not href.startswith("http"):
                href = MOPS_BASE + href if href.startswith("/") else f"{MOPS_BASE}/mops/web/{href}"

            items.append({
                "id":         f"{typek}_{date}_{time_s}_{code}",
                "date":       date,
                "time":       time_s,
                "code":       code,
                "name":       name,
                "title":      title,
                "link":       href,
                "typek":      typek,
                "type_label": COMPANY_TYPES[typek],
            })
        except Exception as e:
            print(f"[{typek}] parse row error: {e}")

    return items


def fetch_content(session: requests.Session, url: str) -> str:
    if not url:
        return ""
    try:
        r = session.get(url, timeout=30)
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "lxml")
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        for selector in ["pre", ".content", "#content", "article"]:
            el = soup.select_one(selector)
            if el:
                return el.get_text("\n", strip=True)[:3000]
        return soup.get_text("\n", strip=True)[:3000]
    except Exception as e:
        print(f"content fetch error ({url}): {e}")
        return ""


# ─── Gemma / Gemini Summarize（新版 google-genai SDK）────────────────────────
PREFERRED_MODELS = ["gemma-3-12b-it", "gemma-3-4b-it", "gemini-2.0-flash"]

def summarize(title: str, content: str, name: str, type_label: str) -> str:
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = (
        "請用繁體中文將以下台灣股市公告摘要成 3 至 5 個重點，"
        "每點以「• 」開頭，只列重點不需解釋：\n\n"
        f"公司：{name}（{type_label}）\n"
        f"標題：{title}\n"
        f"內容：{content[:2000] if content else '（無內容）'}"
    )
    for model_name in PREFERRED_MODELS:
        try:
            resp = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(max_output_tokens=512),
            )
            return resp.text.strip()
        except Exception as e:
            print(f"[{model_name}] error: {e}, trying next...")
    return "（摘要暫不可用）"


# ─── Telegram ─────────────────────────────────────────────────────────────────
def send_tg(text: str) -> None:
    if len(text) > 4000:
        text = text[:4000] + "\n…"
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id":                  TELEGRAM_CHAT_ID,
                "text":                     text,
                "parse_mode":               "HTML",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        r.raise_for_status()
    except Exception as e:
        print(f"telegram error: {e}")


def fmt_msg(ann: dict, summary: str) -> str:
    tag  = ann["type_label"].replace("公司", "")
    link = ann["link"] or f"{MOPS_BASE}/mops/#/web/home"
    return (
        f"📢 <b>{ann['title']}</b>\n\n"
        f"🏢 {ann['name']}（{ann['code']}）\n"
        f"🏷 #{tag}\n"
        f"⏰ {ann['date']} {ann['time']}\n\n"
        f"{summary}\n\n"
        f'🔗 <a href="{link}">查看原文</a>'
    )


# ─── Main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    seen      = load_seen()
    new_count = 0
    session   = get_session()

    for typek, label in COMPANY_TYPES.items():
        print(f"Checking {label}…")
        html  = fetch_list(session, typek)
        items = parse_list(html, typek)
        print(f"  {len(items)} announcements fetched")

        for ann in items:
            if ann["id"] in seen:
                continue

            print(f"  [NEW] {ann['name']} — {ann['title']}")
            seen.add(ann["id"])
            new_count += 1

            content = fetch_content(session, ann["link"])
            summary = summarize(ann["title"], content, ann["name"], ann["type_label"])
            send_tg(fmt_msg(ann, summary))
            time.sleep(1.5)

    save_seen(seen)
    print(f"Done. {new_count} new announcement(s) sent.")


if __name__ == "__main__":
    main()
