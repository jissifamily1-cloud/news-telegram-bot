# -*- coding: utf-8 -*-
"""봇 대화형 키워드 뉴스 → 텔레그램 모니터 (서버 없이 GitHub Actions로 동작).

사용자 경험(방 1개 = 3단계):
  1) 텔레그램 방(채널) 생성
  2) 봇 초대 → 관리자 권한
  3) 방에 `/keywords 반도체, 엔비디아` 입력  → 끝

엔진은 실행마다:
  1) getUpdates로 새 메시지 확인 → 방 등록/키워드 명령 처리(config.json 갱신)
  2) 등록된 활성 방마다 뉴스 수집·매칭·중복제거 후 그 방으로 발송

환경변수:
  TELEGRAM_BOT_TOKEN  (필수)
  NAVER_CLIENT_ID / NAVER_CLIENT_SECRET (선택, 없으면 Google News RSS)
  REGISTER_PASSWORD   (선택) 설정 시 방은 `/register <암호>` 후에만 키워드 설정 가능
  DRY_RUN=1           (선택) 발송 생략(테스트)
"""

import json
import os
import re
import sys
import time
import html as html_mod
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

KST = timezone(timedelta(hours=9))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(ROOT, "config.json")
STATE_FILE = os.path.join(ROOT, "state.json")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
NAVER_ID = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
REGISTER_PASSWORD = os.environ.get("REGISTER_PASSWORD", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"

API = "https://api.telegram.org/bot%s/" % BOT_TOKEN

# 기본값 (방마다 명령으로 조정 가능)
DEFAULT_BODY_MATCH = True
DEFAULT_RECENCY_MINUTES = 120
FETCH_COUNT = 30
FETCH_COUNT_NIGHT = 100
MAX_SEEN = 1500
MAX_SEND_PER_RUN = 20
SEND_INTERVAL_SEC = 2.0
NEAR_DUP_MIN_SHARED = 2
NEAR_DUP_HOURS = 6
NEAR_DUP_MAX = 400

BLOCK_DOMAINS = [
    "mydaily.co.kr", "spotvnews.co.kr", "osen.co.kr", "xportsnews.com",
    "sportschosun.com", "starnewskorea.com", "mksports.co.kr", "sportsworldi.com",
    "sportsseoul.com", "isplus.com", "sportalkorea.com", "interfootball.co.kr",
    "stnsports.co.kr", "mhnse.com", "sportsq.co.kr", "gamefocus.co.kr",
    "maniareport.com", "stoo.com", "fomos.kr", "inven.co.kr",
]
BLOCK_URL_KEYWORDS = ["/sports/", "/baseball/", "/esports/", "/game/", "/lck/", "/kbo/", "/lol/", "sports."]

PRESS_MAP = {
    "yna.co.kr": "연합뉴스", "news1.kr": "뉴스1", "newsis.com": "뉴시스",
    "biz.chosun.com": "조선비즈", "chosun.com": "조선일보", "donga.com": "동아일보",
    "joongang.co.kr": "중앙일보", "hani.co.kr": "한겨레", "khan.co.kr": "경향신문",
    "hankookilbo.com": "한국일보", "mk.co.kr": "매일경제", "hankyung.com": "한국경제",
    "sedaily.com": "서울경제", "fnnews.com": "파이낸셜뉴스", "mt.co.kr": "머니투데이",
    "edaily.co.kr": "이데일리", "asiae.co.kr": "아시아경제", "heraldcorp.com": "헤럴드경제",
    "etnews.com": "전자신문", "zdnet.co.kr": "지디넷코리아", "ddaily.co.kr": "디지털데일리",
    "dt.co.kr": "디지털타임스", "inews24.com": "아이뉴스24", "bloter.net": "블로터",
    "yonhapnewstv.co.kr": "연합뉴스TV", "ohmynews.com": "오마이뉴스", "ajunews.com": "아주경제",
    "businesspost.co.kr": "비즈니스포스트", "mtn.co.kr": "MTN", "digitaltoday.co.kr": "디지털투데이",
    "newspim.com": "뉴스핌", "etoday.co.kr": "이투데이", "koreaherald.com": "코리아헤럴드",
}

_STOP_TOKENS = frozenset({
    "속보", "단독", "공식", "종합", "기자", "뉴스", "오늘", "내일", "관련", "위해", "통해",
    "대한", "밝혀", "예정", "이번", "최대", "최초", "그룹", "AI", "추진", "전환", "조직", "으로",
})


# ---------- 공용 ----------

def _http_get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _strip_tags(s):
    return html_mod.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


# ---------- 텔레그램 ----------

def tg_get_updates(offset):
    if not BOT_TOKEN:
        return []
    url = (API + "getUpdates?timeout=0&offset=%d&allowed_updates=%s"
           % (offset, urllib.parse.quote('["message","channel_post","my_chat_member"]')))
    try:
        data = json.loads(_http_get(url))
    except Exception as e:
        print("getUpdates error: %s" % e)
        return []
    return data.get("result", []) if data.get("ok") else []


def tg_send(chat_id, text):
    if DRY_RUN:
        print("  [DRY_RUN] -> %s: %s" % (chat_id, text[:80].replace("\n", " ")))
        return True
    cid = int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id
    payload = json.dumps({"chat_id": cid, "text": text, "parse_mode": "HTML",
                          "disable_web_page_preview": True}).encode("utf-8")
    req = urllib.request.Request(API + "sendMessage", data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        print("  send error %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:120]))
        return False
    except Exception as e:
        print("  send error: %s" % e)
        return False


# ---------- 방 설정(config) ----------

def new_entry(title):
    return {"title": title, "keywords": [], "title_only": [], "block": [], "exclude": [],
            "body_match": DEFAULT_BODY_MATCH, "recency_minutes": DEFAULT_RECENCY_MINUTES,
            "active": False, "authorized": not bool(REGISTER_PASSWORD),
            "registered_at": datetime.now(KST).isoformat()}


def ensure_entry(chats, cid, title):
    if cid not in chats:
        chats[cid] = new_entry(title)
    elif title and not chats[cid].get("title"):
        chats[cid]["title"] = title
    return chats[cid]


def _parse_list(arg):
    arg = arg.replace("\n", ",")
    return [w.strip() for w in arg.split(",") if w.strip()]


HELP = (
    "🤖 <b>뉴스 모니터 봇</b>\n"
    "이 방으로 키워드 관련 뉴스를 보내드립니다.\n\n"
    "<b>키워드 설정</b>\n"
    "/keywords 반도체, 엔비디아, TSMC  — 키워드 새로 설정\n"
    "/add 파운드리  — 키워드 추가\n"
    "/remove TSMC  — 키워드 삭제\n"
    "/list  — 현재 설정 보기\n\n"
    "<b>정밀 조정</b>\n"
    "/block 야구  — 이 단어 있으면 제외(스포츠 등)\n"
    "/titleonly 엔비디아  — 제목에서만 매칭(흔한 단어 폭주 방지)\n"
    "/pause · /resume  — 잠시 멈춤/재개\n"
    "/stop  — 이 방 등록 해제\n"
    "/help  — 도움말"
)


def _summary(e):
    st = "🟢 작동중" if e.get("active") and e.get("keywords") else ("⏸ 멈춤" if not e.get("active") else "⚠️ 키워드 없음")
    lines = ["📋 <b>%s</b>  (%s)" % (e.get("title") or "이 방", st),
             "키워드(%d): %s" % (len(e["keywords"]), ", ".join(e["keywords"]) or "—")]
    if e["title_only"]:
        lines.append("제목만: %s" % ", ".join(e["title_only"]))
    if e["block"]:
        lines.append("제외어: %s" % ", ".join(e["block"]))
    return "\n".join(lines)


def handle_command(chats, chat, text):
    """명령 처리. 응답 텍스트를 즉시 발송."""
    cid = str(chat.get("id"))
    title = chat.get("title") or chat.get("username") or cid
    raw = text.strip()
    head = raw.split(maxsplit=1)
    cmd = head[0].lower()
    if "@" in cmd:
        cmd = cmd.split("@", 1)[0]
    arg = head[1].strip() if len(head) > 1 else ""

    if cmd in ("/start", "/help"):
        tg_send(cid, HELP)
        return
    if cmd == "/register":
        if not REGISTER_PASSWORD:
            tg_send(cid, "별도 등록 없이 바로 /keywords 로 시작하세요.")
        elif arg == REGISTER_PASSWORD:
            ensure_entry(chats, cid, title)["authorized"] = True
            tg_send(cid, "✅ 등록 인증 완료. 이제 /keywords 로 키워드를 설정하세요.")
        else:
            tg_send(cid, "❌ 등록 암호가 올바르지 않습니다.")
        return

    e = ensure_entry(chats, cid, title)
    if REGISTER_PASSWORD and not e.get("authorized"):
        tg_send(cid, "🔒 먼저 <code>/register 암호</code> 로 등록 인증이 필요합니다.")
        return

    if cmd == "/keywords":
        e["keywords"] = _parse_list(arg)
        e["title_only"] = [k for k in e["title_only"] if k in e["keywords"]]
        e["active"] = bool(e["keywords"])
        tg_send(cid, ("✅ 키워드 설정 완료. 새 뉴스가 뜨면 보내드릴게요.\n\n" if e["keywords"]
                      else "키워드가 비었습니다. 예) /keywords 반도체, 엔비디아\n\n") + _summary(e))
    elif cmd == "/add":
        add = [k for k in _parse_list(arg) if k not in e["keywords"]]
        e["keywords"] += add
        if e["keywords"]:
            e["active"] = True
        tg_send(cid, "➕ 추가: %s\n\n%s" % (", ".join(add) or "(없음)", _summary(e)))
    elif cmd == "/remove":
        rm = _parse_list(arg)
        e["keywords"] = [k for k in e["keywords"] if k not in rm]
        e["title_only"] = [k for k in e["title_only"] if k in e["keywords"]]
        if not e["keywords"]:
            e["active"] = False
        tg_send(cid, "➖ 삭제: %s\n\n%s" % (", ".join(rm), _summary(e)))
    elif cmd in ("/block", "/blockword"):
        e["block"] = sorted(set(e["block"]) | set(_parse_list(arg)))
        tg_send(cid, "🚫 제외어 갱신.\n\n%s" % _summary(e))
    elif cmd in ("/unblock",):
        rm = set(_parse_list(arg))
        e["block"] = [b for b in e["block"] if b not in rm]
        tg_send(cid, "제외어 해제.\n\n%s" % _summary(e))
    elif cmd in ("/titleonly", "/title_only"):
        for k in _parse_list(arg):
            if k not in e["keywords"]:
                e["keywords"].append(k)
            if k not in e["title_only"]:
                e["title_only"].append(k)
        e["active"] = bool(e["keywords"])
        tg_send(cid, "🎯 제목전용 갱신.\n\n%s" % _summary(e))
    elif cmd == "/list":
        tg_send(cid, _summary(e))
    elif cmd == "/pause":
        e["active"] = False
        tg_send(cid, "⏸ 멈췄습니다. /resume 으로 재개.")
    elif cmd == "/resume":
        e["active"] = bool(e["keywords"])
        tg_send(cid, "▶️ 재개." if e["active"] else "키워드가 없어 재개할 수 없어요. /keywords 먼저.")
    elif cmd == "/stop":
        chats.pop(cid, None)
        tg_send(cid, "🗑 이 방 등록을 해제했습니다. 다시 쓰려면 /keywords 로 시작하세요.")
    # 알 수 없는 명령은 무시(채널 일반 글 보호)


def process_updates(chats, state):
    offset = int(state.get("offset", 0))
    updates = tg_get_updates(offset)
    changed = False
    for u in updates:
        offset = max(offset, u["update_id"] + 1)
        # 봇이 방에 추가/관리자됨 → 자동 등록 + 환영
        if "my_chat_member" in u:
            mcm = u["my_chat_member"]
            chat = mcm.get("chat", {})
            status = mcm.get("new_chat_member", {}).get("status", "")
            cid = str(chat.get("id"))
            if status in ("administrator", "member", "creator"):
                e = ensure_entry(chats, cid, chat.get("title") or cid)
                changed = True
                tg_send(cid, "👋 추가해 주셔서 감사합니다!\n" +
                        ("키워드를 설정하면 시작합니다. 예) <code>/keywords 반도체, 엔비디아</code>\n\n" if not REGISTER_PASSWORD
                         else "먼저 <code>/register 암호</code> 로 등록 후 /keywords 를 입력하세요.\n\n") + HELP)
            elif status in ("left", "kicked"):
                if cid in chats:
                    chats[cid]["active"] = False
                    changed = True
            continue
        msg = u.get("message") or u.get("channel_post")
        if not msg:
            continue
        text = msg.get("text", "")
        if not text.startswith("/"):
            continue
        handle_command(chats, msg.get("chat", {}), text)
        changed = True
    state["offset"] = offset
    return changed


# ---------- 매칭 (방별 cfg) ----------

def _is_short_ascii(kw):
    return re.match(r"^[A-Za-z0-9]{1,5}$", kw) is not None


def _clean(text, excluded):
    for w in excluded:
        text = text.replace(w, "")
    return text


def _contains(text, kw):
    if _is_short_ascii(kw):
        pat = r"(?<![A-Za-z])" + re.escape(kw) + r"(?![A-Za-z])"
        return re.search(pat, text, re.IGNORECASE) is not None
    return kw in text


def match_keyword(title, desc, e):
    t = _clean(title, e["exclude"])
    if any(b in t for b in e["block"]):
        return None
    hit = next((k for k in e["keywords"] if _contains(t, k)), None)
    if hit or not e.get("body_match", True) or not desc:
        return hit
    b = _clean(desc, e["exclude"])
    if any(x in b for x in e["block"]):
        return None
    to = set(e["title_only"])
    return next((k for k in e["keywords"] if k not in to and _contains(b, k)), None)


def _title_tokens(title, e):
    t = _clean(title, e["exclude"])
    t = re.sub(r"\[[^\]]*\]", " ", t)
    kw_lower = frozenset(k.lower() for k in e["keywords"])
    out = set()
    for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", t):
        if w in _STOP_TOKENS or w.lower() in kw_lower or w.isdigit():
            continue
        out.add(w)
    return out


def _near_dup(tokens, sigs):
    return any(len(tokens & s) >= NEAR_DUP_MIN_SHARED for s in sigs)


# ---------- 수집 ----------

def fetch_naver(query, count):
    url = ("https://openapi.naver.com/v1/search/news.json?query="
           + urllib.parse.quote(query) + "&display=%d&sort=date" % count)
    raw = _http_get(url, {"X-Naver-Client-Id": NAVER_ID, "X-Naver-Client-Secret": NAVER_SECRET})
    out = []
    for it in json.loads(raw).get("items", []):
        title = _strip_tags(it.get("title", ""))
        link = it.get("originallink") or it.get("link", "")
        desc = _strip_tags(it.get("description", ""))
        try:
            pub = parsedate_to_datetime(it.get("pubDate", "")).astimezone(KST)
        except Exception:
            pub = None
        out.append((title, link, pub, "", desc))
    return out


def fetch_google(query, count):
    url = ("https://news.google.com/rss/search?q="
           + urllib.parse.quote(query + " when:1d") + "&hl=ko&gl=KR&ceid=KR:ko")
    raw = _http_get(url)
    out = []
    for m in re.finditer(r"<item>(.*?)</item>", raw, re.S):
        b = m.group(1)
        t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", b, re.S)
        l = re.search(r"<link/?>(.*?)(?:</link>|<)", b, re.S)
        d = re.search(r"<pubDate>(.*?)</pubDate>", b, re.S)
        s = re.search(r"<source[^>]*>(.*?)</source>", b, re.S)
        dsc = re.search(r"<description>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>", b, re.S)
        if not t or not l:
            continue
        title = _strip_tags(t.group(1))
        source = _strip_tags(s.group(1)) if s else ""
        desc = _strip_tags(dsc.group(1)) if dsc else ""
        if source and title.endswith(" - " + source):
            title = title[: -(len(source) + 3)]
        try:
            pub = parsedate_to_datetime(d.group(1)).astimezone(KST) if d else None
        except Exception:
            pub = None
        out.append((title, l.group(1).strip(), pub, source, desc))
    return out[:count]


def fetch_all(keywords, count):
    use_naver = bool(NAVER_ID and NAVER_SECRET)
    arts = []
    for q in keywords:
        try:
            arts += fetch_naver(q, count) if use_naver else fetch_google(q, count)
        except Exception as ex:
            print("    fetch err (%s): %s" % (q, ex))
    return arts, ("naver" if use_naver else "google")


_STRIP_PARAMS = frozenset({"fbclid", "gclid", "ref", "from", "sid", "utm", "page", "mode"})


def _norm_url(u):
    p = urllib.parse.urlsplit(u)
    q = ""
    if p.query:
        kept = [(k, v) for k, v in urllib.parse.parse_qsl(p.query)
                if not k.lower().startswith("utm") and k.lower() not in _STRIP_PARAMS]
        q = urllib.parse.urlencode(kept)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, q, ""))


def _h(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _host(url):
    return urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")


def press_name(url, source=""):
    if source:
        return source
    h = _host(url)
    for d, n in PRESS_MAP.items():
        if h == d or h.endswith("." + d):
            return n
    return h or "기타"


def blocked_url(url):
    h = _host(url)
    if any(h == d or h.endswith("." + d) for d in BLOCK_DOMAINS):
        return True
    return any(k in url.lower() for k in BLOCK_URL_KEYWORDS)


# ---------- 방 1개 발송 ----------

def run_chat(cid, e, st):
    now = datetime.now(KST)
    seen = set(st.get("seen_urls", []))
    seen_t = set(st.get("seen_titles", []))
    last_run = None
    try:
        last_run = datetime.fromisoformat(st.get("last_run", ""))
    except (ValueError, TypeError):
        pass
    cutoff = now - timedelta(minutes=int(e.get("recency_minutes", DEFAULT_RECENCY_MINUTES)))
    if last_run:
        cutoff = min(last_run, cutoff)
    night = last_run is not None and (now - last_run) > timedelta(hours=2)

    arts, src = fetch_all(e["keywords"], FETCH_COUNT_NIGHT if night else FETCH_COUNT)
    sigs = []
    kept = []
    for it in st.get("recent_sigs", []):
        try:
            ts = datetime.fromisoformat(it[0])
        except Exception:
            continue
        if (now - ts) <= timedelta(hours=NEAR_DUP_HOURS):
            kept.append(it)
            sigs.append(set(it[1].split()))
    st["recent_sigs"] = kept[-NEAR_DUP_MAX:]
    first = not st.get("initialized", False)

    def mark(k, tk):
        if k and k not in seen:
            seen.add(k)
            st.setdefault("seen_urls", []).append(k)
        if tk not in seen_t:
            seen_t.add(tk)
            st.setdefault("seen_titles", []).append(tk)

    cand = []
    for title, url, pub, source, desc in arts:
        k = _norm_url(url)
        tk = re.sub(r"\s+", "", title)[:60]
        if (k and k in seen) or tk in seen_t:
            continue
        if (pub and pub < cutoff) or blocked_url(url) or not match_keyword(title, desc, e):
            mark(k, tk)
            continue
        cand.append((title, url, source, desc, k, tk))

    accepted = []
    run_sigs = []
    for (title, url, source, desc, k, tk) in cand:
        toks = _title_tokens(title, e)
        if len(toks) >= NEAR_DUP_MIN_SHARED and (_near_dup(toks, sigs) or _near_dup(toks, run_sigs)):
            mark(k, tk)
            continue
        run_sigs.append(toks)
        accepted.append((title, url, source, desc, k, tk, toks))

    print("  [%s] %s fetched=%d hits=%d" % (e.get("title") or cid, src, len(arts), len(accepted)))
    sent = 0
    try:
        if first:
            for it in accepted:
                mark(it[4], it[5])
        else:
            for (title, url, source, desc, k, tk, toks) in accepted[:MAX_SEND_PER_RUN]:
                nm = press_name(url, source)
                ex = ("\n" + _h(desc[:200]) + ("..." if len(desc) > 200 else "")) if desc else ""
                ok = tg_send(cid, "%s\n<a href=\"%s\">%s</a>%s" % (_h(nm), url, _h(title), ex))
                mark(k, tk)
                if ok:
                    st.setdefault("recent_sigs", []).append([now.isoformat(), " ".join(toks)])
                    sigs.append(toks)
                    sent += 1
                    time.sleep(SEND_INTERVAL_SEC)
    finally:
        st["initialized"] = True
        st["seen_urls"] = st.get("seen_urls", [])[-MAX_SEEN:]
        st["seen_titles"] = st.get("seen_titles", [])[-MAX_SEEN:]
        st["last_run"] = now.isoformat()
    return sent


# ---------- 메인 ----------

def main():
    if not DRY_RUN and not BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN 미설정"); sys.exit(1)
    config = load_json(CONFIG_FILE, {"chats": {}})
    state = load_json(STATE_FILE, {"offset": 0, "chats": {}})
    chats = config.setdefault("chats", {})

    # 1) 새 메시지(등록·명령) 처리
    try:
        process_updates(chats, state)
    except Exception as ex:
        print("process_updates error: %s" % ex)
    save_json(CONFIG_FILE, config)

    # 2) 활성 방마다 발송
    active = [(cid, e) for cid, e in chats.items() if e.get("active") and e.get("keywords")]
    print("registered=%d active=%d" % (len(chats), len(active)))
    for cid, e in active:
        st = state["chats"].setdefault(cid, {})
        try:
            run_chat(cid, e, st)
        except Exception as ex:
            print("  [%s] run error: %s" % (cid, ex))
    # 등록 해제된 방의 상태 정리
    for cid in list(state["chats"].keys()):
        if cid not in chats:
            state["chats"].pop(cid, None)
    save_json(STATE_FILE, state)


if __name__ == "__main__":
    main()
