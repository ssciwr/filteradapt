from afwizard.dataset import DataSet
from afwizard.paths import locate_file, check_file_extension
from afwizard.utils import (
    is_iterable,
)
from afwizard.utils import AFwizardError

import base64
import geojson
import ipyleaflet
import ipywidgets
import numpy as np
import collections
import copy
from itertools import groupby
from pyproj import Transformer, crs
from geojson_rewind import rewind


class Segmentation(geojson.FeatureCollection):
    def __init__(self, *args, spatial_reference=None, **kwargs):
        self.spatial_reference = spatial_reference
        super().__init__(*args, **kwargs)

    @classmethod
    def load(cls, filename=None, spatial_reference=None):
        """Load segmentation from a filename

        :param filename:
            The filename to load from. Relative paths are interpreted
            w.r.t. the current working directory.
        :type filename: str
        """

        if not isinstance(filename, collections.abc.Iterable):
            error = "filename needs to be a string, a list or a tuple, but is" + str(
                type(filename)
            )
            raise TypeError(error)

        # if a list of files is given a list of segmentations will be returned.
        if is_iterable(filename):
            segmentations = []
            for file in filename:
                file = locate_file(file)

                with open(file, "r") as f:
                    segmentations.append(
                        Segmentation(
                            geojson.load(f), spatial_reference=spatial_reference
                        )
                    )
            segmentations = enforce_geojson_topology(segmentations)
            return segmentations

        elif isinstance(filename, str):
            filename = locate_file(filename)
            with open(filename, "r") as f:
                segmentation = Segmentation(
                    geojson.load(f), spatial_reference=spatial_reference
                )
            segmentation = enforce_geojson_topology(segmentation)
            return segmentation

    def save(self, filename):
        """Save the segmentation to disk

        :param filename:
            The filename to save the segmentation to. Relative paths are interpreted
            w.r.t. the current working directory.
        :type filename: str
        """
        filename = check_file_extension(filename, [".geojson"], ".geojson")

        for feature in self["features"]:
            _ = feature["properties"].pop("style", None)

        with open(filename, "w") as f:
            geojson.dump(self, f)

    def show(self):
        """Create a new InteractiveMap with bounds from the segmentation."""

        segmentation_map = Map(segmentation=self)
        return segmentation_map.show()

    @property
    def __geo_interface__(self):
        return {
            "type": "FeatureCollection",
            "features": self.features,
        }


def enforce_geojson_topology(segmentations):

    if isinstance(segmentations, Segmentation):
        segmentations = [segmentations]

    for i, segmentation in enumerate(segmentations):
        for j, feature in enumerate(segmentation["features"]):
            # print(segmentations[i])
            segmentations[i]["features"][j] = rewind(segmentations[i]["features"][j])

    if len(segmentations) == 1:
        segmentations = segmentations[0]

    return segmentations


def merge_classes(segmentation, keyword=None):
    """
    If multiple polygons share the same class attribute they will be combined in one multipolygon feature.
    Warning, if members of the same class have different metadata it will not be preserved.
    """

    new_segmentation = Segmentation(
        [], spatial_reference=segmentation.spatial_reference
    )
    added_classes = {}

    if keyword == None:
        for feature in segmentation["features"]:
            feature["properties"].setdefault("merge", 1)
        keyword = "merge"

    for feature in segmentation["features"]:
        if keyword in feature["properties"]:
            if feature["properties"][keyword] not in added_classes.keys():
                # stores the class label and the index of the new segmentation associated with that class
                added_classes[feature["properties"][keyword]] = len(
                    new_segmentation["features"]
                )
                new_segmentation["features"].append(feature)
                if new_segmentation["features"][-1]["geometry"]["type"] == "Polygon":
                    new_segmentation["features"][-1]["geometry"][
                        "type"
                    ] = "MultiPolygon"
                    new_segmentation["features"][-1]["geometry"]["coordinates"] = [
                        new_segmentation["features"][-1]["geometry"]["coordinates"]
                    ]
            else:
                class_index = added_classes[feature["properties"][keyword]]
                if feature["geometry"]["type"] == "Polygon":
                    new_segmentation["features"][class_index]["geometry"][
                        "coordinates"
                    ].append(feature["geometry"]["coordinates"])
                elif feature["geometry"]["type"] == "MultiPolygon":
                    for coordinates in feature["geometry"]["coordinates"]:
                        new_segmentation["features"][class_index]["geometry"][
                            "coordinates"
                        ].append(coordinates)

    for feature in segmentation["features"]:
        if "merge" in feature["properties"].keys():
            _ = feature["properties"].pop("merge")
    new_segmentation = enforce_geojson_topology(new_segmentation)
    return new_segmentation


