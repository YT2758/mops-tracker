"""
MOPS Tracker — 公開資訊觀測站重大訊息追蹤機器人
資料來源：鉅亨網 (cnyes.com) — 彙整 MOPS 重大訊息，可從雲端 IP 存取

環境變數（GitHub Secrets）:
  TELEGRAM_BOT_TOKEN  — Telegram bot token
  TELEGRAM_CHAT_ID    — 目標 chat/channel ID
  GEMINI_API_KEY      — Google AI Studio API key
"""

import json
import os
import time

import requests
from google import genai
from google.genai import types

# ─── Configuration ────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_API_KEY     = os.environ["GEMINI_API_KEY"]

SEEN_IDS_FILE = "seen_ids.json"
MAX_SEEN_IDS  = 10000

# 鉅亨網新聞分類 → 公司類型標籤對照
# tw_material = 重大訊息 (上市/上櫃)
# shop        = 興櫃
CNYES_CATEGORIES = [
    ("tw_material", "重大訊息"),
    ("tw_announcement", "公告"),
]

CNYES_BASE = "https://news.cnyes.com"
CNYES_NEWS = f"{CNYES_BASE}/api/v3/news/category"

HEADERS = {
    "User-Agent":  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept":      "application/json, text/plain, */*",
    "Referer":     "https://news.cnyes.com/",
    "Origin":      "https://news.cnyes.com",
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


# ─── cnyes.com 抓取 ───────────────────────────────────────────────────────────
def fetch_cnyes(category: str, minutes_back: int = 6) -> list[dict]:
    """
    抓取鉅亨網最近 N 分鐘的台股公告新聞。
    回傳 raw item 列表。
    """
    now      = int(time.time())
    start_ts = now - (minutes_back * 60)

    url    = f"{CNYES_NEWS}/{category}"
    params = {"startAt": start_ts, "endAt": now, "limit": 30}

    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=30)
        print(f"  [{category}] HTTP {r.status_code}, url={r.url}")

        if r.status_code != 200:
            print(f"  [{category}] error body: {r.text[:200]}")
            return []

        data = r.json()
        # 回應結構：{"items": {"data": [...], "total": N}}
        items = (data.get("items") or {}).get("data") or []
        print(f"  [{category}] got {len(items)} items")
        return items

    except Exception as e:
        print(f"  [{category}] fetch error: {e}")
        return []


def parse_cnyes(raw_items: list[dict], category_label: str) -> list[dict]:
    """將鉅亨網的 raw item 轉成統一格式。"""
    result = []
    for item in raw_items:
        try:
            news_id   = str(item.get("newsId") or item.get("_id") or "")
            title     = item.get("title", "").strip()
            publish_at = item.get("publishAt", 0)          # Unix timestamp
            url       = item.get("url") or f"{CNYES_BASE}/news/id/{news_id}"
            summary   = item.get("summary") or item.get("body") or ""

            # 股票資訊（可能是 list）
            stocks    = item.get("stocks") or []
            if stocks:
                stock  = stocks[0]
                code   = str(stock.get("symbol") or stock.get("stockId") or "")
                name   = stock.get("name") or ""
            else:
                code, name = "", ""

            # 過濾沒有標題的項目
            if not title or not news_id:
                continue

            # 格式化時間
            t        = time.localtime(publish_at)
            date_str = time.strftime("%Y/%m/%d", t)
            time_str = time.strftime("%H:%M", t)

            result.append({
                "id":         f"cnyes_{news_id}",
                "date":       date_str,
                "time":       time_str,
                "code":       code,
                "name":       name if name else title[:10],
                "title":      title,
                "link":       url if url.startswith("http") else CNYES_BASE + url,
                "summary":    summary[:1000],   # 鉅亨有時直接提供摘要
                "type_label": category_label,
            })
        except Exception as e:
            print(f"  parse error: {e}, item={str(item)[:100]}")

    return result


# ─── Gemma / Gemini 摘要 ──────────────────────────────────────────────────────
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
                config=types.GenerateContentConfig(max_output_tokens=400),
            )
            return resp.text.strip()
        except Exception as e:
            print(f"  [{model_name}] error: {e}")
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
        print(f"  telegram error: {e}")


def fmt_msg(ann: dict, summary: str) -> str:
    code_part = f"（{ann['code']}）" if ann["code"] else ""
    link      = ann["link"]
    return (
        f"📢 <b>{ann['title']}</b>\n\n"
        f"🏢 {ann['name']}{code_part}\n"
        f"🏷 #{ann['type_label']}\n"
        f"⏰ {ann['date']} {ann['time']}\n\n"
        f"{summary}\n\n"
        f'🔗 <a href="{link}">查看原文</a>'
    )


# ─── Main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    seen      = load_seen()
    new_count = 0

    for category, label in CNYES_CATEGORIES:
        print(f"Checking {label} ({category})…")
        raw   = fetch_cnyes(category)
        items = parse_cnyes(raw, label)
        print(f"  {len(items)} announcements parsed")

        for ann in items:
            if ann["id"] in seen:
                continue

            print(f"  [NEW] {ann['name']} — {ann['title']}")
            seen.add(ann["id"])
            new_count += 1

            # 如果鉅亨已有摘要就直接用，否則才呼叫 Gemma
            content = ann.get("summary") or ""
            if len(content) < 50:
                summary = summarize(ann["title"], content, ann["name"], ann["type_label"])
            else:
                summary = "📋 " + content

            send_tg(fmt_msg(ann, summary))
            time.sleep(1.5)

    save_seen(seen)
    print(f"Done. {new_count} new announcement(s) sent.")


if __name__ == "__main__":
    main()
