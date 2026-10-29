import scrapy


class PackageItem(scrapy.Item):
    ecosystem = scrapy.Field()
    name = scrapy.Field()
    sampling_category = scrapy.Field()
    sampling_rank = scrapy.Field()
    sampling_seed = scrapy.Field()
    description = scrapy.Field()
    homepage_url = scrapy.Field()
    repository_url = scrapy.Field()
    license = scrapy.Field()
    latest_version = scrapy.Field()
    first_published_at = scrapy.Field()
    latest_published_at = scrapy.Field()
    downloads = scrapy.Field()
    downloads_period = scrapy.Field()
    source = scrapy.Field()
    source_url = scrapy.Field()
    collection_method = scrapy.Field()
    collected_at = scrapy.Field()
    versions = scrapy.Field()
