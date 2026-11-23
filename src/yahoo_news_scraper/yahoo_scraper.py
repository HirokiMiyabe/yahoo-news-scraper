"""
Yahoo!ニュースの記事・コメント収集モジュール。

Yahoo!ニュースの各ページは HTML 内に ``window.__PRELOADED_STATE__`` という
JSON を埋め込んでおり、記事・コメントの情報はすべてそこに入っている。
自動生成の CSS クラス名（``sc-xxxx``）はデプロイごとに変わるため使わず、
この JSON を読むことで安定して取得する。

返信は 返信API → Selenium → コメント個別ページ（先頭10件のみ）の順に
試してフォールバックする（CommentScraper の説明を参照）。
Selenium はフォールバック時にだけ使うので、無くても動く
（``pip install yahoo-news-scraper[selenium]`` で追加）。

構成（処理の流れ）::

    YahooNewsScraper          検索クエリ → 記事URL一覧
    ArticleInfoScraper        記事URL → 記事情報（1行）
    CommentScraper            記事URL → コメント（親コメント＋返信）
    YahooNewsAndCommentScraper  上記を順に実行して 2 つの DataFrame を返す

出力は 2 つのテーブルに分け、``article_id`` で結合できる::

    articles_df  : 1行 = 1記事      （主キー article_id）
    comments_df  : 1行 = 1コメント  （主キー comment_id, 外部キー article_id,
                                     返信は parent_comment_id で親を参照）

    merged = comments_df.merge(articles_df, on="article_id", how="left")

フラグ列（is_*, has_*）はすべて 0/1 の整数。
"""

from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://news.yahoo.co.jp"
JST = timezone(timedelta(hours=9), "JST")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

_STATE_RE = re.compile(r"window\.__PRELOADED_STATE__\s*=\s*(\{.*?\})\s*</script>", re.S)
_ARTICLE_ID_RE = re.compile(r"/articles/([0-9a-f]{40})")
_HIGHLIGHT_RE = re.compile(r"[\u0002\u0003]")  # 検索結果の強調マーカー

ARTICLE_COLUMNS = [
    "article_id", "query", "search_rank", "url", "title",
    "article_type", "is_expert_article", "is_paid",
    "media_id", "media_name", "author_id", "author_name", "author_title",
    "category", "sub_category", "published_at", "updated_at",
    "n_pages", "body", "body_length",
    "reaction_info", "reaction_clear", "reaction_newpov",
    "comments_enabled", "comment_total", "comment_parent_total",
    "comments_collected", "scraped_at",
]

COMMENT_COLUMNS = [
    "comment_id", "article_id", "parent_comment_id",
    "is_reply", "is_expert", "has_reply", "reply_count", "replies_collected", "reply_source",
    "reply_to", "user_id", "user_name", "expert_id", "expert_title", "expert_label",
    "text", "empathy_count", "insight_count", "negative_count", "thumbs_up_count",
    "is_diversity_comment",
    "posted_raw", "posted_at", "posted_at_precision",
    "page", "position", "permalink", "scraped_at",
]

# 0/1 フラグ・カウント列（欠損を許す整数型にする）
_ARTICLE_INT_COLUMNS = [
    "search_rank", "is_expert_article", "is_paid", "n_pages", "body_length",
    "reaction_info", "reaction_clear", "reaction_newpov",
    "comments_enabled", "comment_total", "comment_parent_total", "comments_collected",
]
_COMMENT_INT_COLUMNS = [
    "is_reply", "is_expert", "has_reply", "reply_count", "replies_collected",
    "empathy_count", "insight_count", "negative_count", "thumbs_up_count",
    "is_diversity_comment", "page", "position",
]


# ----------------------------------------------------------------------------
# ユーティリティ
# ----------------------------------------------------------------------------

def extract_article_id(url: str) -> str | None:
    """記事URLから 40 桁の記事ID（contentId）を取り出す。"""
    m = _ARTICLE_ID_RE.search(url)
    return m.group(1) if m else None


def normalize_article_url(url: str) -> str:
    """``?page=2`` や ``/comments`` などを取り除いた記事URLにする。"""
    url = url.split("?")[0].split("#")[0].rstrip("/")
    if url.endswith("/comments"):
        url = url[: -len("/comments")]
    return url


def flag(value: Any) -> int:
    """真偽値っぽいものを 0/1 にする。"""
    return 1 if value else 0


