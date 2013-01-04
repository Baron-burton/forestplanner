from trees.models import Stand, ForestProperty, IdbSummary
from django.contrib.gis.gdal import DataSource
from django.contrib.gis.gdal.error import OGRIndexError
from django.conf import settings
from madrona.common.utils import get_logger
from django.db.models import Min, Max, Avg
from django.contrib.gis.geos import GEOSGeometry
from shapely.ops import cascaded_union
import numpy as np
from scipy.spatial import KDTree
import math

logger = get_logger()

class StandImporter:

    def __init__(self, user):
        self.user = user
        self.required_fields = ['name']
        self.optional_fields = ['domspp','rx'] # model must provide defaults!

    def _validate_field_mapping(self, layer, field_mapping):
        fields = layer.fields
        if not field_mapping:
            field_mapping = {}

        for fname in self.required_fields:
            if fname not in field_mapping.keys():
                # if not mapped, try the attribute name directly
                field_mapping[fname] = fname

            if field_mapping[fname] not in fields:
                raise Exception("Dataset does not have a required field called '%s'" % field_mapping[fname])

        for fname in self.optional_fields:
            if fname not in field_mapping.keys():
                # if not mapped, try the attribute name directly
                field_mapping[fname] = fname
            
        return field_mapping

    def import_ogr(self, shp_path, field_mapping=None, layer_num=0, 
            forest_property=None, new_property_name=None, pre_impute=False):
        ds = DataSource(shp_path)
        layer = ds[0]
        num_features = len(layer)
        field_mapping = self._validate_field_mapping(layer, field_mapping)

        if not forest_property and not new_property_name:
            raise Exception("Must provide either existing forest_property OR new_property_name")

        if new_property_name:
            # Calculating property outline from stands
            stands = []
            for feature in layer:
                stands.append(wkt.loads(feature.geom.wkt))
            casc_poly = cascaded_union(stands)

            if casc_poly.type == 'MultiPolygon':
                polys = []
                for c in casc_poly:
                    # Identify small 'slivers' or areas of empty space b/t polygons that are unintentional
                    # If they're smaller than the threshold, remove them
                    interiors = [x for x in c.interiors if Polygon(x).area > settings.SLIVER_THRESHOLD]
                    polys.append(Polygon(shell=c.exterior, holes=interiors))
            elif casc_poly.type == 'Polygon':
                # Identify small 'slivers' or areas of empty space b/t polygons that are unintentional
                # If they're smaller than the threshold, remove them
                interiors = [x for x in casc_poly.interiors if Polygon(x).area > settings.SLIVER_THRESHOLD]
                polys = [Polygon(shell=casc_poly.exterior, holes=interiors)]

            casc = MultiPolygon(polys)
            
            # Creating Property
            self.forest_property = ForestProperty.objects.create(user=self.user, name=new_property_name, geometry_final=casc.wkt)
        else: 
            self.forest_property = forest_property

        stands = []
        for feature in layer:
            stand = Stand(user=self.user, 
                    name=feature.get(field_mapping['name']), 
                    geometry_orig=feature.geom.geos)
                    #geometry_final=feature.geom.geos) 

            for fname in self.optional_fields:
                if fname in field_mapping.keys():
                    try:
                        stand.__dict__[fname] = feature.get(field_mapping[fname])
                    except OGRIndexError: 
                        pass

            stand.full_clean()
            stands.append(stand)
            del stand

        for stand in stands:
            stand.save()
            self.forest_property.add(stand)
            if pre_impute:
                tmp = stand.geojson()

def calculate_adjacency(qs, threshold):
    """
    Determines a matrix of adjacent polygons from a Feature queryset.
    Since the data are not topological, adjacency is determined
    by a minimum distance threshold.
    """
    features = list(qs)
    adj = {}

    for feat in features:
        fid = feat.pk
        adj[fid] = []

        geom_orig = feat.geometry_final
        geom_buf = feat.geometry_final.buffer(threshold)

        filterqs = qs.filter(geometry_final__bboverlaps = geom_buf, geometry_final__intersects = geom_buf)
        for feat2 in filterqs:
            fid2 = feat2.pk
            if fid == fid2:
                continue
            adj[fid].append(fid2)

        if len(adj[fid]) == 0:
            adj[fid] = None

        del filterqs

    return adj


