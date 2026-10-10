"""UHI Explorer - backend (Flask + Google Earth Engine)

เป้าหมาย: หาย่านที่ควรปรับปรุงเพื่อลดความร้อนให้ผู้อยู่อาศัย
  เร่งด่วน = ร้อนมาก + คนอยู่หนาแน่น + พื้นที่สีเขียวน้อย
  แล้วดูต่อว่ามี "ที่โล่ง" ที่อาจทำเป็นพื้นที่สีเขียวได้หรือไม่

ข้อมูล (วิเคราะห์บนกริด 1 กม. เดียวกับข้อมูลอุณหภูมิ)
  - อุณหภูมิพื้นผิวช่วงบ่าย 12-14 น. : MODIS Aqua (2003-2022), VIIRS (2023 ขึ้นไป)
  - พื้นผิว (สิ่งปลูกสร้างรวมถนน, ต้นไม้, หญ้า, ดินเปล่า) : Dynamic World 10 ม. (2016 ขึ้นไป)
  - สิ่งปลูกสร้างย้อนหลัง 20 ปี : GHSL GHS_BUILT_S (ทุก 5 ปี)
  - ประชากร : GHSL GHS_POP (ทุก 5 ปี)
"""
import json
import os
import tempfile
from datetime import datetime

import ee
from flask import Flask, jsonify, render_template, request

PROJECT_ID = os.environ.get('EE_PROJECT', 'hicm-510809')


def init_earth_engine():
    """บนเว็บจริง: ใช้ service account key จาก environment variable
    บนเครื่องตัวเอง: ใช้การล็อกอินจาก `earthengine authenticate`"""
    key_json = os.environ.get('EE_SERVICE_ACCOUNT_KEY')
    if key_json:
        info = json.loads(key_json)
        key_path = os.path.join(tempfile.gettempdir(), 'ee-service-account.json')
        with open(key_path, 'w', encoding='utf-8') as f:
            f.write(key_json)
        credentials = ee.ServiceAccountCredentials(info['client_email'], key_path)
        ee.Initialize(credentials, project=PROJECT_ID)
    else:
        ee.Initialize(project=PROJECT_ID)


init_earth_engine()

app = Flask(__name__)

THAILAND = ee.Geometry.Rectangle([97.3, 5.6, 105.7, 20.5])

HOUR_MIN, HOUR_MAX = 12, 14      # ช่วงเวลาสังเกต (เวลาท้องถิ่นสุริยะ)
FIRST_YEAR = 2003                # ปีแรกที่ Aqua มีข้อมูลเต็มปี
AQUA_LAST_YEAR = 2022            # หลังจากนี้ Aqua เลื่อนเวลาผ่านจนเกิน 14 น.
DW_FIRST_YEAR = 2016             # ปีแรกที่ Dynamic World มีข้อมูลเต็มปี
LAST_YEAR = datetime.now().year - 1
DEFAULT_YEAR = min(2022, LAST_YEAR)

# ------------------------------------------------------------ เกณฑ์ (ปรับได้ที่นี่)
LST_BREAKS = [30, 34, 38, 42]    # ระดับสีอุณหภูมิ (°C)
LST_PALETTE = ['#2b83ba', '#abdda4', '#ffffbf', '#fdae61', '#d7191c']
HOT = 34                         # ร้อน (°C) -> เฝ้าระวัง
VERY_HOT = 38                    # ร้อนมาก (°C) -> เร่งด่วน
POP_ANY = 300                    # คน/ตร.กม. ขึ้นไป = มีคนอยู่ (นิยาม urban cluster ของ GHSL)
POP_DENSE = 1500                 # คน/ตร.กม. ขึ้นไป = หนาแน่น (นิยาม urban centre ของ GHSL)
GREEN_LOW = 0.15                 # พื้นที่สีเขียวน้อยกว่านี้ = เขียวน้อย
OPEN_MIN = 0.05                  # ที่โล่งตั้งแต่นี้ (สัดส่วนของช่อง) = มีที่โล่ง
PRIO_COLORS = ['#f2c14e', '#f08a24', '#d1352b']  # เฝ้าระวัง / เร่งด่วน-ไม่มีที่โล่ง / เร่งด่วน-มีที่โล่ง
POINT_RADIUS_M = 2000            # คลิกจุด = วิเคราะห์วงกลมรัศมีนี้

