# -*- coding=utf -*-
from ...browser import *
from ...errors import *
from ...model import *
from ...common import get_logger
from ...statutils import *
from .aggregator import _MixpanelResponseAggregator
from .utils import *

from ...statutils import calculators_for_aggregates, CALCULATED_AGGREGATIONS

from .store import DEFAULT_TIME_HIERARCHY

import datetime
import calendar
from collections import OrderedDict, defaultdict

_aggregate_param = {
        "total": "general",
        "unique": "unique",
        "average": "average"
    }

class MixpanelBrowser(AggregationBrowser):
    def __init__(self, cube, store, locale=None, metadata=None, **options):
        """Creates a Mixpanel aggregation browser.

        Requirements and limitations:

        * `time` dimension should always be present in the drilldown
        * only one other dimension is allowd for drilldown
        * range cuts assume numeric dimensions
        * unable to drill-down on `year` level, will default to `month`
        """
        self.store = store
        self.cube = cube
        self.options = options
        self.logger = get_logger()

    def features(self):
        """Return SQL features. Currently they are all the same for every
        cube, however in the future they might depend on the SQL engine or
        other factors."""

        features = {
            "actions": ["aggregate"],
            "aggregate_functions": [],
            "post_aggregate_functions": available_calculators()
        }

        return features

    def aggregate(self, cell=None, measures=None, aggregates=None,
                  drilldown=None, split=None, **options):

        if split:
            raise BrowserError("split in mixpanel is not supported")

        if measures:
            raise ArgumentError("Mixpanel does not provide non-aggregated "
                                "measures")
        if aggregates:
            aggregates = self.cube.get_aggregates(aggregates)
        else:
            aggregates = self.cube.aggregates

        # All aggregates without a function can be considered as "native" as
        # they are handled specially.
        # If there is an explicit aggregate fucntion it is a post-aggregate
        # computation
        aggregate_names = [a.name for a in aggregates]
        native_aggregates = [a for a in aggregates if not a.function]
        native_aggregate_names = [a.name for a in native_aggregates]

        # Get the cell and prepare cut parameters
        cell = cell or Cell(self.cube)

        #
        # Prepare drilldown
        #
        drilldown = Drilldown(drilldown, cell)

        if "time" in drilldown and len(drilldown) > 2:
            raise ArgumentError("Can not drill down with more than one "
                                "non-time dimension in mixpanel")

        #
        # Create from-to date range from time dimension cut
        #
        time_cut = cell.cut_for_dimension("time")
        time_hierarchy = time_cut.hierarchy if time_cut else DEFAULT_TIME_HIERARCHY

        if not time_cut:
            path_time_from = []
            path_time_to = []
        elif isinstance(time_cut, PointCut):
            path_time_from = time_cut.path or []
            path_time_to = time_cut.path or []
        elif isinstance(time_cut, RangeCut):
            path_time_from = time_cut.from_path or []
            path_time_to = time_cut.to_path or []
        else:
            raise ArgumentError("Mixpanel does not know how to handle cuts "
                                "of type %s" % type(time_cut))

        path_time_from = coalesce_date_path(path_time_from, 0, time_hierarchy)
        path_time_to = coalesce_date_path(path_time_to, 1, time_hierarchy)

        params = {
                "from_date": path_time_from.strftime("%Y-%m-%d"),
                "to_date": path_time_to.strftime("%Y-%m-%d")
            }

        time_level = drilldown.last_level("time")
        if time_level:
            time_level = str(time_level)

        # time_level - as requested by the caller
        # actual_time_level - time level in the result (dim.hierarchy
        #                     labeling)
        # mixpanel_unit - mixpanel request parameter

        if not time_level or time_level == "year":
            mixpanel_unit = actual_time_level = "month"
            # Get the default hierarchy
        elif time_level == "date":
            mixpanel_unit = "day"
            actual_time_level = "date"
        else:
            mixpanel_unit = actual_time_level = str(time_level)

        if time_level != actual_time_level:
            self.logger.debug("Time drilldown coalesced from %s to %s" % \
                                    (time_level, actual_time_level))

        if time_level and time_level not in self.cube.dimension("time").level_names:
            raise ArgumentError("Can not drill down time to '%s'" % time_level)

        # Get drill-down dimension (mixpanel "by" segmentation menu)
        # Assumption: first non-time

        drilldown_on = None
        for obj in drilldown:
            if obj.dimension.name != "time":
                drilldown_on = obj

        if drilldown_on:
            params["on"] = 'properties["%s"]' % \
                                    self._property(drilldown_on.dimension)

        cuts = [cut for cut in cell.cuts if str(cut.dimension) != "time"]

        #
        # The Conditions
        # ==============
        #
        # Create 'where' condition from cuts
        # Assumption: all dimensions are flat dimensions

        conditions = []
        for cut in cuts:
            if isinstance(cut, PointCut):
                condition = self._point_condition(cut.dimension, cut.path[0], cut.invert)
                conditions.append(condition)
            elif isinstance(cut, RangeCut):
                condition = self._range_condition(cut.dimension,
                                                  cut.from_path[0],
                                                  cut.to_path[0], cut.invert)
                conditions.append(condition)
            elif isinstance(cut, SetCut):
                set_conditions = []
                for path in cut.paths:
                    condition = self._point_condition(cut.dimension, path[0])
                    set_conditions.append(condition)
                condition = " or ".join(set_conditions)
                conditions.append(condition)

        if len(conditions) > 1:
            conditions = [ "(%s)" % cond for cond in conditions ]
        if conditions:
            condition = " and ".join(conditions)
            params["where"] = condition

            self.logger.debug("condition: %s" % condition)

        if "limit" in options:
            params["limit"] = options["limit"]

        #
        # The request
        # ===========
        # Perform one request per measure aggregate.
        #
        # TODO: use mapper
        event_name = self.cube.name

        # Collect responses for each measure aggregate
        #
        # Note: we are using `segmentation` MXP request by default except for
        # the `unique` measure at the `all` or `year` aggregation level.
        responses = {}

        for aggregate in native_aggregate_names:
            params["type"] = _aggregate_param[aggregate]

            if aggregate == "unique" and (not time_level or time_level == "year"):
                response = self._arb_funnels_request(event_name, params,
                                                     drilldown_on)
            else:
                response = self._segmentation_request(event_name, params,
                                                      mixpanel_unit)

            responses[aggregate] = response

        # TODO: get this: result.total_cell_count = None
        # TODO: compute summary

        #
        # The Result
        # ==========
        #

        result = AggregationResult(cell, aggregates)
        result.cell = cell

        aggregator = _MixpanelResponseAggregator(self, responses,
                                                 native_aggregate_names,
                                                 drilldown, actual_time_level)

        result.levels = drilldown.levels_dictionary()

        labels = aggregator.time_levels[:]
        if drilldown_on:
            labels.append(drilldown_on.dimension.name)

        labels += aggregate_names
        result.labels = labels

        if drilldown or split:
            self.logger.debug("CALCULATED AGGS because drilldown or split")
            result.calculators = calculators_for_aggregates(aggregates,
                                                            drilldown,
                                                            split,
                                                            None)
            result.cells = aggregator.cells

        # add calculated measures w/o drilldown or split if no drilldown or split
        else:
            self.logger.debug("CALCULATED AGGS ON SUMMARY")
            result.summary = aggregator.cells[0]
            result.cells = []
            calculators = calculators_for_aggregates(aggregates,
                                                     drilldown,
                                                     split,
                                                     None)
            for calc in calculators:
                calc(result.summary)

        return result

    def _segmentation_request(self, event_name, params, unit):
        """Perform Mixpanel request ``segmentation`` – this is the default
        request."""
        params = dict(params)
        params["event"] = event_name
        params["unit"] = unit

        response = self.store.request(["segmentation"], params)

        self.logger.debug(response['data'])
        return response

    def _arb_funnels_request(self, event_name, params, drilldown_on):
        """Perform Mixpanel request ``arb_funnels`` for measure `unique` with
        granularity of whole cube (all) or year."""
        params = dict(params)

        params["events"] = [{"event":event_name}]
        params["interval"] = 90
        params["type"] = _aggregate_param["unique"]

        response = self.store.request(["arb_funnels"], params)

        # TODO: remove this debug once satisfied (and below)
        # txt = dumps(response, indent=4)
        # self.logger.info("MXP response: \n%s" % (txt, ))

        # Convert the arb_funnels Mixpanel response to segmentation kind of
        # response.

        # Prepare the structure – only geys processed by the aggregator are
        # needed

        try:
            groups = response["meta"]["property_values"]
            is_drilldown = True
        except KeyError:
            groups = event_name
            is_drilldown = False

        result = { "data": {"values": {} } }

        for group in groups:
            values = result["data"]["values"].setdefault(group, {})

            point_key = group if is_drilldown else "steps"

            for date_key, data_point in response["data"].items():
                values[date_key] = data_point[point_key][0]["count"]

        # txt = dumps(result, indent=4)
        # self.logger.info("Converted response: \n%s" % (txt, ))

        return result

    def _property(self, dim):
        """Return correct property name from dimension."""
        dim = str(dim)
        return self.cube.mappings.get(dim, dim)

    def _point_condition(self, dim, value, invert):
        """Returns a point cut for flat dimension `dim`"""

        op = '!=' if invert else '=='
        condition = '(string(properties["%s"]) %s "%s")' % \
                        (self._property(dim), op, str(value))
        return condition

    def _range_condition(self, dim, from_value, to_value, invert):
        """Returns a point cut for flat dimension `dim`. Assumes number."""

        condition_tmpl = (
            '(number(properties["%s"]) >= %s and number(properties["%s"]) <= %s)' if not invert else
            '(number(properties["%s"]) < %s or number(properties["%s"]) > %s)'
            )

        condition = condition_tmpl % (self._property(dim), from_value, self._property(dim), to_value)
        return condition

