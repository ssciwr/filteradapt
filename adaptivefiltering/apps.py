from adaptivefiltering.asprs import asprs_class_name
from adaptivefiltering.dataset import DataSet, DigitalSurfaceModel, reproject_dataset
from adaptivefiltering.filter import Pipeline, Filter, update_data
from adaptivefiltering.library import (
    get_filter_libraries,
    library_keywords,
)
from adaptivefiltering.paths import load_schema, within_temporary_workspace
from adaptivefiltering.pdal import PDALInMemoryDataSet
from adaptivefiltering.segmentation import Map, Segmentation, swap_coordinates
from adaptivefiltering.utils import (
    AdaptiveFilteringError,
    merge_segmentation_features,
    convert_segmentation,
)
from adaptivefiltering.widgets import WidgetFormWithLabels

from osgeo import ogr

import collections
import contextlib
import copy
import ipywidgets
import ipywidgets_jsonschema
import IPython
import itertools
import numpy as np
import pyrsistent
import pytools
import wrapt
import functools


fullwidth = ipywidgets.Layout(width="100%")


def return_proxy(creator, widgets):
    # Create a new proxy object by calling the creator once
    proxy = wrapt.ObjectProxy(creator())

    # Define a handler that updates the proxy
    def _update_proxy(_):
        proxy.__wrapped__ = creator()

    # Register handler that triggers proxy update
    for widget in widgets:
        widget.observe(
            _update_proxy, names=("value", "selected_index", "data"), type="change"
        )

    return proxy


@contextlib.contextmanager
def hourglass_icon(button):
    """Context manager to temporarily show an hourglass icon on a button

    :param button: The button
    :type button: ipywidgets.Button
    """
    button.icon = "hourglass-half"
    yield
    button.icon = ""


def as_pdal(dataset):
    """Transform a dataset or digital surface model into a PDAL dataset"""
    if isinstance(dataset, DigitalSurfaceModel):
        return as_pdal(dataset.dataset)
    return PDALInMemoryDataSet.convert(dataset)


def classification_widget(datasets, selected=None):
    """Create a widget to select classification values

    The shown classification values are taken from the datasets themselves
    and the total amount of points for each class is shown in the widget.
    The widget then allows to select an arbitrary number of classes. By default,
    all classes are selected, unless ground points are present in the datasets
    in which case only these are selected.

    :param datasets:
        A list of datasets.
    :type datasets: list
    :param selected:
        An optional list of pre-selected indices.
    :type selected: list
    """

    # Determine classes present across all datasets
    joined_count = {}
    for dataset in datasets:
        # Make sure that we have an in-memory copy of the dataset
        dataset = as_pdal(dataset)

        # Get the lists present in this dataset
        for code, numpoints in enumerate(np.bincount(dataset.data["Classification"])):
            if numpoints > 0:
                joined_count.setdefault(code, 0)
                joined_count[code] += numpoints

    # If the dataset already contains ground points, we only want to use
    # them by default. This saves tedious work for the user who is interested
    # in ground point filtering results.
    if selected is None:
        if 2 in joined_count:
            selected = [2]
        else:
            # If there are no ground points, we use all classes
            selected = list(joined_count.keys())

    return ipywidgets.SelectMultiple(
        options=[
            (f"[{code}]: {asprs_class_name(code)} ({joined_count[code]} points)", code)
            for code in sorted(joined_count.keys())
        ],
        value=selected,
    )


@pytools.memoize(key=lambda d, p, **c: (d, p.config, pyrsistent.pmap(c)))
def cached_pipeline_application(dataset, pipeline, **config):
    """Call filter pipelinex execution in a cached way"""
    return pipeline.execute(dataset, **config)


def expand_variability_string(varlist, type_="string", samples_for_continuous=5):
    """Split a string into variants allowing comma separation and ranges with dashes

    :param varlist:
        The input string to expand
    :type varlist: str
    :param type_:
        The type of the variables to return. Maybe `string`, `integer` or `number`.
    :type type_: str
    :param samples_for_continuous:
        The number of samples to use when resolving ranges of floating point values (defaults to 5)
    :type samples_for_continuous: int
    """
    # For discrete variation, we use comma separation
    for part in varlist.split(","):
        part = part.strip()

        # If this is a numeric parameter it might also have ranges specified by dashes
        if type_ == "number":
            slice_ = part.split(":")

            # This is not a range
            if len(slice_) == 1:
                yield float(part)

            # This is a start/stop range that we sample with 5 intervals
            if len(slice_) == 2:
                for i in range(samples_for_continuous):
                    yield float(slice_[0]) + i / (samples_for_continuous - 1) * (
                        float(slice_[1]) - float(slice_[0])
                    )

            # This is a slice with start, stop and step
            if len(slice_) == 3:
                current = float(slice_[0])
                while current <= float(slice_[1]):
                    yield current
                    current = current + float(slice_[2])

            # Check for weird patterns like "0-5-10"
            if len(slice_) > 3:
                raise ValueError(f"Given an invalid range of parameters: '{part}'")

        if type_ == "integer":
            slice_ = part.split(":")

            # This is not a range
            if len(slice_) == 1:
                yield int(part)

            # This is a start/stop range
            if len(slice_) == 2:
                for i in range(int(slice_[0]), int(slice_[1]) + 1):
                    yield i

            # This is a range with start/stop and step
            if len(slice_) == 3:
                current = int(slice_[0])
                while current <= int(slice_[1]):
                    yield current
                    current = current + int(slice_[2])

            if len(slice_) > 3:
                raise ValueError(f"Given an invalid range of parameters: '{part}'")

        if type_ == "string":
            yield part