def get_min_max_values(segmentation):
    # goes over all features in the segmentation and return the min and max coordinates in a dict.
    min_max_dict = {"minX": [], "maxX": [], "minY": [], "maxY": []}

    for feature in segmentation["features"]:
        if feature["geometry"]["type"] == "Polygon":
            feature["geometry"]["coordinates"] = [feature["geometry"]["coordinates"]]
        for polygon in feature["geometry"]["coordinates"]:
            for coord_array in polygon:
                coord_array = np.asarray(coord_array)
                min_max_dict["minX"].append(np.min(coord_array, axis=0)[0])
                min_max_dict["minY"].append(np.min(coord_array, axis=0)[1])
                min_max_dict["maxX"].append(np.max(coord_array, axis=0)[0])
                min_max_dict["maxY"].append(np.max(coord_array, axis=0)[1])

    for key, value in min_max_dict.items():
        if "min" in key:
            min_max_dict[key] = min(value)
        elif "max" in key:
            min_max_dict[key] = max(value)

    # revert special case for Polygon
    for feature in segmentation["features"]:
        if feature["geometry"]["type"] == "Polygon":
            feature["geometry"]["coordinates"] = feature["geometry"]["coordinates"][0]
    return min_max_dict


def convert_segmentation(segmentation, srs_out, srs_in=None):
    """
    This transforms the segmentation into a new spatial reference system.
        For this program all segmentations should be in EPSG:4326.
        :param segmentation:
            The segmentation that should be transformed
        :type: afwizard.segmentation.Segmentation


        :param srs_in:
            Current spatial reference system of the segmentation.
            Must be either EPSG or wkt.
        :type: str

        :param srs_out:
            Desired spatial reference system.
            Must be either EPSG or wkt.
            Default: None
        :type: str



        :return: Transformed segmentation.
        :rtype: afwizard.segmentation.Segmentation

    """
    # logic for determining crs_in

    if srs_in is None:
        if segmentation.spatial_reference:
            srs_in = segmentation.spatial_reference
        else:
            raise AFwizardError("No srs was given for Segmentation transformation.")

    # check if transformation is neccesary:
    if crs.CRS(srs_in) == crs.CRS(srs_out):
        return segmentation

    new_features = copy.deepcopy(segmentation["features"])

    for feature, new_feature in zip(segmentation["features"], new_features):
        if feature["geometry"]["type"] == "Polygon":
            feature["geometry"]["coordinates"] = [feature["geometry"]["coordinates"]]
        polygon_list = []

        transformer = Transformer.from_crs(srs_in, srs_out, always_xy=True)

        for polygon in feature["geometry"]["coordinates"]:
            polygon_list.append([])
            for hole in polygon:
                hole = np.asarray(hole)

                output_x, output_y = transformer.transform(hole[:, 0], hole[:, 1])
                polygon_list[-1].append(np.stack([output_x, output_y], axis=1).tolist())

        if feature["geometry"]["type"] == "Polygon":
            polygon_list = polygon_list[0]
            feature["geometry"]["coordinates"] = feature["geometry"]["coordinates"][0]

        new_feature["geometry"]["coordinates"] = polygon_list

    out_segmentation = Segmentation(new_features, spatial_reference=srs_out)
    out_segmentation = enforce_geojson_topology(out_segmentation)
    return out_segmentation


def swap_coordinates(segmentation):
    """
    Takes a segmentation and swaps the lon and lat coordinates.


    """
    new_features = copy.deepcopy(segmentation["features"])
    for feature, new_feature in zip(segmentation["features"], new_features):
        if feature["geometry"]["type"] == "Polygon":
            feature["geometry"]["coordinates"] = [feature["geometry"]["coordinates"]]

        polygon_list = []
        for polygon in feature["geometry"]["coordinates"]:
            polygon_list.append([])
            for hole in polygon:
                polygon_list[-1].append([])
                for coordinate in hole:
                    polygon_list[-1][-1].append([coordinate[1], coordinate[0]])

        if feature["geometry"]["type"] == "Polygon":
            polygon_list = polygon_list[0]
        new_feature["geometry"]["coordinates"] = polygon_list
    swapped_segmentation = Segmentation(
        new_features, spatial_reference=segmentation.spatial_reference
    )
    swapped_segmentation = enforce_geojson_topology(swapped_segmentation)
    return swapped_segmentation