SEASONS = {'all': 'ทั้งปี', 'dry': 'หน้าแล้ง (พ.ย.–เม.ย.)', 'wet': 'หน้าฝน (พ.ค.–ต.ค.)'}

BUILT_EDGES = [0.1, 0.3, 0.5, 0.7]
BUILT_LABELS = ['ต่ำกว่า 10%', '10–30%', '30–50%', '50–70%', '70% ขึ้นไป']
GREEN_EDGES = [0.1, 0.2, 0.3, 0.5]
GREEN_LABELS = ['ต่ำกว่า 10%', '10–20%', '20–30%', '30–50%', '50% ขึ้นไป']

# ช่วงเปรียบเทียบ 20 ปี: (ปีของข้อมูลสิ่งปลูกสร้าง, ปีเริ่ม LST, ปีจบ LST)
BLOCKS = [(2005, 2003, 2007), (2010, 2008, 2012),
          (2015, 2013, 2017), (2020, 2018, 2022)]
BLOCK_LABELS = {2005: '2003–07', 2010: '2008–12', 2015: '2013–17', 2020: '2018–22'}
CHANGE_LABELS = ['ไม่มีสิ่งปลูกสร้าง (คงเดิม)', 'เพิ่มเล็กน้อย (2–10 จุด)',
                 'เพิ่มปานกลาง (10–25 จุด)', 'เพิ่มมาก (มากกว่า 25 จุด)',
                 'เมืองหนาแน่นอยู่แล้ว (คงเดิม)']

_province_names = None
_viirs_scale_cache = None


# ---------------------------------------------------------------- ฤดู

def season_filter(col, season):
    if season == 'dry':
        return col.filter(ee.Filter.Or(
            ee.Filter.calendarRange(11, 12, 'month'),
            ee.Filter.calendarRange(1, 4, 'month')))
    if season == 'wet':
        return col.filter(ee.Filter.calendarRange(5, 10, 'month'))
    return col


# ---------------------------------------------------------------- อุณหภูมิ (Aqua / VIIRS)

def aqua_day(img):
    """MODIS Aqua รายวัน -> LST (°C) เฉพาะพิกเซลคุณภาพดีที่ถ่ายช่วง 12-14 น."""
    raw = img.select('LST_Day_1km')
    t = img.select('Day_view_time').multiply(0.1)          # ชั่วโมง (เวลาสุริยะท้องถิ่น)
    good = (img.select('QC_Day').bitwiseAnd(3).eq(0)       # คุณภาพดี
            .And(t.gte(HOUR_MIN)).And(t.lte(HOUR_MAX))
            .And(raw.gt(0)))
    lst = raw.multiply(0.02).subtract(273.15).rename('LST')
    return lst.updateMask(good).copyProperties(img, ['system:time_start'])


def viirs_scales():
    """ตรวจว่า VIIRS ใน Earth Engine ถูกคูณ scale มาแล้วหรือยัง (ทำครั้งเดียว)"""
    global _viirs_scale_cache
    if _viirs_scale_cache is None:
        lst_k, time_k = 1.0, 1.0
        try:
            stats = (ee.ImageCollection('NASA/VIIRS/002/VNP21A1D')
                     .filterDate('2023-04-01', '2023-04-15')
                     .select(['LST_1KM', 'View_Time']).max()
                     .reduceRegion(ee.Reducer.max(), THAILAND, 5000,
                                   bestEffort=True, maxPixels=1e9)
                     .getInfo())
            if (stats.get('LST_1KM') or 0) > 1000:
                lst_k = 0.02
            if (stats.get('View_Time') or 0) > 30:
                time_k = 0.1
            print('[VIIRS] ตรวจ scale:', stats, '->', (lst_k, time_k))
        except Exception as e:
            print('[VIIRS] ตรวจ scale ไม่สำเร็จ:', e)
        _viirs_scale_cache = (lst_k, time_k)
    return _viirs_scale_cache


