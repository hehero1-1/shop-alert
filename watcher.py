"""새 상품·새 글 알림 봇.

watchlist.toml 에 적힌 페이지를 확인해서 처음 보는 상품이나 글이 있으면 텔레그램으로 알려준다.
실행할 때마다 텔레그램에서 봇에게 보낸 명령어(/add, /list 등)도 함께 처리한다.
GitHub Actions 가 10분마다 실행한다 (.github/workflows/watch.yml).

환경 변수
  TELEGRAM_BOT_TOKEN  BotFather 에게 받은 봇 토큰. 없으면 알림을 화면에 출력만 한다.
  TELEGRAM_CHAT_ID    알림을 받을 대화 id. 없으면 봇에게 말을 건 사람에게 id 를 알려준다.
"""

import html
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_PATH = os.path.join(HERE, "watchlist.toml")
STATE_PATH = os.path.join(HERE, "seen.json")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
MAX_SEEN = 2000    # 페이지마다 기억해 둘 항목 수
MAX_ALERTS = 10    # 한 페이지에서 한 번에 하나씩 보낼 알림 수 (넘으면 개수만 알림)
FAIL_ALERT = 6     # 이만큼 연속으로 확인에 실패하면 알림 (10분 간격이면 약 1시간)

# 진짜 글이 아닌 항목. 어미새의 등급 제한 글은 처음엔 제목이 "N분 뒤 전체 공개로 전환됩니다" 로
# 보이고, 광고 자리는 "list_adsense" 같은 이름으로 보인다. 이런 항목은 없는 셈 치고,
# 등급 제한 글은 공개된 뒤에 진짜 제목으로 알린다.
NOT_A_POST = re.compile(r"전체\s*공개로\s*전환|^[a-z0-9]+(?:_[a-z0-9]+)+$")

COMMANDS = [
    ["list", "감시 중인 페이지 보기"],
    ["add", "페이지 추가: /add 주소 [키워드]"],
    ["remove", "감시 그만하기: /remove 번호"],
    ["name", "이름 바꾸기: /name 번호 이름"],
    ["kw", "키워드 바꾸기: /kw 번호 [키워드]"],
    ["help", "도움말"],
]

HELP = """사용할 수 있는 명령어

/list - 감시 중인 페이지 보기
/add 주소 [키워드…] - 페이지 추가
    키워드를 쓰면 제목에 그 단어가 있을 때만 알려요.
/remove 번호 - 감시 그만하기
/name 번호 이름 - 이름 바꾸기
/kw 번호 [키워드…] - 키워드 바꾸기 (키워드 없이 쓰면 필터 해제)

주소만 보내도 /add 로 처리해요.
명령어는 다음 확인 때(최대 10분 뒤) 처리돼요."""

WATCHLIST_HEADER = """\
# 감시할 페이지 목록. [[watch]] 하나가 페이지 하나예요.
#
#   name     = 알림에 보일 이름
#   url      = 페이지 주소
#   keywords = ["나이키", "아식스"]   (선택) 제목에 이 단어가 있을 때만 알림
#   type     = 확인 방법 (rss / cafe24 / eomisae / jsonld / links). 비워 두면 자동으로 채워져요.
#   pattern  = (type 이 links 일 때만) 상품 링크 주소에 들어 있는 글자. 예: "/goods/"
#
# 텔레그램 봇 명령어(/add, /remove, /name, /kw)로 바꾸면 이 파일이 다시 쓰이면서
# 여기 직접 적은 다른 주석은 지워져요.
"""


# ---------------------------------------------------------------- 페이지 읽기

def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=20) as res:
        return res.read(), res.headers.get_content_charset()


def decode(body, charset):
    for encoding in (charset, "utf-8", "cp949"):
        if not encoding:
            continue
        try:
            return body.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            pass
    return body.decode("utf-8", "replace")


