from operator import attrgetter
import re
import itertools
from django.db.utils import DatabaseError

from django.utils.translation import gettext as _
from django.shortcuts import redirect, render
from django.contrib.gis.geos import Point
from django.contrib.gis.geos.geometry import GEOSGeometry
from django.contrib.gis.db.models import Collect
from django.views.decorators.csrf import csrf_exempt
from django.db.models import FloatField
from django.db.models.expressions import Func
from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.db.backends.postgis.adapter import PostGISAdapter

from mapit.models import Postcode, Area, Generation
from mapit.utils import is_valid_postcode, is_valid_partial_postcode
from mapit.shortcuts import output_json, get_object_or_404, set_timeout
from mapit.middleware import ViewException
from mapit.ratelimitcache import ratelimit
from mapit.views.areas import add_codes
from mapit import countries

# Stupid fixed IDs from old MaPit
WMP_AREA_ID = 900000
EUP_AREA_ID = 900001
LAE_AREA_ID = 900002
SPA_AREA_ID = 900003
WAS_AREA_ID = 900004
NIA_AREA_ID = 900005
LAS_AREA_ID = 900006
HOL_AREA_ID = 900007
HOC_AREA_ID = 900008
enclosing_areas = {
    'LAC': [LAE_AREA_ID, LAS_AREA_ID],
    'SPC': [SPA_AREA_ID],
    'WAC': [WAS_AREA_ID],
    'NIE': [NIA_AREA_ID],
    'WMC': [WMP_AREA_ID],
    'EUR': [EUP_AREA_ID],
}


class GeometryCentroidDistance(Func):
    function = ''
    arg_joiner = ' <-> '

    def __init__(self, expression, geom, **extra):
        if not isinstance(geom, GEOSGeometry):
            raise TypeError("Please provide a geometry object.")
        if not hasattr(geom, 'srid') or not geom.srid:
            raise ValueError("Please provide a geometry attribute with a defined SRID.")
        super(GeometryCentroidDistance, self).__init__(
            expression, PostGISAdapter(geom), output_field=FloatField(), **extra)


@ratelimit
def postcode(request, postcode, format=None):
    if hasattr(countries, 'canonical_postcode'):
        canon_postcode = countries.canonical_postcode(postcode)
        postcode = canon_postcode
        # if (postcode != canon_postcode and format is None) or format == 'json':
        #     return redirect('mapit.views.postcodes.postcode', postcode=canon_postcode)
    if format is None:
        format = 'json'
    if not is_valid_postcode(postcode):
        raise ViewException(format, "Postcode '%s' is not valid." % postcode, 400)
    postcode = get_object_or_404(Postcode, format=format, postcode=postcode)
    try:
        generation = int(request.GET['generation'])
    except:
        generation = Generation.objects.current()
    if not hasattr(countries, 'is_special_postcode') or not countries.is_special_postcode(postcode.postcode):
        areas = list(add_codes(Area.objects.by_postcode(postcode, generation)))
    else:
        areas = []

    # Shortcuts
    shortcuts = {}
    for area in areas:
        if area.type.code in ('COP', 'LBW', 'LGE', 'MTW', 'UTE', 'UTW'):
            shortcuts['ward'] = area.id
            shortcuts['council'] = area.parent_area_id
        elif area.type.code == 'CED':
            shortcuts.setdefault('ward', {})['county'] = area.id
            shortcuts.setdefault('council', {})['county'] = area.parent_area_id
        elif area.type.code == 'DIW':
            shortcuts.setdefault('ward', {})['district'] = area.id
            shortcuts.setdefault('council', {})['district'] = area.parent_area_id
        elif area.type.code in ('WMC',):
            # XXX Also maybe 'EUR', 'NIE', 'SPC', 'SPE', 'WAC', 'WAE', 'OLF', 'OLG', 'OMF', 'OMG'):
            shortcuts[area.type.code] = area.id

    # Add manual enclosing areas.
    extra = []
    for area in areas:
        if area.type.code in enclosing_areas.keys():
            extra.extend(enclosing_areas[area.type.code])
    areas = itertools.chain(areas, Area.objects.filter(id__in=extra))

    if format == 'html':
        return render(request, 'mapit/postcode.html', {
            'postcode': postcode.as_dict(),
            'areas': areas,
            'json_view': 'mapit-postcode',
        })

    out = postcode.as_dict()
    out['areas'] = dict((area.id, area.as_dict()) for area in areas)
    if shortcuts:
        out['shortcuts'] = shortcuts
    return output_json(out)