def viirs_day_factory():
    lst_k, time_k = viirs_scales()

    def viirs_day(img):
        raw = img.select('LST_1KM').multiply(lst_k)
        t = img.select('View_Time').multiply(time_k)
        good = (img.select('QC').bitwiseAnd(3).eq(0)
                .And(t.gte(HOUR_MIN)).And(t.lte(HOUR_MAX))
                .And(raw.gt(200)))
        lst = raw.subtract(273.15).rename('LST')
        return lst.updateMask(good).copyProperties(img, ['system:time_start'])

    return viirs_day


def aqua_col(start, end, season='all'):
    col = ee.ImageCollection('MODIS/061/MYD11A1').filterDate(start, end)
    return season_filter(col, season).map(aqua_day)


def viirs_col(start, end, season='all'):
    col = ee.ImageCollection('NASA/VIIRS/002/VNP21A1D').filterDate(start, end)
    return season_filter(col, season).map(viirs_day_factory())


def aqua_proj():
    """กริด 1 กม. (sinusoidal) ของ MODIS"""
    return (ee.ImageCollection('MODIS/061/MYD11A1').first()
            .select('LST_Day_1km').projection())


def viirs_proj():
    return (ee.ImageCollection('NASA/VIIRS/002/VNP21A1D').first()
            .select('LST_1KM').projection())


def lst_mean(year, season):
    """LST เฉลี่ย (°C) ของปีและฤดูที่เลือก -> (ภาพ, กริด, ชื่อดาวเทียม)"""
    s, e = f'{year}-01-01', f'{year + 1}-01-01'
    if year <= AQUA_LAST_YEAR:
        proj, col, source = aqua_proj(), aqua_col(s, e, season), 'Aqua'
    else:
        proj, col, source = viirs_proj(), viirs_col(s, e, season), 'VIIRS'
    mean = ee.Image(ee.Algorithms.If(
        col.size().gt(0), col.mean(),
        ee.Image.constant(0).rename('LST').updateMask(0)))
    return mean.setDefaultProjection(proj), proj, source


def lst_level(lst):
    """แบ่ง LST เป็นระดับ 0-4 ตาม LST_BREAKS"""
    level = None
    for b in LST_BREAKS:
        step = lst.gt(b)
        level = step if level is None else level.add(step)
    return level


# ---------------------------------------------------------------- พื้นผิว (Dynamic World)

def dw_year_of(year):
    return max(DW_FIRST_YEAR, min(year, LAST_YEAR))


def dw_mean(year, region):
    """สัดส่วนพื้นผิวเฉลี่ยทั้งปี (0-1) จากค่าความน่าจะเป็นของ Dynamic World (10 ม.)"""
    m = (ee.ImageCollection('GOOGLE/DYNAMICWORLD/V1')
         .filterBounds(region)
         .filterDate(f'{year}-01-01', f'{year + 1}-01-01')
         .select(['built', 'trees', 'grass', 'shrub_and_scrub', 'bare',
                  'crops', 'water', 'flooded_vegetation'])
         .mean())
    return ee.Image.cat([
        m.select('built').rename('BUILT'),
        m.select('trees').rename('TREES'),
        m.select('grass').add(m.select('shrub_and_scrub')).rename('GRASS'),
        m.select('bare').rename('BARE'),
        m.select('crops').rename('CROPS'),
        m.select('water').add(m.select('flooded_vegetation')).rename('WATER'),
    ])


def dw_grid(year, region, proj):
    """เฉลี่ยพื้นผิวให้เป็นช่อง 1 กม. (ผ่านขั้น 100 ม. เพื่อให้คำนวณเร็ว)"""
    m100 = dw_mean(year, region).reproject(crs='EPSG:4326', scale=100)
    return (m100.reduceResolution(reducer=ee.Reducer.mean(), maxPixels=1024)
            .reproject(proj))


# ---------------------------------------------------------------- สิ่งปลูกสร้างย้อนหลัง / ประชากร (GHSL)

def pop_epoch_of(year):
    """GHSL มีข้อมูลทุก 5 ปี ใช้ปีที่ใกล้ที่สุดในช่วง 2000-2020"""
    e = int(round(year / 5.0)) * 5
    return max(2000, min(2020, e))


def built_pct(epoch):
    """สัดส่วนอาคาร (%) ต่อเซลล์ 100 ม. (เซลล์ละ 10,000 ตร.ม.)"""
    return (ee.Image(f'JRC/GHSL/P2023A/GHS_BUILT_S/{epoch}')
            .select('built_surface').divide(100).rename('BUILT'))