class NoPlotMatchError(Exception):
    pass

def nearest_plots(categories, input_params, weight_dict, k=5):
    search_params = input_params.copy()
    keys = search_params.keys()
    for sp in keys:
        categories[sp+"__isnull"] = False

    stand_centroid = None
    if 'latitude_fuzz' in keys and 'longitude_fuzz' in keys:
        stand_centroid = GEOSGeometry('SRID=4326;POINT(%f %f)' % (input_params['longitude_fuzz'], input_params['latitude_fuzz']))
        stand_centroid.transform(settings.EQD_SRID)

    def plot_attrs(ps, keys):
        vals = []
        for attr in keys:
            vals.append(ps.__dict__[attr])

        # an additional special case
        if 'calc_aspect' in keys:
            angle = angular_diff(ps.calc_aspect, input_params['calc_aspect'])
        else:
            angle = 0
        vals.append(angle)
        search_params['_aspect'] = 0 # anglular difference to self is 0

        # Deal with latlon, another special case
        if stand_centroid:
            plot_centroid = ps.eqd_point
            distance = stand_centroid.distance(plot_centroid)
            vals.append(distance)
        else: 
            vals.append(0)
        search_params['_geographic'] = 0 # distance to self is 0
        return vals

    plotsum_qs = IdbSummary.objects.filter(**categories)
    plotsummaries = list(plotsum_qs)
    ps_attr_list= [plot_attrs(ps, keys) for ps in plotsummaries]

    # include our additional special cases
    keys.append('_aspect') 
    keys.append('_geographic')

    num_candidates = len(plotsummaries)
    if num_candidates == 0:
        raise NoPlotMatchError("There are no candidate plots matching the categorical variables: %s" % categories)

    print keys
    weights = np.ones(len(keys))
    for i in range(len(keys)):
        key = keys[i]
        if key in weight_dict:
            weights[i] = weight_dict[key]

    querypoint = np.array([float(search_params[attr]) for attr in keys])

    rawpoints = np.array(ps_attr_list) 

    # Normalize to max of 100; linear scale
    multipliers = (100.0 / np.max(rawpoints, axis=0))
    # Apply weights
    multipliers = multipliers * weights
    multipliers = np.nan_to_num(multipliers)
    # Apply multipliers
    allpoints = rawpoints * multipliers
    querypoint *= multipliers

    # Create tree and query it for nearest plot
    tree = KDTree(allpoints)
    querypoints = np.array([querypoint])
    result = tree.query(querypoints, k=k)
    distances = result[0][0]
    plots = result[1][0]

    top = zip(plots, distances)
    ps = []

    xs = [100 * x for x in weights if x > 0]
    squares = [x * x for x in xs]
    print squares
    print distances
    max_dist = math.sqrt(sum(squares)) # the real max 
    for t in top:
        try:
            p = plotsummaries[t[0]]
            p.__dict__['_kdtree_distance'] = t[1]
            # decrease certainty as the number of variables goes up?
            p.__dict__['_certainty'] = 1.0 - ((t[1] / max_dist) * len(squares)) 
        except:
            pass
        ps.append(p)
    return ps, num_candidates


def classify_aspect(angle):
    '''
    inspired by http://xkcd.com/cyborg.py
    '''
    try:
        angle = float(angle)
    except:
        return ""

    while angle > 360.0:
        angle = angle - 360.0
    angle += 22.5
    angle = int(angle/45.0)
    words=["North", "North-East", "East", "South-East", "South", "South-West", "West", "North-West", "North"]
    return words[angle]

def angular_diff(x,y):
    '''
    input: two angles in degrees
    output: absolute value of the angular difference in degrees
    '''
    import math
    x = math.radians(x)
    y = math.radians(y)
    return math.fabs(math.degrees(min(y-x, y-x+2*math.pi, y-x-2*math.pi, key=abs)))

def potential_minmax(categories, weight_dict):
    ps = IdbSummary.objects.filter(**categories)
    keys = [k for k in weight_dict.keys() if not k.startswith("_")]
    args = [Min(k) for k in keys] + [Max(k) for k in keys] + [Avg(k) for k in keys]
    return ps.aggregate(*args)