def clean(fragment):
    """HTML 조각에서 숨김 요소와 태그를 지우고 글자만 남긴다."""
    fragment = re.sub(r'<(\w+)[^>]*class="[^"]*displaynone[^"]*"[^>]*>.*?</\1>', " ", fragment, flags=re.S)
    text = html.unescape(re.sub(r"<[^>]+>", " ", fragment))
    return re.sub(r"\s+", " ", text).strip()


def local(tag):
    return tag.rsplit("}", 1)[-1]


def is_feed(body):
    head = body[:2000].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return head.startswith((b"<?xml", b"<rss", b"<feed")) and (b"<rss" in head or b"<feed" in head)


def parse_feed(body, url):
    """RSS / Atom 피드에서 글 목록을 뽑는다."""
    items = []
    for node in ET.fromstring(body).iter():
        if local(node.tag) not in ("item", "entry"):
            continue
        fields = {}
        for child in node:
            name = local(child.tag)
            if name == "link" and child.get("href"):
                fields.setdefault("link", child.get("href"))
            elif child.text and child.text.strip():
                fields.setdefault(name, child.text.strip())
        link = urllib.parse.urljoin(url, fields.get("link", ""))
        item_id = fields.get("guid") or fields.get("id") or link
        if item_id:
            items.append({"id": item_id, "title": clean(fields.get("title", "")), "link": link})
    return items


def parse_cafe24(text, url):
    """Cafe24 쇼핑몰 상품 목록에서 상품을 뽑는다. 상품마다 anchorBoxId_<상품번호> 가 붙어 있다."""
    items, numbers = [], set()
    for block in re.split(r'(?=<\w+[^>]*\bid="anchorBoxId_\d+")', text)[1:]:
        block = block[:6000]
        number = re.search(r"anchorBoxId_(\d+)", block).group(1)
        if number in numbers:
            continue
        numbers.add(number)

        href = re.search(r'href="([^"]*/product/[^"]*)"', block)
        name = re.search(r'<(p|strong|div)[^>]*class="(?:nm|name)\b[^"]*"[^>]*>(.*?)</\1>', block, re.S)
        name = clean(name.group(2)).lstrip(": ") if name else ""
        if not name:
            alt = re.search(r'<img[^>]+alt="([^"]*)"', block)
            name = clean(alt.group(1)) if alt else ""

        price = re.search(r"([\d,]{3,})\s*원", clean(block))
        if price:
            price = price.group(1) + "원"
        else:
            raw = re.search(r'ec-data-price="(\d+)"', block)
            price = f"{int(raw.group(1)):,}원" if raw else ""

        items.append({
            "id": number,
            "title": name,
            "price": price,
            "link": urllib.parse.urljoin(url, html.unescape(href.group(1))) if href else url,
        })
    return items


def cafe24_category(text):
    """Cafe24 페이지 위쪽의 '홈 > 브랜드 > RRL' 경로에서 마지막 분류 이름을 꺼낸다."""
    path = re.search(r"xans-product-headcategory(.*?)</ol>", text, re.S)
    names = re.findall(r'<a href="/category/[^"]*"[^>]*>(.*?)</a>', path.group(1)) if path else []
    return clean(names[-1]) if names else ""


def walk(data):
    yield data
    children = data.values() if isinstance(data, dict) else data if isinstance(data, list) else []
    for child in children:
        yield from walk(child)