def pop_density(epoch):
    """ความหนาแน่นประชากร (คน/ตร.กม.) - เซลล์ 100 ม. = 1 เฮกตาร์ จึงคูณ 100"""
    return (ee.Image(f'JRC/GHSL/P2023A/GHS_POP/{epoch}')
            .select('population_count').multiply(100).rename('DENS'))


def to_grid(img, proj):
    """รวมภาพ 100 ม. ให้เป็นช่อง 1 กม. บนกริดเดียวกับ LST"""
    return (img.reduceResolution(reducer=ee.Reducer.mean(), maxPixels=1024)
            .reproject(proj))


# ---------------------------------------------------------------- ชั้นข้อมูลบนกริด 1 กม.

def grid_layers(year, season, region):
    lst, proj, source = lst_mean(year, season)
    dwy = dw_year_of(year)
    epoch = pop_epoch_of(year)
    cover = dw_grid(dwy, region, proj)
    dens = to_grid(pop_density(epoch), proj)
    green = cover.select('TREES').add(cover.select('GRASS')).rename('GREEN')
    open_ = cover.select('GRASS').add(cover.select('BARE')).rename('OPEN')
    return {'lst': lst, 'proj': proj, 'source': source, 'dw_year': dwy,
            'pop_epoch': epoch, 'cover': cover, 'dens': dens,
            'green': green, 'open': open_}


def priority(L):
    """0 = ไม่เข้าเกณฑ์, 1 = เฝ้าระวัง, 2 = เร่งด่วน (ไม่มีที่โล่ง), 3 = เร่งด่วน (มีที่โล่ง)"""
    lst, dens = L['lst'], L['dens']
    hot = lst.gte(HOT).And(dens.gte(POP_ANY))
    urgent = (lst.gte(VERY_HOT).And(dens.gte(POP_DENSE))
              .And(L['green'].lt(GREEN_LOW)))
    return (hot.toInt()
            .where(urgent, 2)
            .where(urgent.And(L['open'].gte(OPEN_MIN)), 3)
            .rename('PRIO'))


def bin_index(img, edges):
    idx = None
    for e in edges:
        step = img.gt(e)
        idx = step if idx is None else idx.add(step)
    return idx.toInt()


def rr(img, reducer, region, proj):
    return img.reduceRegion(reducer=reducer, geometry=region, crs=proj,
                            scale=1000, bestEffort=True, maxPixels=1e9)


# ---------------------------------------------------------------- พื้นที่โฟกัส

def provinces_fc():
    return (ee.FeatureCollection('FAO/GAUL/2015/level1')
            .filter(ee.Filter.eq('ADM0_NAME', 'Thailand')))


def get_province_names():
    global _province_names
    if _province_names is None:
        _province_names = (provinces_fc().aggregate_array('ADM1_NAME')
                           .sort().getInfo())
    return _province_names


def build_region(data):
    """คืนพื้นที่ (ee.Geometry) ตามโหมด: จังหวัด / รูปที่วาด / จุด"""
    if data.get('province'):
        if data['province'] not in get_province_names():
            raise ValueError('ไม่พบจังหวัดนี้')
        return (provinces_fc()
                .filter(ee.Filter.eq('ADM1_NAME', data['province']))
                .geometry())
    g = data.get('geometry')
    if g:
        if g.get('type') not in ('Polygon', 'MultiPolygon'):
            raise ValueError('รูปทรงไม่ถูกต้อง')
        return ee.Geometry(g)
    lat = float(data['lat'])
    lon = float(data['lon'])
    return ee.Geometry.Point([lon, lat]).buffer(POINT_RADIUS_M)


def read_year(d):
    year = int(d.get('year', DEFAULT_YEAR))
    if not FIRST_YEAR <= year <= LAST_YEAR:
        raise ValueError('ปีไม่ถูกต้อง')
    return year


def read_season(d):
    season = d.get('season', 'all')
    if season not in SEASONS:
        raise ValueError('ฤดูไม่ถูกต้อง')
    return season


