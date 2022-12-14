import logging
import re
from pandas import concat, read_csv
from rasterstats import zonal_stats
from slugify import slugify

from hdx.data.dataset import Dataset
from hdx.location.country import Country
from hdx.utilities.downloader import DownloadError

logger = logging.getLogger()


class Population:
    def __init__(self, configuration, downloader, subnational_json, temp_folder):
        self.downloader = downloader
        self.boundaries = subnational_json
        self.temp_folder = temp_folder
        self.exceptions = {"dataset": configuration["inputs"].get("dataset_exceptions", {}),
                           "resource": configuration["inputs"].get("resource_exceptions", {})}
        self.headers = configuration["pcode_mappings"]
        self.skip = configuration["inputs"].get("do_not_process", [])

    def find_resource(self, iso, level):
        dataset = Dataset.read_from_hdx(self.exceptions["dataset"].get(iso, f"cod-ps-{iso.lower()}"))
        if not dataset:
            logger.warning(f"{iso}: Could not find PS dataset")
            dataset = Dataset.read_from_hdx(
                f"worldpop-population-counts-for-{slugify(Country.get_country_name_from_iso3(iso))}"
            )
            pop_resource = [r for r in dataset.get_resources() if r.get_file_type() == "geotiff" and
                            bool(re.match("(?<!\d)\d{4}_constrained", r["name"], re.IGNORECASE))]
            if len(pop_resource) == 0:
                return None, "geotiff"
            return pop_resource[0], "geotiff"

        resources = dataset.get_resources()
        resource_name = self.exceptions["resource"].get(iso, f"adm(in)?{level}")
        pop_resource = [r for r in resources if r.get_file_type() == "csv" and
                        bool(re.match(f".*{resource_name}.*", r["name"], re.IGNORECASE))]
        if len(pop_resource) == 0:
            logger.warning(f"{iso}: Could not find csv resource at adm{level}")
            return None, "geotiff"

        if len(pop_resource) > 1:
            yearmatches = [
                re.findall("(?<!\d)\d{4}(?!\d)", r["name"], re.IGNORECASE)
                for r in pop_resource
            ]
            yearmatches = sum(yearmatches, [])
            if len(yearmatches) > 0:
                yearmatches = [int(y) for y in yearmatches]
            maxyear = [
                r for r in pop_resource if str(max(yearmatches)) in r["name"]
            ]
            if len(maxyear) == 1:
                pop_resource = maxyear

        if len(pop_resource) > 1:
            logger.warning(f"{iso}: Found multiple resources, using first in list")

        return pop_resource[0], "csv"

    def analyze_raster(self, resource, iso, level):
        try:
            _, pop_raster = resource.download(folder=self.temp_folder)
        except DownloadError:
            logger.error(f"{iso}: Could not download geotiff")
            return None

        pop_stats = zonal_stats(
            vectors=self.boundaries.loc[(self.boundaries["alpha_3"] == iso) &
                                        (self.boundaries["ADM_LEVEL"] == level)],
            raster=pop_raster,
            stats="sum",
            geojson_out=True,
        )
        for row in pop_stats:
            pcode = row["properties"]["ADM_PCODE"]
            pop = row["properties"]["sum"]
            if pop:
                pop = int(round(pop, 0))
                self.boundaries.loc[self.boundaries["ADM_PCODE"] == pcode, "Population"] = pop

        return iso

    def analyze_tabular(self, resource, iso, level):
        headers, iterator = self.downloader.get_tabular_rows(
            resource["url"], dict_form=True
        )

        pcode_header = None
        pop_header = None
        for header in headers:
            if not pcode_header:
                if header.upper() in [h.replace("#", str(level)) for h in self.headers]:
                    pcode_header = header
            if not pop_header:
                if header.upper() == "T_TL":
                    pop_header = header

        if not pcode_header:
            logger.error(f"{iso}: Could not find pcode header at adm{level}")
            return None
        if not pop_header:
            logger.error(f"{iso}: Could not find pop header at adm{level}")
            return None

        updated = False
        for row in iterator:
            pcode = row[pcode_header]
            pop = row[pop_header]
            if pcode not in list(self.boundaries["ADM_PCODE"]):
                logger.warning(f"{iso}: Could not find unit {pcode} in boundaries at adm{level}")
            else:
                self.boundaries.loc[self.boundaries["ADM_PCODE"] == pcode, "Population"] = pop
                updated = True
        if not updated:
            return None
        return iso

    def update_population(self, countries):
        updated_countries = dict()
        for iso in countries:
            levels = list(set(self.boundaries["ADM_LEVEL"].loc[(self.boundaries["alpha_3"] == iso)]))
            for level in levels:
                if level not in updated_countries:
                    updated_countries[level] = list()
                logger.info(f"{iso}: Processing population at adm{level}")

                # find dataset and resource to use
                updated = False
                resource, resource_type = self.find_resource(iso, level)

                if not resource:
                    logger.error(f"{iso}: Could not find any {resource_type} data at adm{level}")
                    continue

                if resource_type == "geotiff":
                    updated = self.analyze_raster(resource, iso, level)

                if resource_type == "csv":
                    updated = self.analyze_tabular(resource, iso, level)

                if updated and iso not in updated_countries[level]:
                    updated_countries[level].append(iso)
                continue

        return updated_countries

    def update_hdx_resource(self, dataset_name, updated_countries):
        dataset = Dataset.read_from_hdx(dataset_name)
        if not dataset:
            logger.error("Could not find overall pop dataset")
            return None, None

        resource = dataset.get_resources()[0]
        try:
            _, pop_data = resource.download(folder=self.temp_folder)
        except DownloadError:
            logger.error(f"Could not download population csv")
            return None, None
        pop_data = read_csv(pop_data)

        updated_data = self.boundaries.drop(columns="geometry")
        for level in updated_countries:
            pop_data.drop(pop_data[(pop_data["alpha_3"].isin(updated_countries[level])) &
                                   (pop_data["ADM_LEVEL"] == level)].index, inplace=True)
            pop_data = concat([pop_data,
                               updated_data.loc[(updated_data["alpha_3"].isin(updated_countries[level])) &
                                                (updated_data["ADM_LEVEL"] == level)]])

        pop_data.sort_values(by=["alpha_3", "ADM_LEVEL", "ADM_PCODE"], inplace=True)
        return pop_data, resource