def split_segmentation_classes(segmentation):
    """
    If multiple polygons share the same class attribute they will be split into different segmentations.
    These will be structed in a nested dictionary.
    Warning, if members of the same class have different metadata it will not be preserved.
    """

    def _all_equal(iterable):
        g = groupby(iterable)
        return next(g, True) and not next(g, False)

    keys_list = [
        [
            k
            for k in list(feature["properties"].keys())
            if k not in ("pipeline", "pipeline_key")
        ]
        for feature in segmentation["features"]
    ]

    # only use keys that are present in all features:
    if _all_equal(keys_list):
        property_keys = keys_list[0]

    else:
        # count occurence of key and compare to number of features.
        from collections import Counter

        property_keys = []
        flat_list = [item for sublist in keys_list for item in sublist]
        for key, value in Counter(flat_list).items():
            if value == len(segmentation["features"]):
                property_keys.append(key)

    split_dict = {}

    for feature in segmentation["features"]:
        for key in property_keys:
            value = feature["properties"][key]
            # only use hashable objects as keys
            if isinstance(value, collections.abc.Hashable):
                split_dict.setdefault(key, {}).setdefault(
                    value,
                    Segmentation([], spatial_reference=segmentation.spatial_reference),
                )["features"].append(feature)

    # remove columns with too many entries to avoid slowdown

    keys_to_remove = []
    for key in split_dict.keys():
        if len(split_dict[key]) > 20:
            keys_to_remove.append(key)

    for key in keys_to_remove:
        _ = split_dict.pop(key)

    if len(split_dict.keys()) == 0:
        raise AFwizardError(
            "No suitable property key was found. "
            + "Please make sure there are classification properties present and that at least one of them has less than 20 categories."
        )

    # sort the values to also sort the dropdown menus later.
    for key in split_dict.keys():
        split_dict[key] = dict(sorted(split_dict[key].items()))

    return split_dict