def error_response(e):
    if isinstance(e, (ValueError, KeyError, TypeError)):
        return jsonify({'error': 'ข้อมูลที่ส่งมาไม่ถูกต้อง: ' + str(e)}), 400
    print('[ERROR]', repr(e))
    return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------- หน้าเว็บ / API

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/config')
def config():
    return jsonify({
        'version': 3,
        'first_year': FIRST_YEAR, 'last_year': LAST_YEAR,
        'default_year': DEFAULT_YEAR, 'aqua_last_year': AQUA_LAST_YEAR,
        'dw_first_year': DW_FIRST_YEAR,
        'lst_breaks': LST_BREAKS, 'lst_palette': LST_PALETTE,
        'hot': HOT, 'very_hot': VERY_HOT,
        'hot_level': LST_BREAKS.index(VERY_HOT) + 1,
        'pop_any': POP_ANY, 'pop_dense': POP_DENSE,
        'green_low': GREEN_LOW, 'open_min': OPEN_MIN,
        'prio_colors': PRIO_COLORS, 'seasons': SEASONS,
        'point_radius_m': POINT_RADIUS_M,
        'built_labels': BUILT_LABELS, 'green_labels': GREEN_LABELS,
        'change_labels': CHANGE_LABELS,
    })


@app.route('/api/coverage')
def coverage():
    try:
        latest = (ee.ImageCollection('NASA/VIIRS/002/VNP21A1D')
                  .aggregate_max('system:time_start'))
        viirs_last = ee.Date(latest).format('YYYY-MM-dd').getInfo()
        lst_k, time_k = viirs_scales()
        return jsonify({'viirs_last': viirs_last,
                        'viirs_scale_lst': lst_k, 'viirs_scale_time': time_k})
    except Exception as e:
        return error_response(e)


@app.route('/api/tiles')
def tiles():
    """URL ของชั้นแผนที่ทีละชั้น (โหลดเฉพาะชั้นที่เปิดดู)"""
    try:
        layer = request.args.get('layer', 'priority')
        year = read_year(request.args)
        season = read_season(request.args)
        out = {'layer': layer, 'year': year, 'season': season}

        if layer == 'lst':
            lst, _, out['source'] = lst_mean(year, season)
            img = lst_level(lst)
            vis = {'min': 0, 'max': len(LST_BREAKS), 'palette': LST_PALETTE}

        elif layer == 'priority':
            L = grid_layers(year, season, THAILAND)
            out.update(source=L['source'], dw_year=L['dw_year'], pop_epoch=L['pop_epoch'])
            img = priority(L).selfMask()
            vis = {'min': 1, 'max': 3, 'palette': PRIO_COLORS}

        elif layer in ('green', 'open', 'built'):
            dwy = dw_year_of(year)
            out['dw_year'] = dwy
            c = dw_mean(dwy, THAILAND)
            if layer == 'green':
                g = c.select('TREES').add(c.select('GRASS'))
                img = g.updateMask(g.gte(0.2))
                vis = {'min': 0.2, 'max': 0.9,
                       'palette': ['#d9f0a3', '#78c679', '#238443', '#004529']}
            elif layer == 'open':
                o = c.select('GRASS').add(c.select('BARE'))
                lived = to_grid(pop_density(pop_epoch_of(year)), aqua_proj()).gte(POP_ANY)
                img = o.updateMask(o.gte(0.5).And(lived))
                vis = {'min': 0.5, 'max': 1, 'palette': ['#f3d27a', '#b5651d']}
            else:
                b = c.select('BUILT')
                img = b.updateMask(b.gte(0.2))
                vis = {'min': 0.2, 'max': 0.9,
                       'palette': ['#dadaeb', '#9e9ac8', '#6a51a3', '#3f007d']}

        elif layer == 'built_change':
            d = built_pct(2020).subtract(built_pct(2005))
            img = d.updateMask(d.gt(2))
            vis = {'min': 2, 'max': 40,
                   'palette': ['#fde0ef', '#f1b6da', '#de77ae', '#c51b7d', '#8e0152']}
        else:
            raise ValueError('ไม่รู้จักชั้นข้อมูลนี้')

        out['url'] = img.getMapId(vis)['tile_fetcher'].url_format
        return jsonify(out)
    except Exception as e:
        return error_response(e)


@app.route('/api/provinces')
def provinces():
    try:
        return jsonify(get_province_names())
    except Exception as e:
        return error_response(e)