def create_variability(batchdata, samples_for_continuous=5, non_persist_only=True):
    """Create combinatorical product of specified variants

    :param batchdata:
        The variability data provided by the filter data model.
    :type batchdata: list
    :param samples_for_continuous:
        The number of samples to use when resolving ranges of floating point values (defaults to 5)
    :type samples_for_continuous: int
    :param non_persist_only:
        Whether or not the creation of variability is restricted to entries
        with `persist=False`. The `persist` field is used to distinguish batch
        processing from end user configuration.
    :type non_persist_only: bool
    """
    if non_persist_only:
        batchdata = [bd for bd in batchdata if not bd["persist"]]

    variants = []
    varpoints = [
        tuple(
            expand_variability_string(
                bd["values"],
                samples_for_continuous=samples_for_continuous,
                type_=bd["type"],
            )
        )
        for bd in batchdata
    ]
    for combo in itertools.product(*varpoints):
        variant = []
        for i, val in enumerate(combo):
            newbd = batchdata[i].copy()
            newbd["values"] = val
            variant.append(newbd)
        variants.append(variant)

    return variants


# A data structure to store widgets within to quickly navigate back and forth
# between visualizations in the pipeline_tuning widget.
PipelineWidgetState = collections.namedtuple(
    "PipelineWidgetState",
    ["pipeline", "rasterization", "visualization", "classification", "image"],
)