class Map:
    def __init__(
        self, dataset=None, segmentation=None, in_srs=None, inlude_draw_controle=True
    ):
        """Manage the interactive map use to create segmentations

        It can be initilized with a dataset from which it will detect the boundaries and show them on the map.
        Or it can be initilized with a segmentation which will also be visualized on the map.
        There can be multiple polygons in the Segmentation and all will drawn. The
        depending on the segmentation it might be necessary to first swap the coordinates of the segmentation to fit with the ipyleaflet map
        in_srs can be used to override the current srs.

        :param dataset:
            The dataset from which the map should be displayed.
        :type dataset: Dataset
        :param segmentation:
            A premade segmentation can can be loaded and shown on a map without the need to load a dataset.
        :type segmentation: Segmentation
        :param in_srs:
            manually override the srs of the dataset or segmentation, necessary if none are specified in the object.
        :type in_srs: str

        """
        from afwizard.pdal import PDALInMemoryDataSet

        # handle exeptions
        if dataset and segmentation:
            raise AFwizardError(
                "A dataset and a segmentation can't be loaded at the same time."
            )

        if dataset is None and segmentation["features"] is []:
            raise AFwizardError("an empty segmention was given.")

        if dataset is None and segmentation is None:
            # if no dataset or segmentation is given, the map will be centered at the SSC office
            raise AFwizardError(
                "Please use either a dataset or a segmentation. None were given."
            )

        # check if dataset and segmentation are of correct type
        if dataset:
            if isinstance(dataset, Segmentation):
                raise AFwizardError(
                    "A segmentation was given as a dataset, please call Map(segmentation=yourSegmentation)"
                )
            elif not isinstance(dataset, DataSet):
                raise AFwizardError(
                    f"The given dataset is not of type DataSet, but {type(dataset)}."
                )

        elif segmentation:
            if isinstance(segmentation, DataSet):
                raise AFwizardError(
                    "A DataSet was given as a Segmentation, please call Map(dataset=yourDataset)"
                )
            elif not isinstance(segmentation, Segmentation):
                raise AFwizardError(
                    f"The given segmentation is not of type Segmentation, but {type(segmentation)}."
                )

        # convert to pdal dataset
        if dataset:
            dataset = PDALInMemoryDataSet.convert(dataset)
            # preserve the original srs from dataset
            if in_srs is None:
                self.original_srs = dataset.spatial_reference
            else:
                if in_srs is None:
                    raise AFwizardError(
                        "No srs could be found. Please specify one or use a dataset that includes one."
                    )
                self.original_srs = in_srs

        elif segmentation:
            self.original_srs = segmentation.spatial_reference
        self.inlude_draw_controle = inlude_draw_controle

        self.dataset = dataset  # needed for overlay function.

        # convert to a srs the ipyleaflet map can use.
        # the only way this seems to work is to convert the dataset to EPSG:4326 and set the map to expect EPSG:3857
        # https://gis.stackexchange.com/questions/48949/epsg-3857-or-4326-for-googlemaps-openstreetmap-and-leaflet/48952#48952
        boundary_segmentation = self.load_hexbin_boundary(dataset, segmentation)

        # get square edges of the boundary_segmentation for use in hillshade overlay
        self.boundary_edges = get_min_max_values(boundary_segmentation)

        self.setup_map(boundary_segmentation)
        self.setup_controls()

        # set up overlay list.
        # this stores the parameters used in the load_overlay function to avoid multipole calculations of the same overlay
        self.overlay_list = []

    def load_overlay(self, vis, title):
        """
        Takes a visualisation and loads it into the map.
        """

        # Construct URL for image to use in ipyleaflet
        data = base64.b64encode(vis.value)
        data = data.decode("ascii")
        url = "data:image/{};base64,".format("png") + data

        # convert the edges into a tuple
        boundary_tuple = (
            (self.boundary_edges["minY"], self.boundary_edges["minX"]),
            (self.boundary_edges["maxY"], self.boundary_edges["maxX"]),
        )
        layer = ipyleaflet.ImageOverlay(
            url=url,
            bounds=((boundary_tuple[1]), (boundary_tuple[0])),
            name=title,
        )
        # load the desired layer
        self.map.add_layer(layer)
        self.overlay_list.append(title)

    def show(self):
        return self.map

    def setup_controls(self):
        """Modifies the polygon draw control to only include polygons, delete and clear all.
        Also initilizes the zoom slider, and layer control
        """
        self.draw_control = ipyleaflet.DrawControl(
            layout=ipywidgets.Layout(width="auto", grid_area="main")
        )
        # deactivate polyline and circlemarker
        self.draw_control.polyline = {}
        self.draw_control.circlemarker = {}

        self.draw_control.polygon = {
            "shapeOptions": {
                "fillColor": "black",
                "color": "black",
                "fillOpacity": 0.1,
            },
            "drawError": {"color": "#dd253b", "message": "Oups!"},
            "allowIntersection": False,
        }

        # add draw control
        if self.inlude_draw_controle:
            self.map.add_control(self.draw_control)

        # add zoom control
        self.zoom_slider = ipywidgets.IntSlider(
            description="Zoom level:", min=0, max=20, value=16
        )
        ipywidgets.jslink((self.zoom_slider, "value"), (self.map, "zoom"))
        self.zoom_control1 = ipyleaflet.WidgetControl(
            widget=self.zoom_slider, position="topright"
        )
        self.map.add_control(self.zoom_control1)

        # layer control
        self.layer_control = ipyleaflet.LayersControl(position="topright")
        self.map.add_control(self.layer_control)

    def load_geojson(self, segmentation, name=""):
        """Imports a segmentation objectas an actual layer.

        :param segmentation:
            A segmentation object which is to be loaded.
        :type segmentation: Segmentation
        """
        # check if segmentation has draw style information.
        segmentation = copy.deepcopy(segmentation)

        segmentation = convert_segmentation(segmentation, "EPSG:4326")

        segmentation = merge_classes(segmentation)
        for feature in segmentation["features"]:
            if "style" not in feature["properties"].keys():
                feature["properties"]["style"] = {
                    "pane": "overlayPane",
                    "attribution": "null",
                    "bubblingMouseEvents": "true",
                    "fill": "true",
                    "smoothFactor": 1,
                    "noClip": "false",
                    "stroke": "true",
                    "color": "black",
                    "weight": 4,
                    "opacity": 0.5,
                    "lineCap": "round",
                    "lineJoin": "round",
                    "dashArray": "null",
                    "dashOffset": "null",
                    "fillColor": "black",
                    "fillOpacity": 0.1,
                    "fillRule": "evenodd",
                    "interactive": "true",
                    "clickable": "true",
                }

        self.map.add_layer(ipyleaflet.GeoJSON(data=segmentation, name=name))

    def load_segmentation(self, segmentation, override=False):
        """Imports a segmentation object into the draw control data

        :param segmentation:
            A segmentation object which is to be loaded.
        :type segmentation: Segmentation
        """

        if override:
            self.draw_control.data = [
                new_polygon for new_polygon in segmentation["features"]
            ]

        else:
            # save current polygon data
            current_data = self.draw_control.data
            # filters only new polygons. to avoid double entrys. Ignores color and style, only checks for the geometry.

            # adds the new polygons to the current data
            new_polygons = [
                new_polygon
                for new_polygon in segmentation["features"]
                if not new_polygon["geometry"]
                in [data["geometry"] for data in current_data]
            ]
            new_data = current_data + new_polygons

            self.draw_control.data = new_data

    def load_hexbin_boundary(self, dataset=None, segmentation=None):
        """
        takes the dataset returns the boundary Segmentation.
        If a segmentation is given, this will convert it into a boundary segmentation.
        """
        from afwizard.pdal import execute_pdal_pipeline

        if dataset:
            info_pipeline = execute_pdal_pipeline(
                dataset=dataset, config=[{"type": "filters.info"}]
            )

            hexbin_pipeline = execute_pdal_pipeline(
                dataset=dataset,
                config=[
                    {
                        "type": "filters.hexbin",
                        "sample_size": info_pipeline.metadata["metadata"][
                            "filters.info"
                        ]["num_points"],
                        "precision": 10,
                        "threshold": 1,
                    },
                ],
            )

            # get the coordinates from the metadata:
            # this gives us lat, lon but for geojson we need lon, lat

            hexbin_geometry = hexbin_pipeline.metadata["metadata"]["filters.hexbin"][
                "boundary_json"
            ]

        elif segmentation:
            segmentation = merge_classes(segmentation)
            hexbin_geometry = segmentation["features"][0]["geometry"]
        boundary_segmentation = Segmentation(
            [
                {
                    "type": "Feature",
                    "properties": {
                        "style": {
                            "stroke": True,
                            "color": "#add8e6",
                            "weight": 4,
                            "opacity": 0.5,
                            "fill": True,
                            "clickable": False,
                        }
                    },
                    "geometry": hexbin_geometry,
                }
            ],
            spatial_reference=self.original_srs,
        )

        # the segmentation should already be in the correct format so no additaional conversion is requiered
        boundary_segmentation = convert_segmentation(boundary_segmentation, "EPSG:4326")
        return boundary_segmentation

    def setup_map(self, boundary_segmentation):
        """Takes the boundary coordinates of the  given dataset
        through the pdal hexbin filter and returns them as a segmentation.
        From the segmentation it calculates the center point as well as the edge points to implement the starting location of the map.
        The edge points are used to draw the boundary square of the given dataset.
        """
        from afwizard.pdal import execute_pdal_pipeline

        coordinates_mean = [
            (self.boundary_edges["maxX"] + self.boundary_edges["minX"]) / 2,
            (self.boundary_edges["maxY"] + self.boundary_edges["minY"]) / 2,
        ]
        self.map = ipyleaflet.Map(
            basemap=ipyleaflet.basemaps.Esri.WorldImagery,
            center=(coordinates_mean[1], coordinates_mean[0]),
            # we have to use epsg 3857 see comment in init
            crs=ipyleaflet.projections.EPSG3857,
            scroll_wheel_zoom=False,
            max_zoom=20,
        )
        self.map.add_layer(
            ipyleaflet.GeoJSON(data=boundary_segmentation, name="Boundary")
        )

    def return_segmentation(self):
        """Exports the current polygon list as a Segmentation object

        :return:
            :param segmentation:
                All current polygons in one segmentation object
            :type segmentation: Segmentation


        """
        segmentation = Segmentation(
            self.draw_control.data, spatial_reference="EPSG:4326"
        )

        return segmentation


def load_segmentation(filename, spatial_reference=None):
    """Load a GeoJSON segmentation from a file

    :param filename:
        The filename to load the GeoJSON file from.
    :type filename: str
    :param spatial_reference:
        The WKT or EPSG code of the segmentation file.
    """

    # TODO: Add spatial_reference here
    return Segmentation.load(filename, spatial_reference=spatial_reference)