@app.route('/api/province_shape')
def province_shape():
    try:
        name = request.args.get('name', '')
        if name not in get_province_names():
            return jsonify({'error': 'ไม่พบจังหวัดนี้'}), 404
        geom = (provinces_fc()
                .filter(ee.Filter.eq('ADM1_NAME', name))
                .geometry().simplify(500))
        return jsonify(geom.getInfo())
    except Exception as e:
        return error_response(e)


@app.route('/api/summary', methods=['POST'])
def summary():
    """สรุปพื้นที่: พื้นผิว, ระดับความเร่งด่วน, ที่โล่ง, คนกับความร้อน"""
    try:
        data = request.get_json(force=True)
        year, season = read_year(data), read_season(data)
        region = build_region(data)
        L = grid_layers(year, season, region)
        proj = L['proj']

        km2 = ee.Image.pixelArea().divide(1e6)
        people = L['dens'].multiply(km2).rename('PEOPLE')
        open_km2 = L['open'].multiply(km2).rename('OPENKM2')
        cls = priority(L)

        prio_reducer = (ee.Reducer.sum()
                        .combine(reducer2=ee.Reducer.sum(), outputPrefix='open_')
                        .combine(reducer2=ee.Reducer.sum(), outputPrefix='area_')
                        .group(groupField=3, groupName='g'))
        prio_stack = (people.addBands(open_km2)
                      .addBands(km2.rename('AREA')).addBands(cls))
        expo_stack = people.addBands(lst_level(L['lst']).toInt().rename('g'))
        lived = L['dens'].gte(POP_ANY)

        res = ee.Dictionary({
            'prio': rr(prio_stack, prio_reducer, region, proj).get('groups'),
            'expo': rr(expo_stack, ee.Reducer.sum().group(groupField=1, groupName='g'),
                       region, proj).get('groups'),
            'cover_all': rr(L['cover'], ee.Reducer.mean(), region, proj),
            'cover_lived': rr(L['cover'].updateMask(lived), ee.Reducer.mean(),
                              region, proj),
            'pop_total': rr(people, ee.Reducer.sum(), region, proj).get('PEOPLE'),
            'lst_lived': rr(L['lst'].updateMask(lived), ee.Reducer.mean(),
                            region, proj).get('LST'),
            'area_km2': region.area(maxError=1).divide(1e6),
        }).getInfo()

        found = {int(r['g']): r for r in (res.get('prio') or [])}
        prio = [{'cls': c,
                 'people': found.get(c, {}).get('sum') or 0,
                 'open_km2': found.get(c, {}).get('open_sum') or 0,
                 'area_km2': found.get(c, {}).get('area_sum') or 0}
                for c in range(4)]
        efound = {int(r['g']): r.get('sum') or 0 for r in (res.get('expo') or [])}
        expo = [{'level': i, 'people': efound.get(i, 0)}
                for i in range(len(LST_BREAKS) + 1)]

        return jsonify({
            'year': year, 'season': season, 'source': L['source'],
            'dw_year': L['dw_year'], 'pop_epoch': L['pop_epoch'],
            'area_km2': res.get('area_km2'),
            'cover_all': res.get('cover_all') or {},
            'cover_lived': res.get('cover_lived') or {},
            'pop_total': res.get('pop_total') or 0,
            'lst_lived': res.get('lst_lived'),
            'prio': prio, 'expo': expo,
        })
    except Exception as e:
        return error_response(e)


@app.route('/api/bins', methods=['POST'])
def bins():
    """อุณหภูมิในย่านที่มีคนอยู่ แยกตามสัดส่วนพื้นที่สีเขียว และสัดส่วนสิ่งปลูกสร้าง"""
    try:
        data = request.get_json(force=True)
        year, season = read_year(data), read_season(data)
        region = build_region(data)
        L = grid_layers(year, season, region)
        proj = L['proj']

        lst = L['lst'].updateMask(L['dens'].gte(POP_ANY))
        reducer = (ee.Reducer.mean()
                   .combine(reducer2=ee.Reducer.count(), sharedInputs=True)
                   .group(groupField=1, groupName='g'))
        built_idx = bin_index(L['cover'].select('BUILT'), BUILT_EDGES).rename('g')
        green_idx = bin_index(L['green'], GREEN_EDGES).rename('g')

        res = ee.Dictionary({
            'built': rr(lst.addBands(built_idx), reducer, region, proj).get('groups'),
            'green': rr(lst.addBands(green_idx), reducer, region, proj).get('groups'),
        }).getInfo()

        out = {'year': year, 'season': season, 'source': L['source'],
               'dw_year': L['dw_year']}
        for key, labels in (('built', BUILT_LABELS), ('green', GREEN_LABELS)):
            found = {int(r['g']): r for r in (res.get(key) or [])}
            out[key] = [{'label': lab,
                         'mean': found.get(i, {}).get('mean'),
                         'count': int(found.get(i, {}).get('count') or 0)}
                        for i, lab in enumerate(labels)]
        return jsonify(out)
    except Exception as e:
        return error_response(e)


