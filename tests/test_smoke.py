"""ネットワークを使わない単体テスト。"""

from datetime import datetime

import pandas as pd

from yahoo_news_scraper.yahoo_scraper import (
    COMMENT_COLUMNS,
    JST,
    CommentScraper,
    YahooNewsAndCommentScraper,
    extract_article_id,
    normalize_article_url,
    parse_comment_date,
    parse_count,
    parse_display_datetime,
)

AID = "1259832d694b0c3fd7763a61bc7c1c35be8c3d23"
NOW = datetime(2026, 11, 23, 17, 30, tzinfo=JST)


def test_article_id_and_url():
    url = f"https://news.yahoo.co.jp/articles/{AID}/comments?page=3"
    assert extract_article_id(url) == AID
    assert normalize_article_url(url) == f"https://news.yahoo.co.jp/articles/{AID}"
    assert extract_article_id(f"https://news.yahoo.co.jp/expert/articles/{AID}") == AID


def test_parse_comment_date():
    assert parse_comment_date("たった今", NOW) == (datetime(2026, 11, 23, 17, 30, tzinfo=JST), "minute")
    assert parse_comment_date("10分前", NOW) == (datetime(2026, 11, 23, 17, 20, tzinfo=JST), "minute")
    assert parse_comment_date("5時間前", NOW) == (datetime(2026, 11, 23, 12, 30, tzinfo=JST), "hour")
    assert parse_comment_date("2日前", NOW) == (datetime(2026, 11, 21, 17, 30, tzinfo=JST), "day")
    assert parse_comment_date("11/20(金) 9:05", NOW) == (datetime(2026, 11, 20, 9, 5, tzinfo=JST), "exact")
    assert parse_comment_date("", NOW) == (None, None)


def test_parse_display_datetime_year_rollover():
    # 1月に「12/31」と表示されたら前年
    jan = datetime(2027, 1, 2, 10, 0, tzinfo=JST)
    assert parse_display_datetime("12/31(木) 23:00", jan) == datetime(2026, 12, 31, 23, 0, tzinfo=JST)
    assert parse_display_datetime("2024/8/2(金) 11:54", NOW) == datetime(2024, 8, 2, 11, 54, tzinfo=JST)


def test_comment_rows_flags_are_0_1():
    s = CommentScraper(verbose=False)
    user = {
        "commentId": "c1", "commentArticleId": AID, "name": "abc***", "ppid": "u1",
        "text": "本文", "postDate": "1日前", "empathyCount": 3, "insightCount": 1,
        "negativeCount": 0, "isDiversityComment": False, "reply": {"totalResults": 12},
        "permalink": "https://news.yahoo.co.jp/profile/news/comments/c1",
    }
    expert = {
        "commentId": "e1", "commentArticleId": AID, "name": "専門家", "ppid": "u2",
        "text": "解説", "postDate": "1日前", "thumbsUpCount": 100, "label": "viewpoint",
        "commentator": {"id": "ex", "title": "専門家｜肩書"},
    }
    reply = {"commentId": "r1", "name": "x", "ppid": "u3", "text": "返信", "postDate": "3分前",
             "empathyCount": 0, "insightCount": 0, "negativeCount": 0}

    u = s._user_row(user, AID, 1, 1, NOW)
    e = s._expert_row(expert, AID, 1, 1, NOW)
    r = s._reply_row(reply, AID, "c1", 1, NOW)

    assert (u["is_expert"], u["is_reply"], u["has_reply"], u["reply_count"]) == (0, 0, 1, 12)
    assert (e["is_expert"], e["is_reply"], e["has_reply"], e["thumbs_up_count"]) == (1, 0, 0, 100)
    assert (r["is_expert"], r["is_reply"], r["parent_comment_id"], r["article_id"]) == (0, 1, "c1", AID)
    for row in (u, e, r):
        assert set(row) <= set(COMMENT_COLUMNS)
        for k in ("is_expert", "is_reply", "has_reply", "is_diversity_comment"):
            assert row.get(k, 0) in (0, 1)


def test_parse_count():
    assert parse_count("共感した 1.3万") == 13000
    assert parse_count("うーん 1,234") == 1234
    assert parse_count("なるほど 0") == 0
    assert parse_count("") is None


def test_reply_fallback_order(monkeypatch):
    s = CommentScraper(verbose=False)
    calls = []

    def fail(comment, aid):
        calls.append("api")
        return None

    def ok(comment, aid):
        calls.append("selenium")
        return [s._reply_row({"commentId": "r1", "postDate": "1分前"}, aid, "c1", 1, NOW)]

    monkeypatch.setattr(s, "_replies_via_api", fail)
    monkeypatch.setattr(s, "_replies_via_selenium", ok)
    rows, source = s.reply_scrape({"commentId": "c1"}, AID)
    assert calls == ["api", "selenium"]
    assert source == "selenium" and rows[0]["reply_source"] == "selenium"


def test_parse_reply_dom():
    html = """
    <ul><li><div>
      <article>
        <a data-cl-params="_cl_vmodule:rep;_cl_link:profnm;_cl_position:1;"
           href="https://news.yahoo.co.jp/users/PPID1">vai*****</a>
        <time>1時間前</time>
        <a data-cl-params="_cl_vmodule:rep;_cl_link:report;_cl_position:1;"
           href="https://news.yahoo.co.jp/comment/violation/?pid=news_user&amp;tid=X&amp;cid=r-1&amp;.done=x">報告</a>
        <p>返信本文</p>
      </article>
      <button data-cl-params="_cl_vmodule:rep;_cl_link:agbtn1cl;">共感した 3</button>
      <button data-cl-params="_cl_vmodule:rep;_cl_link:agbtn1;">共感した 2</button>
      <button data-cl-params="_cl_vmodule:rep;_cl_link:agbtn2;">なるほど 1.2万</button>
      <button data-cl-params="_cl_vmodule:rep;_cl_link:disbtn1;">うーん 0</button>
    </div></li></ul>
    """
    rows = CommentScraper(verbose=False)._parse_reply_dom(html, {"commentId": "c1"}, AID)
    assert len(rows) == 1
    r = rows[0]
    assert (r["comment_id"], r["parent_comment_id"], r["user_id"], r["user_name"]) == ("r-1", "c1", "PPID1", "vai*****")
    assert (r["empathy_count"], r["insight_count"], r["negative_count"]) == (2, 12000, 0)
    assert r["text"] == "返信本文" and r["is_reply"] == 1


def test_articles_and_comments_join_on_article_id(monkeypatch):
    s = YahooNewsAndCommentScraper(verbose=False)
    monkeypatch.setattr(s, "article_info_scrape", lambda url: {
        "article_id": AID, "url": url, "title": "t", "is_paid": 0, "comments_enabled": 1})
    comments = pd.DataFrame([{"comment_id": "c1", "article_id": AID, "is_reply": 0, "is_expert": 0}],
                            columns=COMMENT_COLUMNS)
    monkeypatch.setattr(s, "comment_scrape", lambda url, **kw: (comments, {
        "article_id": AID, "comment_total": 1, "comment_parent_total": 1,
        "comments_collected": 1, "is_paid": 0}))

    articles_df, comments_df = s.scrape_urls([f"https://news.yahoo.co.jp/articles/{AID}"])
    assert articles_df["article_id"].is_unique
    merged = comments_df.merge(articles_df, on="article_id", how="left")
    assert len(merged) == 1 and merged.loc[0, "title"] == "t"
    assert str(comments_df["is_reply"].dtype) == "Int64"
