import re
from datetime import datetime, timedelta
from functools import reduce
from traceback import format_exc

from bs4 import BeautifulSoup, Comment
from pyspark.sql import Row, SparkSession

from common import download

min_date = datetime(2000, 1, 1)
base_url = 'http://www.spiegel.de'
archive_url_template = base_url + '/nachrichtenarchiv/artikel-{}.html'

executor_count = 64
sample_fraction = 1

refugee_words = ['Flüchtling', 'Asylant', 'Asylbewerber', 'Asylsuchender', 'Heimatvertriebener', 'Migrant']

def build_archive_url(date):
    return archive_url_template.format(date.strftime('%d.%m.%Y'))


def generate_dates(min_date):
    delta = datetime.today() - min_date
    return [min_date + timedelta(days=n) for n in range(delta.days)]


def extract_article_urls(url):
    html = download(url)
    if html is not None:
        return [a_tag['href'] if re.match('http.*', a_tag['href']) else base_url + a_tag['href'] for a_tag in
                BeautifulSoup(html, 'lxml').select('#content-main .column-wide ul li a')]
    else:
        print('extraction of urls {} failed ({})'.format(url, format_exc()))
        return []


def extract_text(nodes):
    return reduce(lambda agg, cur: agg + cur.getText(), nodes)


def extract_article_content(url):
    html = download(url)
    try:
        if html is not None:
            soup = BeautifulSoup(html, 'lxml')
            content_main = soup.select_one('#content-main')
            selectors_to_remove = ['.article-function-social-media',
                                   '.article-icon.spiegelplus',
                                   '.article-function-box',
                                   'script',
                                   'style',
                                   '#js-article-column > p',
                                   '#js-article-top-wide-asset',
                                   '.asset-box',
                                   '.article-copyright',
                                   '.article-function-box-wide',
                                   '.top-anchor',
                                   '.module-box',
                                   '.spiegel-asset-box',
                                   '#spRecommendations',
                                   '#js-video-slider',
                                   '.column-both-bottom',
                                   '#footer']
            for selector in selectors_to_remove:
                for node in content_main.select(selector):
                    node.decompose()
            for comment in soup.findAll(text=lambda text: isinstance(text, Comment)):
                comment.extract()
            content = re.sub('(\r\n|\n|\t|\s+)', ' ',
                             reduce(lambda agg, cur: agg + ' ' + cur, content_main.findAll(text=True)))
            return content
    except Exception:
        print('extraction of {} failed ({})'.format(url, format_exc()))


if __name__ == '__main__':
    spark = SparkSession.builder.appName('spiegel_scraper').master('local[{}]'.format(executor_count)).getOrCreate()

    dates = generate_dates(min_date)
    article_urls = spark \
        .sparkContext \
        .parallelize(dates) \
        .sample(fraction=sample_fraction, withReplacement=False) \
        .map(lambda date: Row(date=date, archive_url=build_archive_url(date))) \
        .flatMap(lambda r: [Row(date=r.date, article_url=url) for url in extract_article_urls(r.archive_url)]) \
        .filter(lambda r: 'spiegel.de' in r.article_url) \
        .filter(lambda r: 'spiegel.de/video' not in r.article_url) \
        .repartition(512) \
        .map(lambda r: Row(date=r.date, article_url=r.article_url, article=extract_article_content(r.article_url))) \
        .filter(lambda r: r.article is not None) \
        .filter(lambda r: any([w in r.article for w in refugee_words])) \
        .toDF() \
        .write \
        .format('csv') \
        .mode('overwrite') \
        .option('header', 'true') \
        .save('spiegel-articles-csv')