def parse_jsonld(text, url):
    """페이지에 들어 있는 구조화 데이터(JSON-LD)의 목록(ItemList)에서 항목을 뽑는다."""
    items, links = [], set()
    for raw in re.findall(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", text, re.S):
        try:
            data = json.loads(raw, strict=False)
        except ValueError:
            continue
        for node in walk(data):
            if not isinstance(node, dict) or not isinstance(node.get("itemListElement"), list):
                continue
            if node.get("@type") == "BreadcrumbList":
                continue  # '홈 > 게시판' 같은 위치 표시라 목록이 아니다
            for element in node["itemListElement"]:
                if not isinstance(element, dict):
                    continue
                entry = element.get("item", element)
                if isinstance(entry, str):
                    entry = {"url": entry}
                link = entry.get("url") or element.get("url")
                if not isinstance(link, str) or not link:
                    continue
                link = urllib.parse.urljoin(url, link)
                if link in links:
                    continue
                links.add(link)
                title = entry.get("headline") or entry.get("name") or element.get("name") or ""
                items.append({"id": link, "title": clean(str(title)), "link": link})
    return items


def parse_links(text, url, pattern):
    """주소가 pattern(정규식)에 맞는 링크를 모두 항목으로 본다."""
    items, index = [], {}
    for href, inner in re.findall(r'<a\s[^>]*?href="([^"#]+)"[^>]*>(.*?)</a>', text, re.S):
        link = urllib.parse.urljoin(url, html.unescape(href))
        if not re.search(pattern, link):
            continue
        title = clean(inner)
        if link in index:
            if title and not index[link]["title"]:
                index[link]["title"] = title
            continue
        index[link] = {"id": link, "title": title, "link": link}
        items.append(index[link])
    return items


def eomisae_board(url):
    """어미새 게시판 주소(https://eomisae.co.kr/os)에서 게시판 이름(os)을 꺼낸다. 어미새가 아니면 None."""
    if not host(url).endswith("eomisae.co.kr"):
        return None
    parts = urllib.parse.urlsplit(url)
    board = re.fullmatch(r"/([A-Za-z0-9_]+)/?", parts.path)
    return board.group(1) if board else dict(urllib.parse.parse_qsl(parts.query)).get("mid")


def parse_eomisae(text, url):
    """어미새 게시판 목록에서 글을 뽑는다. RSS 가 잠긴 게시판(패션정보 등)에 쓴다.

    공지와 '인기' 고정글은 위쪽 표에 따로 있고 새로고침할 때마다 바뀌므로,
    그 아래 카드 목록(card_wrap)의 일반 글만 본다.
    """
    start = text.find('class="card_wrap"')
    return parse_links(text[start:] if start >= 0 else text, url, rf"/{eomisae_board(url)}/\d+$")


def parse(watch, body, charset):
    url, kind = watch["url"], watch["type"]
    if kind == "rss":
        items = parse_feed(body, url)
    elif kind == "cafe24":
        items = parse_cafe24(decode(body, charset), url)
    elif kind == "eomisae":
        items = parse_eomisae(decode(body, charset), url)
    elif kind == "jsonld":
        items = parse_jsonld(decode(body, charset), url)
    elif kind == "links":
        items = parse_links(decode(body, charset), url, watch.get("pattern") or ".")
    else:
        raise ValueError(f"모르는 type 이에요: {kind}")
    return [item for item in items if not NOT_A_POST.search(item["title"])]


def fetch_items(watch):
    return parse(watch, *fetch(watch["url"]))


def page_title(text):
    title = re.search(r"<title[^>]*>(.*?)</title>", text, re.S)
    return clean(title.group(1)) if title else ""


def feed_title(body):
    for node in ET.fromstring(body).iter():
        if local(node.tag) == "title" and node.text:
            return clean(node.text)
    return ""


def host(url):
    return urllib.parse.urlsplit(url).netloc


def with_query(url, **params):
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True) + list(params.items())
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def resolve(url):
    """주소만 보고 확인 방법(type)과 이름을 정한다. (감시 항목, 지금 있는 항목들) 을 돌려준다."""
    body, charset = fetch(url)
    if is_feed(body):
        watch = {"name": feed_title(body) or host(url), "url": url, "type": "rss"}
        return watch, parse(watch, body, charset)

    text = decode(body, charset)
    title = page_title(text) or host(url)
    if "anchorBoxId_" in text:
        # Cafe24 쇼핑몰은 신상품순(sort_method=5)으로 봐야 새 상품이 첫 페이지에 나온다.
        if "sort_method=" not in url:
            url = with_query(url, sort_method="5")
        category = cafe24_category(text)
        name = title if category in title else f"{title} {category}"
        watch = {"name": name, "url": url, "type": "cafe24"}
        return watch, fetch_items(watch)

    for tag in re.findall(r"<link[^>]+application/(?:rss|atom)\+xml[^>]*>", text):
        href = re.search(r'href="([^"]+)"', tag)
        if not href:
            continue
        watch = {"name": title, "url": urllib.parse.urljoin(url, html.unescape(href.group(1))), "type": "rss"}
        try:
            items = fetch_items(watch)
        except Exception:
            continue
        if items:
            return watch, items

    if eomisae_board(url):
        watch = {"name": title, "url": url, "type": "eomisae"}
        items = parse(watch, body, charset)
        if items:
            return watch, items

    watch = {"name": title, "url": url, "type": "jsonld"}
    items = parse(watch, body, charset)
    if items:
        return watch, items
    raise ValueError("상품이나 글 목록을 찾지 못했어요")