def pipeline_tuning(datasets=[], pipeline=None):
    """The Jupyter UI to create a filtering pipeline from scratch.

    The use of this UI is described in detail in `the notebook on creating filter pipelines`_.

    .. _the notebook on creating filter pipelines: filtering.nblink

    :param datasets:
        One or more instances of Lidar datasets to work on
    :type datasets: list
    :param pipeline:
        A pipeline to use as a starting point. If omitted, a new pipeline object will be created.
    :type pipeline: adaptivefiltering.filter.Pipeline
    :return:
        Returns the created pipeline object
    :rtype: adaptivefiltering.filter.Pipeline
    """

    # Instantiate a new pipeline object if we are not modifying an existing one.
    if pipeline is None:
        pipeline = Pipeline()

    # If a single dataset was given, transform it into a list
    if isinstance(datasets, DataSet):
        datasets = [datasets]

    # Assert that at least one dataset has been provided
    if len(datasets) == 0:
        raise AdaptiveFilteringError(
            "At least one dataset must be provided to pipeline_tuning"
        )

    # Create the data structure to store the history of visualizations in this app
    history = []

    # Loop over the given datasets
    def create_history_item(ds, data, classes=None):
        # Create a new classification widget and insert it into the Box
        _class_widget = classification_widget([ds], selected=classes)
        app.right_sidebar.children[-1].children = (_class_widget,)

        # Create widgets from the datasets
        image = ipywidgets.Box(
            children=[
                ds.show(
                    classification=class_widget.children[0].value,
                    **rasterization_widget_form.data,
                    **visualization_form.data,
                )
            ]
        )

        # Add the set of widgets to our history
        history.append(
            PipelineWidgetState(
                pipeline=data,
                rasterization=rasterization_widget_form.data,
                visualization=visualization_form.data,
                classification=_class_widget,
                image=image,
            )
        )

        # Add it to the center Tab widget
        nonlocal center
        index = len(center.children)
        center.children = center.children + (image,)
        center.titles = center.titles + (f"#{index}",)

    # Configure control buttons
    preview = ipywidgets.Button(description="Preview", layout=fullwidth)
    finalize = ipywidgets.Button(description="Finalize", layout=fullwidth)
    delete = ipywidgets.Button(
        description="Delete this filtering", layout=ipywidgets.Layout(width="50%")
    )
    delete_all = ipywidgets.Button(
        description="Delete filtering history", layout=ipywidgets.Layout(width="50%")
    )

    # The center widget holds the Tab widget to browse history
    center = ipywidgets.Tab(children=[], titles=[])
    center.layout = fullwidth

    def _switch_tab(_):
        if len(center.children) > 0:
            item = history[center.selected_index]
            pipeline_form.data = item.pipeline
            rasterization_widget_form.data = item.rasterization
            visualization_form_widget.data = item.visualization
            class_widget.children = (item.classification,)

    def _trigger_preview(config=None):
        if config is None:
            config = pipeline_form.data

        # Extract the currently selected classes and implement heuristic:
        # If ground was already in the classification, we keep the values
        if history:
            old_classes = history[-1].classification.value
            had_ground = 2 in [o[1] for o in history[-1].classification.options]
            classes = old_classes if had_ground else None
        else:
            classes = None

        for ds in datasets:
            # Extract the pipeline from the widget
            nonlocal pipeline
            pipeline = pipeline.copy(**config)

            # TODO: Do this in parallel!
            with within_temporary_workspace():
                transformed = cached_pipeline_application(ds, pipeline)

            # Create a new entry in the history list
            create_history_item(transformed, config, classes=classes)

            # Select the newly added tab
            center.selected_index = len(center.children) - 1

    def _update_preview(button):
        with hourglass_icon(button):
            # Check whether there is batch-processing information
            batchdata = pipeline_form.batchdata

            if len(batchdata) == 0:
                _trigger_preview()
            else:
                for variant in create_variability(batchdata):
                    config = pyrsistent.freeze(pipeline_form.data)

                    # Modify all the necessary bits
                    for mod in variant:
                        config = update_data(config, mod)

                    _trigger_preview(pyrsistent.thaw(config))

    def _delete_history_item(_):
        i = center.selected_index
        nonlocal history
        history = history[:i] + history[i + 1 :]
        center.children = center.children[:i] + center.children[i + 1 :]
        center.selected_index = len(center.children) - 1

        # This ensures that widgets are updated when this tab is removed
        _switch_tab(None)

    def _delete_all(_):
        nonlocal history
        history = []
        center.children = tuple()

    # Register preview button click handler
    preview.on_click(_update_preview)

    # Register delete button click handler
    delete.on_click(_delete_history_item)
    delete_all.on_click(_delete_all)

    # When we switch tabs, all widgets should restore the correct information
    center.observe(_switch_tab, names="selected_index")

    # Create the (persisting) building blocks for the app
    pipeline_form = pipeline.widget_form()

    # Get a widget for rasterization
    raster_schema = copy.deepcopy(load_schema("rasterize.json"))

    # We drop classification, because we add this as a specialized widget
    raster_schema["properties"].pop("classification")

    rasterization_widget_form = ipywidgets_jsonschema.Form(
        raster_schema, vertically_place_labels=True
    )
    rasterization_widget = rasterization_widget_form.widget
    rasterization_widget.layout = fullwidth

    # Get a widget that allows configuration of the visualization method
    schema = load_schema("visualization.json")
    visualization_form = ipywidgets_jsonschema.Form(
        schema, vertically_place_labels=True, use_sliders=True
    )
    visualization_form_widget = visualization_form.widget
    visualization_form_widget.layout = fullwidth

    # Get the container widget for classification
    class_widget = ipywidgets.Box([])
    class_widget.layout = fullwidth

    # Create the final app layout
    app = ipywidgets.AppLayout(
        left_sidebar=ipywidgets.VBox(
            [
                ipywidgets.HTML(
                    "Interactive pipeline configuration:", layout=fullwidth
                ),
                pipeline_form.widget,
            ]
        ),
        center=center,
        right_sidebar=ipywidgets.VBox(
            [
                ipywidgets.HTML("Ground point filtering controls:", layout=fullwidth),
                preview,
                finalize,
                ipywidgets.HBox([delete, delete_all]),
                ipywidgets.HTML("Rasterization options:", layout=fullwidth),
                rasterization_widget,
                ipywidgets.HTML("Visualization options:", layout=fullwidth),
                visualization_form_widget,
                ipywidgets.HTML(
                    "Point classifications to include in the hillshade visualization (click preview to update):",
                    layout=fullwidth,
                ),
                class_widget,
            ]
        ),
    )

    # Initially trigger preview generation
    preview.click()

    # Show the app in Jupyter notebook
    IPython.display.display(app)

    # Implement finalization
    pipeline_proxy = return_proxy(
        lambda: pipeline.copy(
            _variability=pipeline_form.batchdata, **pipeline_form.data
        ),
        [pipeline_form],
    )

    def _finalize(_):
        app.layout.display = "none"

    finalize.on_click(_finalize)

    # Return the pipeline proxy object
    return pipeline_proxy