@app.route('/api/trend', methods=['POST'])
def trend():
    """ย้อนหลัง 20 ปี: สัดส่วนสิ่งปลูกสร้าง vs อุณหภูมิ ทุก 5 ปี (เฉพาะย่านที่มีคนอยู่)"""
    try:
        data = request.get_json(force=True)
        season = read_season(data)
        region = build_region(data)
        proj = aqua_proj()
        lived = to_grid(pop_density(2020), proj).gte(POP_ANY)

        out = {}
        for epoch, y0, y1 in BLOCKS:
            lst = (aqua_col(f'{y0}-01-01', f'{y1 + 1}-01-01', season)
                   .mean().setDefaultProjection(proj))
            built = to_grid(built_pct(epoch), proj)
            img = lst.addBands(built).updateMask(lst.mask().And(lived))
            out[str(epoch)] = rr(img, ee.Reducer.mean(), region, proj)
        res = ee.Dictionary(out).getInfo()

        rows = [{'epoch': e, 'label': BLOCK_LABELS[e],
                 'lst': (res.get(str(e)) or {}).get('LST'),
                 'built': (res.get(str(e)) or {}).get('BUILT')}
                for e, _, _ in BLOCKS]
        return jsonify({'season': season, 'rows': rows})
    except Exception as e:
        return error_response(e)


@app.route('/api/change', methods=['POST'])
def change():
    """อุณหภูมิที่เปลี่ยนไป (2018-22 เทียบ 2003-07) แยกตามระดับที่สิ่งปลูกสร้างเพิ่ม (2005 -> 2020)"""
    try:
        data = request.get_json(force=True)
        season = read_season(data)
        region = build_region(data)
        proj = aqua_proj()

        lst_a = aqua_col('2003-01-01', '2008-01-01', season).mean().setDefaultProjection(proj)
        lst_b = aqua_col('2018-01-01', '2023-01-01', season).mean().setDefaultProjection(proj)
        d_lst = lst_b.subtract(lst_a).rename('DLST')

        b05 = to_grid(built_pct(2005), proj)
        b20 = to_grid(built_pct(2020), proj)
        d_b = b20.subtract(b05)

        cls = ee.Image(-1)
        cls = cls.where(d_b.lte(2).And(b05.lt(10)), 0)     # กลุ่มเทียบ
        cls = cls.where(d_b.gt(2).And(d_b.lte(10)), 1)
        cls = cls.where(d_b.gt(10).And(d_b.lte(25)), 2)
        cls = cls.where(d_b.gt(25), 3)
        cls = cls.where(d_b.lte(2).And(b05.gte(60)), 4)    # เมืองหนาแน่นอยู่แล้ว
        cls = cls.updateMask(cls.gte(0)).rename('g').toInt()

        reducer = (ee.Reducer.mean()
                   .combine(reducer2=ee.Reducer.count(), sharedInputs=True)
                   .group(groupField=1, groupName='g'))
        groups = ee.List(rr(d_lst.addBands(cls), reducer, region, proj)
                         .get('groups')).getInfo() or []
        found = {int(r['g']): r for r in groups}

        rows = [{'label': lab,
                 'mean': found.get(i, {}).get('mean'),
                 'count': int(found.get(i, {}).get('count') or 0)}
                for i, lab in enumerate(CHANGE_LABELS)]
        return jsonify({'season': season, 'rows': rows})
    except Exception as e:
        return error_response(e)


if __name__ == '__main__':
    app.run(debug=True)