# ---------------------------------------------------------------- 기록 파일

def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        state = {}
    state.setdefault("telegram_offset", 0)
    state.setdefault("pages", {})
    return state


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
        f.write("\n")


def load_watchlist():
    with open(WATCHLIST_PATH, "rb") as f:
        entries = tomllib.load(f).get("watch", [])
    watches = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("url"):
            continue
        if isinstance(entry.get("keywords"), str):
            entry["keywords"] = entry["keywords"].split()
        watches.append(entry)
    return watches


def toml_string(value):
    value = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + re.sub(r"[\x00-\x1f\x7f]", lambda m: f"\\u{ord(m.group()):04x}", value) + '"'


def save_watchlist(watches):
    parts = [WATCHLIST_HEADER]
    for watch in watches:
        lines = ["[[watch]]"]
        for key in ("name", "url", "type", "pattern"):
            if watch.get(key):
                lines.append(f"{key} = {toml_string(watch[key])}")
        if watch.get("keywords"):
            lines.append("keywords = [" + ", ".join(map(toml_string, watch["keywords"])) + "]")
        parts.append("\n".join(lines) + "\n")
    with open(WATCHLIST_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))


# ---------------------------------------------------------------- 텔레그램

def esc(text):
    return html.escape(text, quote=False)


def telegram(method, **params):
    data = urllib.parse.urlencode(params).encode()
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, data=data, timeout=30) as res:
                return json.load(res)
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 2:
                raise
            time.sleep(json.load(e).get("parameters", {}).get("retry_after", 5))


def send(text, chat_id=None):
    if not TOKEN:
        print("[텔레그램]", text, "", sep="\n")
        return
    telegram("sendMessage", chat_id=chat_id or CHAT_ID, text=text, parse_mode="HTML")


def register_commands(state):
    """텔레그램 입력창에서 / 를 누르면 명령어 목록이 보이게 등록한다."""
    if state.get("commands") == COMMANDS:
        return
    telegram("setMyCommands", commands=json.dumps(
        [{"command": c, "description": d} for c, d in COMMANDS], ensure_ascii=False))
    state["commands"] = COMMANDS


def handle_messages(watches, state):
    """봇에게 온 명령어를 처리한다. 감시 목록이 바뀌면 True."""
    updates = telegram("getUpdates", offset=state["telegram_offset"], timeout=0,
                       allowed_updates='["message"]').get("result", [])
    changed = False
    for update in updates:
        state["telegram_offset"] = update["update_id"] + 1
        message = update.get("message") or {}
        chat = str(message.get("chat", {}).get("id", ""))
        text = (message.get("text") or "").strip()
        if not chat or not text:
            continue
        if not CHAT_ID:
            send(f"이 대화의 chat id 는 <code>{chat}</code> 이에요.\n"
                 "GitHub 저장소의 Secrets 에 TELEGRAM_CHAT_ID 로 등록해 주세요.", chat)
            continue
        if chat != CHAT_ID:
            continue  # 다른 사람이 보낸 명령은 무시한다
        reply, did_change = run_command(text, watches, state)
        changed |= did_change
        send(reply)
    return changed


