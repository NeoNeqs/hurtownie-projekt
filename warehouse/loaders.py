from itemloaders.processors import MapCompose, TakeFirst
from scrapy.loader import ItemLoader

from warehouse.items import PackageItem
from warehouse.processing import (
    collapse_whitespace,
    normalize_license,
    normalize_repository_url,
    normalize_url,
    strip_whitespace,
)


class PackageLoader(ItemLoader):
    default_item_class = PackageItem
    default_output_processor = TakeFirst()

    name_in = MapCompose(strip_whitespace)
    description_in = MapCompose(collapse_whitespace)
    homepage_url_in = MapCompose(normalize_url)
    repository_url_in = MapCompose(normalize_repository_url)
    license_in = MapCompose(normalize_license)