@ratelimit
def partial_postcode(request, postcode, format=''):
    postcode = re.sub(r'\s+', '', postcode.upper())
    if is_valid_postcode(postcode):
        postcode = re.sub(r'\d[A-Z]{2}$', '', postcode)
    if not is_valid_partial_postcode(postcode):
        raise ViewException(format, "Partial postcode '%s' is not valid." % postcode, 400)

    location = Postcode.objects.filter(postcode__startswith=postcode).extra(
        where=['length(postcode) = %d' % (len(postcode) + 3)]
        ).aggregate(Collect('location'))['location__collect']
    if not location:
        raise ViewException(format, 'Postcode not found', 404)

    postcode = Postcode(postcode=postcode, location=location.centroid)

    if format == 'html':
        return render(request, 'mapit/postcode.html', {
            'postcode': postcode.as_dict(),
            'json_view': 'mapit-postcode-partial',
        })

    return output_json(postcode.as_dict())


@ratelimit
def example_postcode_for_area(request, area_id, format=''):
    area = get_object_or_404(Area, format=format, id=area_id)
    try:
        pc = Postcode.objects.filter(areas=area).order_by()[0]
    except:
        set_timeout(format)
        try:
            pc = Postcode.objects.filter_by_area(area, limit=1)[0]
        except DatabaseError as e:
            if 'canceling statement due to statement timeout' not in e.args[0] \
               and 'canceling statement due to user request' not in e.args[0]:
                raise
            raise ViewException(format, 'That query was taking too long to compute.', 500)
        except:
            pc = None
    if pc:
        pc = pc.get_postcode_display()
    if format == 'html':
        return render(request, 'mapit/example-postcode.html', {'area': area, 'postcode': pc})
    return output_json(pc)


@csrf_exempt
def form_submitted(request):
    pc = request.POST.get('pc', '')
    if hasattr(countries, 'canonical_postcode'):
        pc = countries.canonical_postcode(pc)
    if not request.method == 'POST' or not pc:
        return redirect('/')
    return redirect('mapit-postcode', postcode=pc, format='html')


@ratelimit
def nearest(request, srid, x, y, format=''):
    location = Point(float(x), float(y), srid=int(srid))
    set_timeout(format)

    try:
        # Transform to database SRID for comparison (GeometryCentroidDistance does not yet do this)
        location.transform(4326)
    except:
        raise ViewException(format, _('Point outside the area geometry'), 400)

    try:
        # Ordering will be in 'degrees', so fetch a few and sort by actual distance
        postcodes = Postcode.objects.annotate(
            centroid_distance=GeometryCentroidDistance('location', location)
            ).annotate(
                distance=Distance('location', location)
            ).order_by('centroid_distance')[:100]
        postcodes = sorted(postcodes, key=attrgetter('distance'))
        postcode = postcodes[0]
    except DatabaseError as e:
        if 'Cannot find SRID' in e.args[0]:
            raise ViewException(format, e.args[0], 400)
        if 'canceling statement due to statement timeout' not in e.args[0] \
           and 'canceling statement due to user request' not in e.args[0]:
            raise
        raise ViewException(format, 'That query was taking too long to compute.', 500)
    except:
        raise ViewException(format, 'No postcode found near %s,%s (%s)' % (x, y, srid), 404)

    if format == 'html':
        return render(request, 'mapit/postcode.html', {
            'postcode': postcode.as_dict(),
            'json_view': 'mapit-postcode',
        })

    pc = postcode.as_dict()
    pc['distance'] = round(postcode.distance.m)
    return output_json({
        'postcode': pc,
    })