def setup_overlay_control(dataset, with_map=False, inlude_draw_controle=True):
    """
    This function creates the rasterization control widged for the restrict, assign_pipeline and show_ineractive widgets

    """

    # Get a widget for rasterization

    raster_schema = copy.deepcopy(load_schema("rasterize.json"))

    # We drop classification, because we add this as a specialized widget
    raster_schema["properties"].pop("classification")

    rasterization_widget_form = ipywidgets_jsonschema.Form(
        raster_schema, vertically_place_labels=True
    )
    rasterization_widget = rasterization_widget_form.widget
    rasterization_widget.layout = fullwidth

    # Get a widget that allows configuration of the visualization method
    schema = load_schema("visualization.json")

    form = ipywidgets_jsonschema.Form(
        schema, vertically_place_labels=True, use_sliders=True
    )
    formwidget = form.widget
    formwidget.layout = fullwidth

    # Create the classification widget
    classification = ipywidgets.Box([classification_widget([dataset])])
    classification.layout = fullwidth

    load_raster_button = ipywidgets.Button(description="Visualize", layout=fullwidth)

    def load_raster_to_map(b):
        with hourglass_icon(b):
            # Rerasterize if necessary
            nonlocal dataset
            if not isinstance(dataset, DigitalSurfaceModel):
                dataset = dataset.rasterize(
                    classification=classification.children[0].value,
                    **rasterization_widget_form.data,
                )

            else:
                dataset = dataset.dataset.rasterize(
                    classification=classification.children[0].value,
                    **rasterization_widget_form.data,
                )

            vis = dataset.show(**form.data).children[0]
            map_.load_overlay(vis, "Visualisation")

    # case for restrict
    if with_map:

        # create instance of dataset with srs of "EPSG:3857",
        # this ensures that the slope and hillshade overlay fit the map projection.
        dataset = reproject_dataset(dataset, "EPSG:3857")
        # Create the map
        map_ = Map(dataset, inlude_draw_controle=inlude_draw_controle)
        load_raster_button.on_click(load_raster_to_map)

        load_raster_label = ipywidgets.Box(
            (ipywidgets.Label("Add Geotiff layer to the map:"),)
        )
        controls = ipywidgets.VBox(
            [
                load_raster_label,
                load_raster_button,
                rasterization_widget,
                formwidget,
                classification,
            ]
        )
        return controls, map_
    # case for show_interactive
    else:
        controls = ipywidgets.VBox(
            [
                load_raster_button,
                rasterization_widget,
                formwidget,
                classification,
            ]
        )
        return (
            controls,
            form,
            classification,
            rasterization_widget_form,
            load_raster_button,
        )