def parse_iso_datetime(value: str | None) -> datetime | None:
    """'2026-11-21T22:00:00.000Z' → JST の datetime。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(JST)
    except ValueError:
        return None


def parse_display_datetime(text: str | None, now: datetime | None = None) -> datetime | None:
    """
    画面表示形式の日時を JST の datetime にする。

    対応形式: '11/22(日) 7:00', '2024/8/2(金) 11:54', '11/22 7:00'
    年が無い場合は ``now`` の年を補い、未来になるなら前年とみなす。
    """
    if not text:
        return None
    now = now or datetime.now(JST)
    cleaned = re.sub(r"\(.*?\)", "", text).strip()
    m = re.match(r"(?:(\d{4})/)?(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})", cleaned)
    if not m:
        return None
    year, month, day, hour, minute = m.groups()
    dt = datetime(int(year or now.year), int(month), int(day), int(hour), int(minute), tzinfo=JST)
    if year is None and dt > now + timedelta(days=1):
        dt = dt.replace(year=dt.year - 1)
    return dt


def parse_comment_date(text: str | None, now: datetime | None = None) -> tuple[datetime | None, str | None]:
    """
    コメントの投稿日時表示を (推定日時, 精度) にする。

    Yahoo!コメントは '1日前' '5時間前' '10分前' 'たった今' のような相対表示が多い。
    相対表示は丸められているため、精度を併せて返す::

        'たった今' / 'N秒前' → 'minute'
        'N分前'             → 'minute'
        'N時間前'           → 'hour'
        'N日前'             → 'day'
        '11/22(日) 7:00'    → 'exact'
    """
    if not text:
        return None, None
    now = now or datetime.now(JST)
    text = text.strip()

    if text in ("たった今", "今"):
        return now.replace(second=0, microsecond=0), "minute"

    m = re.search(r"(\d+)\s*(秒|分|時間|日|週間|か月|ヶ月|年)前", text)
    if m:
        # 「N単位前」は取得時刻から N 単位を引いた値（切り捨てはしない）。
        # 実際の投稿時刻はそこから最大 1 単位さかのぼる可能性がある。
        n, unit = int(m.group(1)), m.group(2)
        delta, precision = {
            "秒": (timedelta(seconds=n), "minute"),
            "分": (timedelta(minutes=n), "minute"),
            "時間": (timedelta(hours=n), "hour"),
            "日": (timedelta(days=n), "day"),
            "週間": (timedelta(weeks=n), "week"),
            "か月": (timedelta(days=30 * n), "month"),
            "ヶ月": (timedelta(days=30 * n), "month"),
            "年": (timedelta(days=365 * n), "year"),
        }[unit]
        return (now - delta).replace(second=0, microsecond=0), precision

    dt = parse_display_datetime(text, now)
    if dt is not None:
        return dt, "exact"

    m = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})$", text)
    if m:
        return datetime(*map(int, m.groups()), tzinfo=JST), "day"

    return None, None


def parse_count(text: str | None) -> int | None:
    """'共感した 1.3万' や '1,234' のような表示を整数にする。"""
    if not text:
        return None
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(万)?", text)
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    return int(round(value * 10000)) if m.group(2) else int(value)


def _to_int_columns(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    for col in columns:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
    return df


def _html_to_text(html: str | None) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text("\n").strip()


# ----------------------------------------------------------------------------
# HTTP 基底クラス
# ----------------------------------------------------------------------------

class _YahooClient:
    """
    requests.Session を持つ共通基底クラス。

    Args:
        delay: リクエスト間の待機秒数（サーバー負荷を避けるため 1 秒以上推奨）
        max_retries: 5xx / 429 / 通信エラー時の再試行回数
        timeout: 1 リクエストのタイムアウト秒数
        session: 共有したい requests.Session（省略時は新規作成）
        verbose: 進捗を print するか
    """

    def __init__(
        self,
        delay: float = 1.0,
        max_retries: int = 3,
        timeout: float = 20.0,
        session: requests.Session | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        verbose: bool = True,
    ):
        self.delay = delay
        self.max_retries = max_retries
        self.timeout = timeout
        self.verbose = verbose
        self.session = session or requests.Session()
        self.session.headers.setdefault("Accept-Language", "ja,en;q=0.8")
        self.session.headers["User-Agent"] = user_agent
        self._last_request = 0.0

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg)

    def _throttle(self) -> None:
        """前回のリクエストから delay 秒経つまで待つ。"""
        wait = self.delay - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _get(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        return_client_error: bool = False,
    ) -> requests.Response | None:
        """
        待機・再試行付き GET。404 や再試行切れのときは None。
        ``return_client_error=True`` なら 4xx（404 以外）のレスポンスもそのまま返す。
        """
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                res = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
            except requests.RequestException as e:
                self._log(f"[retry {attempt + 1}] 通信エラー: {e} ({url})")
                time.sleep(2 ** attempt)
                continue
            if res.status_code == 200:
                return res
            if res.status_code == 404:
                return None
            if res.status_code in (429, 500, 502, 503, 504):
                self._log(f"[retry {attempt + 1}] HTTP {res.status_code} ({url})")
                time.sleep(2 ** (attempt + 1))
                continue
            if return_client_error:
                return res
            self._log(f"HTTP {res.status_code}: {url}")
            return None
        self._log(f"取得失敗（再試行上限）: {url}")
        return None

    def get_state(self, url: str, params: dict | None = None) -> dict | None:
        """ページを取得し、埋め込み JSON（__PRELOADED_STATE__）を dict で返す。"""
        res = self._get(url, params=params)
        if res is None:
            return None
        m = _STATE_RE.search(res.text)
        if not m:
            self._log(f"__PRELOADED_STATE__ が見つかりません: {url}")
            return None
        return json.loads(m.group(1))

    def get_json(self, url: str, params: dict | None = None) -> dict | None:
        res = self._get(url, params=params)
        return res.json() if res is not None else None


# ----------------------------------------------------------------------------
# 1. 検索 → 記事URL一覧
# ----------------------------------------------------------------------------

class YahooNewsScraper(_YahooClient):
    """Yahoo!ニュース検索から記事のタイトルと URL を集めるクラス。"""

    SEARCH_FEED_API = f"{BASE_URL}/api/public/search-feed"
    PAGE_SIZE = 50

    def title_url_scraper(self, word: str, max_articles: int | None = None) -> pd.DataFrame:
        """
        検索結果の記事一覧を取得する（画面の「もっと見る」と同じ API を使う）。

        Args:
            word: 検索クエリ（例: "VIVANT ゲイ"）
            max_articles: 取得上限（None なら全件）

        Returns:
            DataFrame: article_id, query, search_rank, url, title, article_type,
                       media_id, media_name, published_at
        """
        rows: list[dict] = []
        seen: set[str] = set()
        start = 1
        now = datetime.now(JST)

        while True:
            data = self.get_json(self.SEARCH_FEED_API, params={
                "start": start, "query": word, "categories": "", "qrw": 0,
                "results": self.PAGE_SIZE, "ei": "utf-8",
            })
            if not data:
                break
            contents = data.get("contents") or []
            for item in contents:
                url = item.get("permalink")
                article_id = extract_article_id(url or "")
                if not article_id or article_id in seen:  # 広告枠などは permalink が無い
                    continue
                seen.add(article_id)
                headline = (item.get("highlightSearchText") or {}).get("headline", "")
                media = item.get("media") or {}
                pt = item.get("publishTime") or {}
                rows.append({
                    "article_id": article_id,
                    "query": word,
                    "search_rank": len(rows) + 1,
                    "url": url,
                    "title": _HIGHLIGHT_RE.sub("", headline),
                    "article_type": "expert" if "/expert/" in url else "news",
                    "media_id": media.get("id"),
                    "media_name": media.get("name"),
                    "published_at": parse_display_datetime(f"{pt.get('date', '')} {pt.get('time', '')}", now),
                })
                if max_articles and len(rows) >= max_articles:
                    break

            total = data.get("totalResults", 0)
            next_start = data.get("nextStart")
            self._log(f"検索 '{word}': {len(rows)} 件取得 / 全 {total} 件")
            if (max_articles and len(rows) >= max_articles) or not contents or not next_start \
                    or data.get("isLimit") or next_start > total:
                break
            start = next_start

        return pd.DataFrame(rows, columns=[
            "article_id", "query", "search_rank", "url", "title", "article_type",
            "media_id", "media_name", "published_at",
        ])


# ----------------------------------------------------------------------------
# 2. 記事URL → 記事情報
# ----------------------------------------------------------------------------

class ArticleInfoScraper(_YahooClient):
    """個別記事ページから基本情報と本文を取得するクラス。"""

    def article_info_scrape(self, url: str) -> dict:
        """
        記事情報を 1 行分の dict で返す（キーは ARTICLE_COLUMNS の一部）。

        通常記事（/articles/...）とエキスパート記事（/expert/articles/...）の両方に対応。
        複数ページの記事は全ページの本文を連結する。
        """
        url = normalize_article_url(url)
        self._log(f"記事情報を取得中: {url}")
        state = self.get_state(url)
        row: dict[str, Any] = {
            "article_id": extract_article_id(url),
            "url": url,
            "scraped_at": datetime.now(JST),
        }
        if state is None:
            return row
        if "expertArticle" in state:
            row.update(self._parse_expert_article(state))
        elif "articleDetail" in state:
            row.update(self._parse_news_article(state, url))
        else:
            self._log(f"未知のページ形式です: {url}")
        return row

    # --- 通常記事 -----------------------------------------------------------
    def _parse_news_article(self, state: dict, url: str) -> dict:
        ad = state["articleDetail"]
        media = ad.get("media") or {}
        reactions = (state.get("articleReaction") or {}).get("articleReactions") or {}
        n_pages = int(ad.get("maxPage") or 1)

        bodies = [self._paragraphs_to_text(ad.get("paragraphs") or [])]
        for page in range(2, n_pages + 1):
            st = self.get_state(url, params={"page": page})
            if not st or "articleDetail" not in st:
                break
            bodies.append(self._paragraphs_to_text(st["articleDetail"].get("paragraphs") or []))
        body = "\n\n".join(b for b in bodies if b)

        is_paid = any(bool(ad.get(k)) for k in ("isPay", "isPaid", "pay", "paid"))
        return {
            "article_id": ad.get("contentId"),
            "title": ad.get("headline"),
            "article_type": "news",
            "is_expert_article": 0,
            "is_paid": flag(is_paid),
            "media_id": media.get("mediaId"),
            "media_name": media.get("mediaName"),
            "category": ad.get("category"),
            "sub_category": ad.get("subCategory"),
            "published_at": parse_iso_datetime(ad.get("createDateTime")),
            "updated_at": parse_iso_datetime(ad.get("updateDateTime")),
            "n_pages": n_pages,
            "body": body,
            "body_length": len(body),
            "reaction_info": reactions.get("info"),
            "reaction_clear": reactions.get("clear"),
            "reaction_newpov": reactions.get("newpov"),
            "comments_enabled": flag(ad.get("commentPermission") and ad.get("isCommentVisible", True)),
        }

    @staticmethod
    def _paragraphs_to_text(paragraphs: list[dict]) -> str:
        parts: list[str] = []
        for p in paragraphs:
            if p.get("head"):
                parts.append(p["head"])
            for td in p.get("textDetails") or []:
                text = (td.get("text") or "").strip()
                # 同じ段落テキストが重複して入っていることがあるので除く
                if text and (not parts or parts[-1] != text):
                    parts.append(text)
        return "\n\n".join(parts)

    # --- エキスパート記事 ---------------------------------------------------
    def _parse_expert_article(self, state: dict) -> dict:
        ea = state["expertArticle"]
        author = state.get("expertAuthorForArticleDetail") or {}
        reactions = (state.get("articleReaction") or {}).get("articleReactions") or {}
        pub = ea.get("publishedAt") or {}
        body = _html_to_text(ea.get("body"))
        comment = ea.get("comment") or {}
        return {
            "article_id": ea.get("contentId"),
            "title": (ea.get("title") or {}).get("text"),
            "article_type": "expert",
            "is_expert_article": 1,
            "is_paid": flag(ea.get("paid")),
            "media_id": author.get("id"),
            "media_name": author.get("name"),
            "author_id": author.get("id"),
            "author_name": author.get("name"),
            "author_title": author.get("title"),
            "category": (state.get("articleReaction") or {}).get("categoryId"),
            "published_at": parse_display_datetime(f"{pub.get('date', '')} {pub.get('time', '')}"),
            "n_pages": 1,
            "body": body,
            "body_length": len(body),
            "reaction_info": reactions.get("info"),
            "reaction_clear": reactions.get("clear"),
            "reaction_newpov": reactions.get("newpov"),
            "comments_enabled": flag(comment.get("permission") and not comment.get("isTerminated")),
        }


# ----------------------------------------------------------------------------
# 3. 記事URL → コメント
# ----------------------------------------------------------------------------

class CommentScraper(_YahooClient):
    """
    記事のコメント（エキスパートコメント・一般コメント・返信）を集めるクラス。

    返信の取得方法（``reply_methods`` の順に試し、失敗したら次へ）:

        "api"        Yahoo!ニュースの返信 API。コメントページに埋め込まれた
                     userToken をヘッダに付けて 10 件ずつ全件取得する（既定・最速）。
        "selenium"   ブラウザでコメント個別ページを開き「もっと見る」を押し切って
                     全件読み込み、DOM から取り出す（selenium と Chrome が必要）。
        "permalink"  コメント個別ページに埋め込まれた返信を読む。
                     先頭 10 件までしか取れない最終手段。

    真の返信数は親コメントの ``reply_count``、実際に取れた件数は
    ``replies_collected``、使われた方法は ``reply_source`` に入る。

    Args:
        reply_methods: 返信取得方法の優先順
        headless: Selenium を画面なしで動かすか
        その他は _YahooClient と同じ
    """

    REPLY_API = BASE_URL + "/api/comment/properties/{property_id}/articles/{article_id}/comments/{comment_id}/reply"
    REPLY_PAGE_SIZE = 10
    _MORE_BUTTON_RE = re.compile(r"もっと見る\s*[（(]\s*\d+\s*件")

    def __init__(
        self,
        *args,
        reply_methods: tuple[str, ...] = ("api", "selenium", "permalink"),
        headless: bool = True,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        unknown = set(reply_methods) - {"api", "selenium", "permalink"}
        if unknown:
            raise ValueError(f"未知の reply_methods: {unknown}")
        self.reply_methods = tuple(reply_methods)
        self.headless = headless
        self._user_token: str | None = None
        self._token_source_url: str | None = None
        self._driver = None
        self._disabled_methods: set[str] = set()

    # --- 後片付け -------------------------------------------------------------
    def close(self) -> None:
        """Selenium のブラウザを閉じる。"""
        if self._driver is not None:
            try:
                self._driver.quit()
            finally:
                self._driver = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- コメント本体 ---------------------------------------------------------
    def comment_scrape(
        self,
        url: str,
        fetch_replies: bool = True,
        max_pages: int | None = None,
        order: str = "newer",
    ) -> tuple[pd.DataFrame, dict]:
        """
        記事のコメントを全ページ取得する。

        Args:
            url: 記事URL（/articles/... または /expert/articles/...）
            fetch_replies: 返信も取得するか
            max_pages: 取得するコメントページ数の上限（1ページ = 親コメント10件）
            order: "newer"（新着順・推奨）または "recommended"（おすすめ順）。
                   取得中に新規投稿があっても、新着順なら取りこぼしが出にくい
                   （重複は comment_id で除去する）。

        Returns:
            (comments_df, meta)
            meta: {"article_id", "comment_total", "comment_parent_total", "is_paid",
                   "comments_collected", "reply_sources"}
        """
        article_url = normalize_article_url(url)
        article_id = extract_article_id(article_url)
        comment_url = f"{article_url}/comments"
        params_base = {"order": "newer"} if order == "newer" else {}
        self._token_source_url = comment_url

        rows: list[dict] = []
        seen: set[str] = set()
        meta: dict[str, Any] = {"article_id": article_id, "reply_sources": {}}
        page = 1
        n_pages = None

        try:
            while True:
                if max_pages and page > max_pages:
                    break
                state = self.get_state(comment_url, params={**params_base, "page": page})
                if state is None or "commentFull" not in state:
                    break  # 最終ページの次は commentFull 自体が無い
                if isinstance(state.get("userToken"), str):
                    self._user_token = state["userToken"]  # ページごとに新しいトークンに更新
                now = datetime.now(JST)
                cf = state["commentFull"]

                if page == 1:
                    ca = state.get("commentArticle") or {}
                    meta.update({
                        "comment_total": ca.get("parentAndReplyCommentTotalCount"),
                        "comment_parent_total": cf.get("parentOnlyCommentTotalCount"),
                        "is_paid": flag(ca.get("pay")),
                    })
                    n_pages = math.ceil((cf.get("parentOnlyCommentTotalCount") or 0) / 10)
                    self._log(f"コメント取得開始: {article_id} 親コメント {meta['comment_parent_total']} 件"
                              f"（返信込み {meta['comment_total']} 件, {n_pages} ページ）")

                    # エキスパートコメントは 1 ページ目にのみ表示される
                    for pos, c in enumerate(cf.get("commentatorComment") or [], start=1):
                        if c.get("commentId") in seen:
                            continue
                        seen.add(c.get("commentId"))
                        rows.append(self._expert_row(c, article_id, page, pos, now))

                user_comments = cf.get("userCommentList") or []
                if not user_comments:
                    break
                for pos, c in enumerate(user_comments, start=1):
                    cid = c.get("commentId")
                    if cid in seen:
                        continue
                    seen.add(cid)
                    parent = self._user_row(c, article_id, page, pos, now)
                    rows.append(parent)
                    if fetch_replies and parent["reply_count"]:
                        replies, source = self.reply_scrape(c, article_id)
                        replies = [r for r in replies if r["comment_id"] not in seen]
                        seen.update(r["comment_id"] for r in replies)
                        parent["replies_collected"] = len(replies)
                        parent["reply_source"] = source
                        meta["reply_sources"][source] = meta["reply_sources"].get(source, 0) + 1
                        rows.extend(replies)

                if page % 10 == 0:
                    self._log(f"  {page}/{n_pages} ページ完了（累計 {len(rows)} 件）")
                page += 1
        finally:
            self.close()

        df = pd.DataFrame(rows, columns=COMMENT_COLUMNS)
        df = _to_int_columns(df, _COMMENT_INT_COLUMNS)
        meta["comments_collected"] = len(df)
        self._log(f"コメント取得完了: {article_id} {len(df)} 件 返信の取得方法: {meta['reply_sources']}")
        return df, meta

    # --- 返信 -----------------------------------------------------------------
    def reply_scrape(self, comment: dict, article_id: str | None = None) -> tuple[list[dict], str | None]:
        """
        親コメント（コメントページの userCommentList の要素）の返信を取得する。

        ``reply_methods`` の順に試し、最初に成功した方法の結果を返す。

        Returns:
            (返信行のリスト, 使った方法名)  すべて失敗したら ([], None)
        """
        cid = comment.get("commentId")
        for method in self.reply_methods:
            if method in self._disabled_methods:
                continue
            try:
                replies = getattr(self, f"_replies_via_{method}")(comment, article_id)
            except Exception as e:
                self._log(f"返信取得失敗（{method}）: {cid} ({type(e).__name__}: {e})")
                replies = None
            if replies is not None:
                for r in replies:
                    r["reply_source"] = method
                return replies, method
            self._log(f"返信取得を次の方法に切り替えます（{method} 失敗）: {cid}")
        return [], None

    def _refresh_token(self) -> bool:
        if not self._token_source_url:
            return False
        state = self.get_state(self._token_source_url)
        token = (state or {}).get("userToken")
        if isinstance(token, str) and token:
            self._user_token = token
            return True
        return False

    def _replies_via_api(self, comment: dict, article_id: str | None) -> list[dict] | None:
        """返信 API で全件取得。失敗時は None。"""
        if not self._user_token and not self._refresh_token():
            return None
        cid = comment["commentId"]
        aid = comment.get("commentArticleId") or article_id
        url = self.REPLY_API.format(
            property_id=comment.get("propertyId") or "news_user", article_id=aid, comment_id=cid)

        rows: list[dict] = []
        seen: set[str] = set()
        start, refreshed = 1, False
        total = int((comment.get("reply") or {}).get("totalResults") or 0)
        while True:
            res = self._get(
                url,
                params={"start": start, "results": self.REPLY_PAGE_SIZE, "sort": "recommendation"},
                headers={"UserToken": self._user_token},
                return_client_error=True,
            )
            if res is None:
                return None
            if res.status_code in (401, 403):  # トークン切れ → 1 回だけ取り直す
                if refreshed or not self._refresh_token():
                    self._log(f"返信 API が HTTP {res.status_code} を返しました: {cid}")
                    return None
                refreshed = True
                continue
            if res.status_code != 200:
                self._log(f"返信 API が HTTP {res.status_code} を返しました: {cid}")
                return None
            data = res.json()
            now = datetime.now(JST)
            items = data.get("comments") or []
            for r in items:
                if r.get("commentId") in seen:
                    continue
                seen.add(r.get("commentId"))
                rows.append(self._reply_row(r, aid, cid, len(rows) + 1, now))
            total = int(data.get("totalResults") or total)
            start += self.REPLY_PAGE_SIZE
            if not items or start > total:
                break
        return rows

    def _get_driver(self):
        if self._driver is None:
            from selenium import webdriver  # 必要になったときだけ読み込む

            opts = webdriver.ChromeOptions()
            if self.headless:
                opts.add_argument("--headless=new")
            for arg in ("--lang=ja", "--disable-dev-shm-usage", "--no-sandbox",
                        f"--user-agent={self.session.headers['User-Agent']}"):
                opts.add_argument(arg)
            self._driver = webdriver.Chrome(options=opts)
        return self._driver

    def _replies_via_selenium(self, comment: dict, article_id: str | None) -> list[dict] | None:
        """ブラウザで「もっと見る」を押し切って全件読み込む。失敗時は None。"""
        permalink = comment.get("permalink")
        if not permalink:
            return None
        try:
            driver = self._get_driver()
        except Exception as e:  # selenium 未インストール / Chrome 起動失敗
            self._log(f"Selenium を使えないため以後スキップします: {type(e).__name__}: {e}")
            self._disabled_methods.add("selenium")
            return None

        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait

        report_sel = 'a[href*="/comment/violation/"][data-cl-params*="_cl_vmodule:rep"]'
        self._throttle()
        driver.get(permalink)
        WebDriverWait(driver, 15).until(lambda d: d.find_elements(By.CSS_SELECTOR, report_sel))

        total = int((comment.get("reply") or {}).get("totalResults") or 0)
        for _ in range(math.ceil(total / self.REPLY_PAGE_SIZE) + 5):
            buttons = [b for b in driver.find_elements(By.TAG_NAME, "button")
                       if self._MORE_BUTTON_RE.search(b.text or "")]
            if not buttons:
                break
            before = len(driver.find_elements(By.CSS_SELECTOR, report_sel))
            self._throttle()
            driver.execute_script("arguments[0].click();", buttons[0])
            try:
                WebDriverWait(driver, 15).until(
                    lambda d: len(d.find_elements(By.CSS_SELECTOR, report_sel)) > before)
            except Exception:
                break

        rows = self._parse_reply_dom(driver.page_source, comment, article_id)
        return rows or None

    def _parse_reply_dom(self, html: str, comment: dict, article_id: str | None) -> list[dict]:
        """コメント個別ページの DOM から返信を取り出す（自動生成クラス名は使わない）。"""
        cid = comment["commentId"]
        aid = comment.get("commentArticleId") or article_id
        now = datetime.now(JST)
        soup = BeautifulSoup(html, "html.parser")
        rows: list[dict] = []
        seen: set[str] = set()
        for a in soup.select('a[href*="/comment/violation/"][data-cl-params*="_cl_vmodule:rep"]'):
            reply_id = parse_qs(urlparse(a["href"]).query).get("cid", [None])[0]
            if not reply_id or reply_id in seen:
                continue
            seen.add(reply_id)
            article = a.find_parent("article")
            box = a
            while box is not None and not box.select_one('button[data-cl-params*="_cl_link:agbtn1;"]'):
                box = box.parent
            if article is None:
                continue

            name_a = article.select_one('a[data-cl-params*="_cl_link:profnm"]')
            time_el = article.find("time")
            text_el = article.find("p")

            def count(link: str) -> int | None:
                btn = box.select_one(f'button[data-cl-params*="_cl_link:{link};"]') if box else None
                return parse_count(btn.get_text(" ", strip=True)) if btn else None

            user_href = name_a.get("href", "") if name_a else ""
            rows.append(self._reply_row({
                "commentId": reply_id,
                "name": name_a.get_text(strip=True) if name_a else None,
                "ppid": user_href.rstrip("/").split("/")[-1] if "/users/" in user_href else None,
                "text": text_el.get_text() if text_el else None,
                "postDate": time_el.get_text(strip=True) if time_el else None,
                "empathyCount": count("agbtn1"),
                "insightCount": count("agbtn2"),
                "negativeCount": count("disbtn1"),
            }, aid, cid, len(rows) + 1, now))
        return rows

    def _replies_via_permalink(self, comment: dict, article_id: str | None) -> list[dict] | None:
        """コメント個別ページの埋め込み JSON から返信を読む（先頭 10 件まで）。"""
        permalink = comment.get("permalink")
        if not permalink:
            return None
        state = self.get_state(permalink)
        if not state or "userComment" not in state:
            return None
        cid = comment["commentId"]
        aid = comment.get("commentArticleId") or article_id
        now = datetime.now(JST)
        replies = ((state.get("userComment") or {}).get("reply") or {}).get("comments") or []
        return [self._reply_row(r, aid, cid, pos, now) for pos, r in enumerate(replies, start=1)]

    # --- 行の組み立て -------------------------------------------------------
    @staticmethod
    def _base_row(c: dict, article_id: str | None, now: datetime) -> dict:
        posted_at, precision = parse_comment_date(c.get("postDate"), now)
        return {
            "comment_id": c.get("commentId"),
            "article_id": c.get("commentArticleId") or article_id,
            "user_id": c.get("ppid"),
            "user_name": c.get("name"),
            "text": c.get("text"),
            "posted_raw": c.get("postDate"),
            "posted_at": posted_at,
            "posted_at_precision": precision,
            "scraped_at": now,
        }

    def _expert_row(self, c: dict, article_id, page: int, pos: int, now: datetime) -> dict:
        commentator = c.get("commentator") or {}
        row = self._base_row(c, article_id, now)
        row.update({
            "parent_comment_id": None,
            "is_reply": 0,
            "is_expert": 1,
            "has_reply": 0,
            "reply_count": 0,
            "replies_collected": 0,
            "expert_id": commentator.get("id"),
            "expert_title": commentator.get("title"),
            "expert_label": c.get("label"),
            "thumbs_up_count": c.get("thumbsUpCount"),
            "is_diversity_comment": 0,
            "page": page,
            "position": pos,
            "permalink": c.get("permalink"),
        })
        return row

    def _user_row(self, c: dict, article_id, page: int, pos: int, now: datetime) -> dict:
        reply_count = int((c.get("reply") or {}).get("totalResults") or 0)
        row = self._base_row(c, article_id, now)
        row.update({
            "parent_comment_id": None,
            "is_reply": 0,
            "is_expert": 0,
            "has_reply": flag(reply_count > 0),
            "reply_count": reply_count,
            "replies_collected": 0,
            "empathy_count": c.get("empathyCount"),
            "insight_count": c.get("insightCount"),
            "negative_count": c.get("negativeCount"),
            "is_diversity_comment": flag(c.get("isDiversityComment")),
            "page": page,
            "position": pos,
            "permalink": c.get("permalink"),
        })
        return row

    def _reply_row(self, r: dict, article_id, parent_comment_id: str, pos: int, now: datetime) -> dict:
        row = self._base_row(r, article_id, now)
        row.update({
            "parent_comment_id": parent_comment_id,
            "is_reply": 1,
            "is_expert": 0,
            "has_reply": 0,
            "reply_count": 0,
            "replies_collected": 0,
            "reply_to": r.get("replyTo") or None,
            "empathy_count": r.get("empathyCount"),
            "insight_count": r.get("insightCount"),
            "negative_count": r.get("negativeCount"),
            "is_diversity_comment": flag(r.get("isDiversityComment")),
            "position": pos,
        })
        return row


# ----------------------------------------------------------------------------
# 4. まとめて実行
# ----------------------------------------------------------------------------

class YahooNewsAndCommentScraper(YahooNewsScraper, ArticleInfoScraper, CommentScraper):
    """検索 → 記事情報 → コメント を順に実行し、記事表とコメント表を返すクラス。"""

    def scrape(
        self,
        word: str,
        max_articles: int | None = None,
        paid_remove: bool = True,
        fetch_replies: bool = True,
        max_comment_pages: int | None = None,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """
        Args:
            word: 検索クエリ
            max_articles: 対象記事数の上限
            paid_remove: 有料記事を除外するか
            fetch_replies: 返信も取得するか
            max_comment_pages: 1記事あたりのコメントページ上限（1ページ=親コメント10件）

        Returns:
            (articles_df, comments_df)  ``article_id`` で結合可能
        """
        search_df = self.title_url_scraper(word, max_articles=max_articles)
        return self.scrape_urls(
            search_df["url"].tolist(),
            search_df=search_df,
            paid_remove=paid_remove,
            fetch_replies=fetch_replies,
            max_comment_pages=max_comment_pages,
        )

    def scrape_urls(
        self,
        urls: list[str],
        search_df: pd.DataFrame | None = None,
        paid_remove: bool = True,
        fetch_replies: bool = True,
        max_comment_pages: int | None = None,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """検索を経由せず、記事URLのリストから直接収集する。"""
        search_info = {}
        if search_df is not None and not search_df.empty:
            search_info = search_df.set_index("article_id")[["query", "search_rank"]].to_dict("index")

        article_rows: list[dict] = []
        comment_frames: list[pd.DataFrame] = []
        for i, url in enumerate(urls, start=1):
            self._log(f"===== [{i}/{len(urls)}] {url}")
            try:
                article = self.article_info_scrape(url)
                article.update(search_info.get(article.get("article_id"), {}))
                if paid_remove and article.get("is_paid"):
                    self._log("有料記事のためスキップ")
                    continue
                if article.get("comments_enabled", 1):
                    comments, meta = self.comment_scrape(
                        url, fetch_replies=fetch_replies, max_pages=max_comment_pages)
                    article["is_paid"] = max(article.get("is_paid") or 0, meta.get("is_paid") or 0)
                    for k in ("comment_total", "comment_parent_total", "comments_collected"):
                        article[k] = meta.get(k)
                    if paid_remove and article["is_paid"]:
                        self._log("有料記事のためスキップ")
                        continue
                    comment_frames.append(comments)
                else:
                    article.update(comment_total=0, comment_parent_total=0, comments_collected=0)
                article_rows.append(article)
            except Exception as e:  # 1記事の失敗で全体を止めない
                self._log(f"エラーのためスキップ: {url} ({type(e).__name__}: {e})")

        articles_df = pd.DataFrame(article_rows).reindex(columns=ARTICLE_COLUMNS)
        articles_df = _to_int_columns(articles_df, _ARTICLE_INT_COLUMNS)
        frames = [f for f in comment_frames if not f.empty]
        comments_df = (pd.concat(frames, ignore_index=True) if frames
                       else pd.DataFrame(columns=COMMENT_COLUMNS))
        comments_df = _to_int_columns(comments_df, _COMMENT_INT_COLUMNS)
        return articles_df, comments_df