def pick(args, watches):
    if args and args[0].isdigit() and 1 <= int(args[0]) <= len(watches):
        return int(args[0]) - 1
    return None


def list_text(watches):
    if not watches:
        return "감시 중인 페이지가 없어요. /add 주소 로 추가해 보세요."
    lines = [f"📋 감시 중인 페이지 ({len(watches)}개)"]
    for number, watch in enumerate(watches, 1):
        lines.append(f"\n{number}. <b>{esc(watch.get('name') or watch['url'])}</b>\n{esc(watch['url'])}")
        if watch.get("keywords"):
            lines.append("키워드: " + esc(", ".join(watch["keywords"])))
    return "\n".join(lines)


def run_command(text, watches, state):
    """명령어 하나를 처리한다. (답장, 감시 목록이 바뀌었는지) 를 돌려준다."""
    words = text.split()
    command, args = words[0].split("@")[0].lower(), words[1:]
    if command.startswith(("http://", "https://")):
        command, args = "/add", words

    if command == "/list":
        return list_text(watches), False

    if command == "/add":
        if not args or not args[0].startswith(("http://", "https://")):
            return "사용법: /add 주소 [키워드…]", False
        url, keywords = args[0], args[1:]
        if any(w["url"] == url for w in watches):
            return "이미 감시 중인 주소예요. /list 로 확인해 보세요.", False
        try:
            watch, items = resolve(url)
        except Exception as e:
            return f"이 페이지는 자동으로 확인할 수 없었어요.\n({esc(str(e))})\n{esc(url)}", False
        if any(w["url"] == watch["url"] for w in watches):
            return "이미 감시 중인 주소예요. /list 로 확인해 보세요.", False
        if keywords:
            watch["keywords"] = keywords
        watches.append(watch)
        state["pages"][watch["url"]] = {"seen": [item["id"] for item in items][:MAX_SEEN], "fails": 0}
        number = len(watches)
        reply = f"✅ {number}번으로 추가했어요: <b>{esc(watch['name'])}</b>\n"
        if keywords:
            reply += f"키워드: {esc(', '.join(keywords))}\n"
        reply += (f"지금 있는 {len(items)}개는 건너뛰고, 다음부터 새로 올라오는 것만 알려드릴게요.\n"
                  f"이름 바꾸기: /name {number} 새이름")
        return reply, True

    if command in ("/remove", "/name", "/kw"):
        index = pick(args, watches)
        if index is None:
            usage = {"/remove": "/remove 번호", "/name": "/name 번호 이름", "/kw": "/kw 번호 [키워드…]"}
            return f"사용법: {usage[command]}\n번호는 /list 에서 확인할 수 있어요.", False
        watch = watches[index]
        if command == "/remove":
            watches.pop(index)
            state["pages"].pop(watch["url"], None)
            return f"🗑 그만 볼게요: <b>{esc(watch.get('name') or watch['url'])}</b>", True
        if command == "/name":
            if len(args) < 2:
                return "사용법: /name 번호 이름", False
            watch["name"] = " ".join(args[1:])
            return f"✏️ {index + 1}번 이름을 <b>{esc(watch['name'])}</b> 로 바꿨어요.", True
        if len(args) < 2:
            watch.pop("keywords", None)
            return f"{index + 1}번은 이제 키워드 없이 새 항목을 모두 알려드릴게요.", True
        watch["keywords"] = args[1:]
        return f"🔎 {index + 1}번은 이제 제목에 {esc(', '.join(watch['keywords']))} 가 있을 때만 알려드릴게요.", True

    return HELP, False


# ---------------------------------------------------------------- 확인과 알림

def fill_missing(watches):
    """watchlist.toml 에 직접 추가해서 type 이 비어 있는 항목의 확인 방법을 채운다."""
    changed = False
    for watch in watches:
        if watch.get("type"):
            continue
        try:
            resolved, _ = resolve(watch["url"])
        except Exception as e:
            print(f"[실패] {watch['url']}: 확인 방법을 정하지 못했어요 ({e})")
            continue
        watch["type"], watch["url"] = resolved["type"], resolved["url"]
        watch.setdefault("name", resolved["name"])
        changed = True
    return changed