def assign_pipeline(dataset, segmentation, pipelines):
    """
    Load a segmentation object with one or more multipolygons and a list of pipelines.
    Each multipolygon can be assigned to one pipeline.


    :param segmentation:
        This segmentation object needs to have one multipolygon for every type of ground class (dense forrest, steep hill, etc..).
        If the segmentation is not in EPSG:4326 it must be converted first! See utils.convert_segmentation.
        It might be necessary to swap the lon and lat coordinates. See  segmentation.swap_coordinates
    :type: adaptivefiltering.segmentation.Segmentation

    :param pipelines:
        All pipelines that one wants to link with the given segmentations.

    :type: list of adaptivefiltering.filter.Pipeline


    :return:
        A segmentation object with added pipeline information
    :rtype:  adaptivefiltering.segmentation.Segmentation
    """

    # passes the segment to the _update_seg_pin function
    def on_button_clicked(b, layer_data=None):
        return _update_seg_marker(layer_data)

    # holds a segmentation and calculates
    def _update_seg_marker(layer_data):
        # initilizes a new marker
        layer_data.properties["style"]["color"] = "red"
        layer_data.properties["style"]["fillOpacity"] = "0"

        for layer in map_.map.layers:
            if layer.name == "Current Segmentation":
                map_.map.remove_layer(layer)

        map_.load_geojson(layer_data, "Current Segmentation")

    def _create_right_side_menu():
        right_side_label = ipywidgets.Label("Assign Pipelines to Segmentations")

        # needed to quickly check if all features have the same keys
        from itertools import groupby

        def _all_equal(iterable):
            g = groupby(iterable)
            return next(g, True) and not next(g, False)

        keys_list = [
            list(feature["properties"].keys()) for feature in segmentation["features"]
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

        feature_properties_options = [("no grouping", "")] + [
            (keys, keys) for keys in property_keys
        ]
        feature_dropdown = ipywidgets.Dropdown(
            options=feature_properties_options,
            layout=fullwidth,
        )

        right_side = ipywidgets.VBox(
            [
                feature_dropdown,
                right_side_label,
            ]
        )
        right_side, dropdown_list = _update_segmentation_pipeline_assignment(right_side)
        return right_side, dropdown_list

    def _update_segmentation_pipeline_assignment(right_side):
        # pipeline author has to be replaced with the storage location

        # the no pipeline option ensures, that the user picks one pipeline.
        dropdown_options = [("no Pipeline", "")] + [
            (pipeline.title, pipeline.author) for pipeline in pipelines
        ]
        # used for assigning dropdown_values to the segmentation_proxy
        dropdown_list = []

        # for every new feature we ccreate alocation button, a nametag and a dropdown menu with all pipeline options.
        for i, feature in enumerate(segmentation["features"]):

            label = ipywidgets.Label(
                f"Segmentation {i}",
                layout=ipywidgets.Layout(width="80%"),
            )
            map_.load_geojson(feature, label.value)
            last_layer = map_.map.layers[-1]
            button = ipywidgets.Button(
                icon="map-marker-alt", layout=ipywidgets.Layout(width="20%")
            )
            button.on_click(
                functools.partial(on_button_clicked, layer_data=last_layer.data)
            )

            new_dropdown = ipywidgets.Dropdown(
                options=dropdown_options,
                layout=fullwidth,
            )
            dropdown_list.append(new_dropdown)
            box = ipywidgets.VBox(
                children=[ipywidgets.HBox(children=[label, button]), new_dropdown]
            )

            right_side.children = right_side.children + (box,)
        return right_side, dropdown_list

    def _assign_pipelines():
        assigned_segmentation = copy.deepcopy(segmentation)

        for i, (feature, dropdown_widget) in enumerate(
            zip(assigned_segmentation["features"], dropdown_list)
        ):
            feature["properties"]["pipeline"] = dropdown_widget.value
        return assigned_segmentation

    dataset = PDALInMemoryDataSet.convert(dataset)

    controls, map_ = setup_overlay_control(
        dataset, with_map=True, inlude_draw_controle=False
    )
    map_widget = map_.show()
    map_widget.layout = fullwidth

    finalize = ipywidgets.Button(description="Finalize")
    finalize.layout = fullwidth

    # Create the overall app layout
    right_side, dropdown_list = _create_right_side_menu()

    # Add finalize to the controls widgets
    controls.children = (finalize,) + controls.children

    app = ipywidgets.AppLayout(
        header=None,
        left_sidebar=controls,
        center=map_widget,
        right_sidebar=right_side,
        footer=None,
        pane_widths=[1, 3, 1],
    )
    segmentation_proxy = return_proxy(lambda: _assign_pipelines(), dropdown_list)
    IPython.display.display(app)

    def _finalize_simple(_):
        app.layout.display = "none"

    finalize.on_click(_finalize_simple)

    return segmentation_proxy


def apply_restriction(dataset, segmentation=None):
    """The Jupyter UI to create a segmentation object from scratch.

    The use of this UI will soon be described in detail.
    """

    dataset = as_pdal(dataset)

    def apply_restriction(seg):
        from pyproj import crs

        # "EPSG:4326 specifically states that the coordinate order should be latitude, longitude.
        # Many software packages still use longitude, latitude ordering.
        # This situation has wreaked unimaginable havoc on project deadlines and programmer sanity."
        # https://gis.stackexchange.com/questions/3334/difference-between-wgs84-and-epsg4326
        epsg_4326 = crs.CRS("EPSG:4326")
        ds_crs = crs.CRS(dataset.spatial_reference)
        if epsg_4326 != ds_crs:
            seg = swap_coordinates(seg)
        # convert the segmentation from EPSG:4326 to the spatial reference of the dataset
        seg = convert_segmentation(seg, dataset.spatial_reference)

        # if multiple polygons have been selected they will be merged in one multipolygon
        # this guarentees, that len(seg[features]) is always 1.
        seg = merge_segmentation_features(seg)

        # Construct a WKT Polygon for the clipping
        # this will be either a single polygon or a multipolygon
        polygons = ogr.CreateGeometryFromJson(str(seg["features"][0]["geometry"]))
        polygons_wkt = polygons.ExportToWkt()

        from adaptivefiltering.pdal import execute_pdal_pipeline

        # Apply the cropping filter with all polygons
        newdata = execute_pdal_pipeline(
            dataset=dataset, config={"type": "filters.crop", "polygon": polygons_wkt}
        )

        return PDALInMemoryDataSet(
            pipeline=newdata,
            spatial_reference=dataset.spatial_reference,
        )

    # Maybe this is not meant to be interactive
    if segmentation is not None:
        return apply_restriction(segmentation)

    # If this is interactive, construct the widgets
    controls, map_ = setup_overlay_control(dataset, with_map=True)
    finalize = ipywidgets.Button(description="Finalize")
    segmentation_proxy = return_proxy(lambda: dataset, [])

    map_widget = map_.show()

    map_widget.layout = fullwidth
    finalize.layout = fullwidth

    # Add finalize to the controls widgets
    controls.children = (finalize,) + controls.children

    # Create the overall app layout
    app = ipywidgets.AppLayout(
        header=None,
        left_sidebar=controls,
        center=map_widget,
        right_sidebar=None,
        footer=None,
        pane_widths=[1, 3, 0],
    )

    # Show the final widget
    IPython.display.display(app)

    def _finalize_simple(_):
        app.layout.display = "none"
        segmentation_proxy.__wrapped__ = apply_restriction(map_.return_segmentation())

    finalize.on_click(_finalize_simple)

    return segmentation_proxy


def create_upload(filetype, finalization_hook=lambda x: x):
    """Create a Jupyter UI snippet that allows a user to upload a file

    :param filetype:
        The file extension to expect for the upload.
    :type filetype: str
    """

    confirm_button = ipywidgets.Button(
        description="Confirm upload",
        disabled=False,
        button_style="",  # 'success', 'info', 'warning', 'danger' or ''
        tooltip="Confirm upload",
        icon="check",  # (FontAwesome names without the `fa-` prefix)
    )
    upload = ipywidgets.FileUpload(
        accept=filetype,  # Accepted file extension e.g. '.txt', '.pdf', 'image/*', 'image/*,.pdf'
        multiple=True,  # True to accept multiple files upload else False
    )

    layout = ipywidgets.Layout(width="100%")
    confirm_button.layout = layout
    upload.layout = layout
    app = ipywidgets.VBox([upload, confirm_button])
    IPython.display.display(app)
    upload_proxy = return_proxy(lambda: upload, [upload])

    def _finalize(_):
        app.layout.display = "none"
        upload_proxy.__wrapped__ = finalization_hook(upload_proxy.__wrapped__)

    confirm_button.on_click(_finalize)
    return upload_proxy


def show_interactive(dataset, filtering_callback=None, update_classification=False):
    """The interactive UI to visualize a dataset

    :param dataset:
        The Lidar dataset to visualize
    :type dataset: adaptivefiltering.DataSet
    :param filtering_callback:
        A callback that is called to transform the dataset before visualization.
        This may be used to hook in additional functionality like the execution
        of a filtering pipeline
    :type pipeline: Callable
    :param update_classification:
        Whether or not the classification values shown in the UI need to be updated
        for each preview. Boils down to the question of whether :code:`filtering_callback`
        potentially changes the classification of the dataset.
    :type update_classification: bool
    """

    # If dataset is not rasterized already, do it now
    if not isinstance(dataset, DigitalSurfaceModel):
        dataset = dataset.rasterize()

    (
        controls,
        form,
        classification,
        rasterization_widget_form,
        load_raster_button,
    ) = setup_overlay_control(dataset)

    # Get a container widget for the visualization itself
    content = ipywidgets.Box([ipywidgets.Label("Currently rendering visualization...")])

    # Create the overall app layout
    app = ipywidgets.AppLayout(
        header=None,
        left_sidebar=controls,
        center=content,
        right_sidebar=None,
        footer=None,
        pane_widths=[1, 3, 0],
    )

    def trigger_visualization(b):
        with hourglass_icon(b):
            # Maybe call the given callback
            nonlocal dataset
            if filtering_callback is not None:
                dataset = filtering_callback(dataset.dataset).rasterize()

            # Maybe update the classification widget if necessary
            if update_classification:
                nonlocal classification
                classification.children = (classification_widget([dataset]),)

            # Rerasterize if necessary
            dataset = dataset.dataset.rasterize(
                classification=classification.children[0].value,
                **rasterization_widget_form.data,
            )

            # Trigger visualization
            app.center.children = (dataset.show(**form.data),)

    # Get a visualization button
    load_raster_button.on_click(trigger_visualization)

    # Click the button once to trigger initial visualization
    load_raster_button.click()

    return app


def select_pipeline_from_library(multiple=False):
    """The Jupyter UI to select filtering pipelines from libraries.

    The use of this UI is described in detail in `the notebook on filtering libraries`_.

    .. _the notebook on filtering libraries: libraries.nblink

    :param multiple:
        Whether or not it should be possible to select multiple filter pipelines.
    :type multiple: bool
    :return:
        Returns the selected pipeline object(s)
    :rtype: adaptivefiltering.filter.Pipeline
    """

    def library_name(lib):
        if lib.name is not None:
            return lib.name
        else:
            return lib.path

    # Collect checkboxes in the selection menu
    library_checkboxes = [
        ipywidgets.Checkbox(value=True, description=library_name(lib), indent=False)
        for lib in get_filter_libraries()
    ]
    backend_checkboxes = {
        name: ipywidgets.Checkbox(value=cls.enabled(), description=name, indent=False)
        for name, cls in Filter._filter_impls.items()
        if Filter._filter_is_backend[name]
    }

    # Extract all authors that contributed to the filter libraries
    def get_author(f):
        if f.author == "":
            return "(unknown)"
        else:
            return f.author

    all_authors = []
    for lib in get_filter_libraries():
        for f in lib.filters:
            all_authors.append(get_author(f))
    all_authors = list(sorted(set(all_authors)))

    # Create checkbox widgets for the all authors
    author_checkboxes = {
        author: ipywidgets.Checkbox(value=True, description=author, indent=False)
        for author in all_authors
    }

    # Use a TagsInput widget for keywords
    keyword_widget = ipywidgets.TagsInput(
        value=library_keywords(),
        allow_duplicates=False,
        tooltip="Keywords to filter for. Filters need to match at least one given keyword in order to be shown.",
    )

    # Create the filter list widget
    filter_list = []
    widget_type = ipywidgets.SelectMultiple if multiple else ipywidgets.Select
    filter_list_widget = widget_type(
        options=[f.title for f in filter_list],
        value=[] if multiple else None,
        description="",
        layout=fullwidth,
    )

    # Create the pipeline description widget
    metadata_schema = load_schema("pipeline.json")["properties"]["metadata"]
    metadata_form = WidgetFormWithLabels(metadata_schema, vertically_place_labels=True)

    def metadata_updater(change):
        # The details of how to access this from the change object differs
        # for Select and SelectMultiple
        if multiple:
            # Check if the change selected a new entry
            if len(change["new"]) > len(change["old"]):
                # If so, we display the metadata of the newly selected one
                (entry,) = set(change["new"]) - set(change["old"])
                metadata_form.data = pyrsistent.thaw(
                    filter_list[entry].config["metadata"]
                )
        else:
            metadata_form.data = pyrsistent.thaw(
                filter_list[change["new"]].config["metadata"]
            )

    filter_list_widget.observe(metadata_updater, names="index")

    # Define a function that allows use to access the selected filters
    def accessor():
        indices = filter_list_widget.index
        if indices is None:
            return None

        # Either return a tuple of filters or a single filter
        if multiple:
            return tuple(filter_list[i] for i in indices)
        else:
            return filter_list[indices]

    # A function that recreates the filtered list of filters
    def update_filter_list(_):
        filter_list.clear()

        # Iterate over all libraries to find filters
        for i, lbox in enumerate(library_checkboxes):
            # If the library is deactivated -> skip
            if not lbox.value:
                continue

            # Iterate over all filters in the given library
            for filter_ in get_filter_libraries()[i].filters:
                # If the filter uses a deselected backend -> skip
                if any(
                    not bbox.value and name in filter_.used_backends()
                    for name, bbox in backend_checkboxes.items()
                ):
                    continue

                # If the author of this pipeline has been deselected -> skip
                if not author_checkboxes[get_author(filter_)].value:
                    continue

                # If the filter does not have at least one selected keyword -> skip
                # Exception: No keywords are specified at all in the library (early dev)
                if library_keywords():
                    if not set(keyword_widget.value).intersection(
                        set(filter_.keywords)
                    ):
                        continue

                # Once we got here we use the filter
                filter_list.append(filter_)

        # Update the widget
        nonlocal filter_list_widget
        filter_list_widget.value = [] if multiple else None
        filter_list_widget.options = [f.title for f in filter_list]

    # Trigger it once in the beginning
    update_filter_list(None)

    # Make all checkbox changes trigger the filter list update
    for box in itertools.chain(
        library_checkboxes, backend_checkboxes.values(), author_checkboxes.values()
    ):
        box.observe(update_filter_list, names="value")

    # Piece all of the above selcetionwidgets together into an accordion
    acc = ipywidgets.Accordion(
        children=[
            ipywidgets.VBox(children=tuple(library_checkboxes)),
            ipywidgets.VBox(children=tuple(backend_checkboxes.values())),
            keyword_widget,
            ipywidgets.VBox(children=tuple(author_checkboxes.values())),
        ],
        titles=["Libraries", "Backends", "Keywords", "Author"],
    )

    button = ipywidgets.Button(description="Finalize", layout=fullwidth)

    # Piece things together into an app layout
    app = ipywidgets.AppLayout(
        left_sidebar=acc,
        center=filter_list_widget,
        right_sidebar=ipywidgets.VBox([button, metadata_form.widget]),
        pane_widths=(1, 1, 1),
    )
    IPython.display.display(app)

    # Return proxy handling
    proxy = return_proxy(accessor, [filter_list_widget])

    def _finalize(_):
        # If nothing has been selected, the finalize button is no-op
        if accessor():
            app.layout.display = "none"

    button.on_click(_finalize)

    return proxy


def select_pipelines_from_library():
    """The Jupyter UI to select filtering pipelines from libraries.

    The use of this UI is described in detail in `the notebook on filtering libraries`_.

    .. _the notebook on filtering libraries: libraries.nblink

    :return:
        Returns the selected pipeline object(s)
    :rtype: adaptivefiltering.filter.Pipeline
    """

    return select_pipeline_from_library(multiple=True)


def select_best_pipeline(dataset=None, pipelines=None):
    """Select the best pipeline for a given dataset.

    The use of this UI is described in detail in `the notebook on selecting filter pipelines`_.

    .. _the notebook on selecting filter pipelines: selection.nblink

    :param dataset:
        The dataset to use for visualization of ground point filtering results
    :type dataset: adaptivefiltering.DataSet
    :param pipelines:
        The tentative list of pipelines to try. May e.g. have been selected using
        the select_pipelines_from_library tool.
    :type pipelines: list
    :return:
        The selected pipeline with end user configuration baked in
    :rtype: adaptivefiltering.filter.Pipeline
    """
    if dataset is None:
        raise AdaptiveFilteringError("A dataset is required for 'select_best_pipeline'")

    if pipelines is None:
        raise AdaptiveFilteringError(
            "At least one pipeline needs to be passed to 'select_best_pipeline'"
        )

    # Finalize button
    finalize = ipywidgets.Button(
        description="Finalize (including end-user configuration into filter)",
        layout=ipywidgets.Layout(width="100%"),
    )

    # Per-pipeline data structures to keep track off
    subwidgets = []
    pipeline_accessors = []

    # Subwidget generator function
    def interactive_pipeline(p):
        # A widget that contains the variability
        varform = ipywidgets_jsonschema.Form(
            p.variability_schema, vertically_place_labels=True, use_sliders=False
        )

        # Piggy-back onto the visualization app
        vis = show_interactive(
            dataset,
            filtering_callback=lambda ds: cached_pipeline_application(
                ds, p, **varform.data
            ),
            update_classification=True,
        )

        # Insert the variability form
        vis.right_sidebar = ipywidgets.VBox(
            children=[ipywidgets.Label("Customization points:"), varform.widget]
        )
        vis.pane_widths = [1, 2, 1]

        # Insert the generated widgets into the outer structures
        subwidgets.append(vis)

        pipeline_accessors.append(
            lambda: p.copy(**p._modify_filter_config(varform.data))
        )

    # Trigger subwidget generation for all pipelines
    for p in pipelines:
        interactive_pipeline(p)

    # Tabs that contain the interactive execution with all given pipelines
    if len(subwidgets) > 1:
        tabs = ipywidgets.Tab(
            children=subwidgets, titles=[f"#{i}" for i in range(len(pipelines))]
        )
    elif len(subwidgets) == 1:
        tabs = subwidgets[0]
    else:
        tabs = ipywidgets.Box()

    app = ipywidgets.VBox([finalize, tabs])
    IPython.display.display(app)

    def _return_handler():
        # Get the current selection index of the Tabs widget (if any)
        if len(subwidgets) > 1:
            index = tabs.selected_index
        elif len(subwidgets) == 1:
            index = 0
        else:
            return Pipeline()

        return pipeline_accessors[index]()

    # Return proxy handling
    proxy = return_proxy(_return_handler, [tabs])

    def _finalize(_):
        app.layout.display = "none"

    finalize.on_click(_finalize)

    return proxy


def execute_interactive(dataset, pipeline):
    """Interactively apply a filter pipeline to a given dataset in Jupyter

    This allows you to interactively explore the effects of end user configuration
    values specified by the filtering pipeline.

    :param dataset:
        The dataset to work on
    :type dataset: adaptivefiltering.DataSet
    :param pipeline:
        The pipeline to execute.
    :type pipelines: adaptivefiltering.filter.Pipeline
    :return:
        The pipeline with the end user configuration baked in
    :rtype: adaptivefiltering.filter.Pipeline
    """

    return select_best_pipeline(dataset=dataset, pipelines=[pipeline])
