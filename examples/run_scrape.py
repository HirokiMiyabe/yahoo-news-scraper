"""使用例: 検索クエリで記事とコメントを集め、2 つの CSV に保存する。"""

from yahoo_news_scraper.yahoo_scraper import (
    ArticleInfoScraper,
    CommentScraper,
    YahooNewsAndCommentScraper,
)

# --- まとめて実行（検索 → 記事 → コメント） ---------------------------------
scraper = YahooNewsAndCommentScraper(delay=1.0)
articles_df, comments_df = scraper.scrape("VIVANT ゲイ", max_articles=5)

articles_df.to_csv("articles.csv", index=False, encoding="utf-8-sig")
comments_df.to_csv("comments.csv", index=False, encoding="utf-8-sig")

# 記事情報をコメントに付けたいときは article_id で結合する
merged = comments_df.merge(articles_df[["article_id", "title", "media_name"]], on="article_id")
print(merged.head())

# --- 個別に使う --------------------------------------------------------------
url = "https://news.yahoo.co.jp/articles/1259832d694b0c3fd7763a61bc7c1c35be8c3d23"
article = ArticleInfoScraper().article_info_scrape(url)            # dict（1行）
comments, meta = CommentScraper().comment_scrape(url, max_pages=2)  # DataFrame, dict