def check(watch, state):
    name, url = watch.get("name") or watch["url"], watch["url"]
    page = state["pages"].setdefault(url, {"fails": 0})
    try:
        items = fetch_items(watch)
        if not items:
            raise ValueError("상품이나 글을 하나도 찾지 못했어요")
    except Exception as e:
        page["fails"] = page.get("fails", 0) + 1
        print(f"[실패] {name}: {e}")
        if page["fails"] == FAIL_ALERT:
            send(f"⚠️ <b>{esc(name)}</b> 을(를) {FAIL_ALERT}번 연속으로 확인하지 못했어요.\n"
                 f"({esc(str(e))})\n{esc(url)}")
        return
    if page.get("fails", 0) >= FAIL_ALERT:
        send(f"✅ <b>{esc(name)}</b> 다시 정상적으로 확인하고 있어요.")
    page["fails"] = 0

    if "seen" not in page:
        page["seen"] = [item["id"] for item in items][:MAX_SEEN]
        send(f"👀 감시 시작: <b>{esc(name)}</b>\n"
             f"지금 있는 {len(items)}개는 건너뛰고, 다음부터 새로 올라오는 것만 알려드릴게요.")
        return

    seen = set(page["seen"])
    unseen = [item for item in items if item["id"] not in seen]
    if not unseen:
        print(f"[확인] {name}: 새 항목 없음")
        return
    page["seen"] = ([item["id"] for item in unseen] + page["seen"])[:MAX_SEEN]

    # 목록은 최신순이라 새 항목은 위쪽에 생긴다. 이미 본 항목보다 아래에 나타난 것은
    # 앞의 글·상품이 지워지면서 밀려 올라온 예전 항목이라 알리지 않는다.
    if watch["type"] != "links":
        position = {item["id"]: i for i, item in enumerate(items)}
        last_seen = max((position[item["id"]] for item in items if item["id"] in seen), default=len(items))
        unseen = [item for item in unseen if position[item["id"]] < last_seen]

    keywords = [k.lower() for k in watch.get("keywords") or []]
    if keywords:
        unseen = [item for item in unseen if any(k in item["title"].lower() for k in keywords)]
    print(f"[확인] {name}: 새 항목 {len(unseen)}개")

    for item in reversed(unseen[:MAX_ALERTS]):
        lines = [f"🆕 <b>{esc(name)}</b>", esc(item["title"] or "(제목 없음)")]
        if item.get("price"):
            lines.append(esc(item["price"]))
        lines.append(esc(item["link"]))
        send("\n".join(lines))
    if len(unseen) > MAX_ALERTS:
        send(f"…<b>{esc(name)}</b> 에 새 항목이 {len(unseen) - MAX_ALERTS}개 더 있어요.\n{esc(url)}")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    state = load_state()
    try:
        watches = load_watchlist()
    except (OSError, tomllib.TOMLDecodeError) as e:
        problem = f"watchlist.toml 을 읽지 못했어요: {e}"
        print(problem)
        if TOKEN and CHAT_ID and state.get("watchlist_error") != problem:
            send("⚠️ " + esc(problem))
            state["watchlist_error"] = problem
            save_state(state)
        return
    state.pop("watchlist_error", None)

    changed = False
    if TOKEN:
        register_commands(state)
        changed |= handle_messages(watches, state)
        if not CHAT_ID:
            print("TELEGRAM_CHAT_ID 가 없어서 명령어 답장만 하고 확인은 건너뛰어요.")
            save_state(state)
            return

    changed |= fill_missing(watches)
    for watch in watches:
        if watch.get("type"):
            check(watch, state)

    urls = {watch["url"] for watch in watches}
    for url in list(state["pages"]):
        if url not in urls:
            del state["pages"][url]
    save_state(state)
    if changed:
        save_watchlist(watches)


if __name__ == "__main__":
    main()
