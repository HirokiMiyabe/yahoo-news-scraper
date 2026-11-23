# yahoo-news-scraper

Yahoo!ニュースの記事とコメント（返信を含む）を収集し、`article_id` で結合できる 2 つの DataFrame として返すパッケージです。非公開で運用しています。

## インストール

リポジトリへのアクセス権が必要です。

```bash
# GitHub から直接（private リポジトリ）
pip install "git+https://github.com/HirokiMiyabe/yahoo-news-scraper.git"

# Selenium フォールバックも使う場合
pip install "yahoo-news-scraper[selenium] @ git+https://github.com/HirokiMiyabe/yahoo-news-scraper.git"
```

開発時（ソースは `code/` 配下）:

```bash
pip install -e ".[dev]"
pytest
```

## 使い方

```python
from yahoo_news_scraper import YahooNewsAndCommentScraper

scraper = YahooNewsAndCommentScraper(delay=1.0)
articles_df, comments_df = scraper.scrape("検索キーワード", max_articles=5)
merged = comments_df.merge(articles_df, on="article_id", how="left")
```

詳しくは [examples/run_scrape.py](examples/run_scrape.py) を参照してください。
