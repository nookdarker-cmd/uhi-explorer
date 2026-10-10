"""UHI Explorer - backend (Flask + Google Earth Engine)

เป้าหมาย: หาย่านที่ควรปรับปรุงเพื่อลดความร้อนให้ผู้อยู่อาศัย
  เร่งด่วน = ร้อนมาก + คนอยู่หนาแน่น + พื้นที่สีเขียวน้อย
  แล้วดูต่อว่ามี "ที่โล่ง" ที่อาจทำเป็นพื้นที่สีเขียวได้หรือไม่

ข้อมูล (วิเคราะห์บนกริด 30 ฟิลิปดา ≈ 1 กม.)
  - อุณหภูมิพื้นผิวช่วงบ่าย 12-14 น. : MODIS Aqua (2003-2022), VIIRS (2023 ขึ้นไป
    ปรับเทียบให้อยู่ระดับเดียวกับ Aqua รายพิกเซล จากช่วงที่วัดซ้อนกัน 2013-2020)
  - พื้นผิว (สิ่งปลูกสร้างรวมถนน, ต้นไม้, หญ้า, ดินเปล่า) : Dynamic World 10 ม. (2016 ขึ้นไป)
  - ที่โล่ง (หญ้า พุ่มไม้ ดินเปล่า) : ESA WorldCover 2021 10 ม.
  - สิ่งปลูกสร้างย้อนหลัง 20 ปี และประชากร : GHSL (ทุก 5 ปี)

ความเร็ว: เซิร์ฟเวอร์สั่ง Earth Engine คำนวณชั้นข้อมูลรายปีล่วงหน้าเก็บเป็น asset
ใน projects/<project>/assets/uhi_v1 ระหว่างที่ยังไม่เสร็จจะคำนวณสดแทน (ช้ากว่า)
"""
import json
import os
import tempfile
import threading
import time
from datetime import datetime

import ee
from flask import Flask, jsonify, render_template, request

PROJECT_ID = os.environ.get('EE_PROJECT', 'hicm-510809')
ASSET_FOLDER = f'projects/{PROJECT_ID}/assets/uhi_v1'
TASK_PREFIX = 'uhi_v1_'
# สั่งคำนวณล่วงหน้าอัตโนมัติบนเว็บจริง (Render ตั้งค่า RENDER=true ให้เอง)
PRECOMPUTE = os.environ.get('UHI_PRECOMPUTE', '1' if os.environ.get('RENDER') else '0') == '1'


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
GRID_CRS = 'EPSG:4326'
GRID_TRANSFORM = [1 / 120, 0, 97.3, 0, -1 / 120, 20.5]   # 30 ฟิลิปดา ≈ 0.9 กม.
GRID = ee.Projection(GRID_CRS, GRID_TRANSFORM)

HOUR_MIN, HOUR_MAX = 12, 14      # ช่วงเวลาสังเกต (เวลาท้องถิ่นสุริยะ)
FIRST_YEAR = 2003                # ปีแรกที่ Aqua มีข้อมูลเต็มปี
AQUA_LAST_YEAR = 2022            # หลังจากนี้ Aqua เลื่อนเวลาผ่านจนเกิน 14 น.
DW_FIRST_YEAR = 2016             # ปีแรกที่ Dynamic World มีข้อมูลเต็มปี
BIAS_YEARS = (2013, 2020)        # ช่วงที่ใช้ปรับเทียบ VIIRS กับ Aqua
CHECK_YEARS = (2021, 2022)       # ช่วงที่ใช้ตรวจสอบการปรับเทียบ (ไม่ได้ใช้ตอนปรับเทียบ)
LAST_YEAR = datetime.now().year - 1
DEFAULT_YEAR = min(2022, LAST_YEAR)
POP_EPOCHS = (2005, 2010, 2015, 2020)
MAX_AREA_KM2 = 30000             # พื้นที่ที่วาดได้มากสุด

# ------------------------------------------------------------ เกณฑ์ (ปรับได้ที่นี่)
LST_BREAKS = [30, 34, 38, 42]    # ระดับสีอุณหภูมิ (°C)
LST_PALETTE = ['#2b83ba', '#abdda4', '#ffffbf', '#fdae61', '#d7191c']
HOT = 34                         # ร้อน (°C) -> เฝ้าระวัง
VERY_HOT = 38                    # ร้อนมาก (°C) -> เร่งด่วน
POP_ANY = 300                    # คน/ตร.กม. ขึ้นไป = มีคนอยู่ (นิยาม urban cluster ของ GHSL)
POP_DENSE = 1500                 # คน/ตร.กม. ขึ้นไป = หนาแน่น (นิยาม urban centre ของ GHSL)
GREEN_LOW = 0.15                 # พื้นที่สีเขียวน้อยกว่านี้ = เขียวน้อย
OPEN_MIN = 0.05                  # ที่โล่งตั้งแต่นี้ (สัดส่วนของช่อง ≈ 5 ไร่ต่อ 1 ตร.กม.) = มีที่โล่ง
OPEN_CLASSES = (20, 30, 60)      # WorldCover: พุ่มไม้, ทุ่งหญ้า, ดินเปล่า
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
# กลุ่มตามพื้นที่อาคารที่เพิ่มขึ้น 2005 -> 2020 (จุดเปอร์เซ็นต์ของพื้นที่ช่อง)
CHANGE_LABELS = ['ไม่มีอาคารและไม่เปลี่ยน (กลุ่มเทียบ)', 'อาคารเพิ่มเล็กน้อย (1–4 จุด)',
                 'อาคารเพิ่มปานกลาง (4–8 จุด)', 'อาคารเพิ่มมาก (เกิน 8 จุด)',
                 'เมืองหนาแน่นอยู่แล้ว (คงเดิม)']

PROVINCE_TH = {
    'Amnat Charoen': 'อำนาจเจริญ', 'Ang Thong': 'อ่างทอง', 'Bangkok': 'กรุงเทพมหานคร',
    'Buriram': 'บุรีรัมย์', 'Chachoengsao': 'ฉะเชิงเทรา', 'Chainat': 'ชัยนาท',
    'Chaiyaphum': 'ชัยภูมิ', 'Chanthaburi': 'จันทบุรี', 'Chiang Mai': 'เชียงใหม่',
    'Chiang Rai': 'เชียงราย', 'Chonburi': 'ชลบุรี', 'Chumphon': 'ชุมพร',
    'Kalasin': 'กาฬสินธุ์', 'Kampaeng Phet': 'กำแพงเพชร', 'Kanchanaburi': 'กาญจนบุรี',
    'Khon Kaen': 'ขอนแก่น', 'Krabi': 'กระบี่', 'Lampang': 'ลำปาง', 'Lamphun': 'ลำพูน',
    'Loei': 'เลย', 'Lopburi': 'ลพบุรี', 'Mae Hong Son': 'แม่ฮ่องสอน',
    'Maha Sarakham': 'มหาสารคาม', 'Mukdahan': 'มุกดาหาร', 'Nakhon Nayok': 'นครนายก',
    'Nakhon Pathom': 'นครปฐม', 'Nakhon Phanom': 'นครพนม', 'Nakhon Ratchasima': 'นครราชสีมา',
    'Nakhon Sawan': 'นครสวรรค์', 'Nakhon Si Thammarat': 'นครศรีธรรมราช', 'Nan': 'น่าน',
    'Narathiwat': 'นราธิวาส', 'Nong Bua Lamphu': 'หนองบัวลำภู', 'Nong Khai': 'หนองคาย',
    'Nonthaburi': 'นนทบุรี', 'Pathum Thani': 'ปทุมธานี', 'Pattani': 'ปัตตานี',
    'Phachinburi': 'ปราจีนบุรี', 'Phangnga': 'พังงา', 'Phatthalung': 'พัทลุง',
    'Phayao': 'พะเยา', 'Phetchabun': 'เพชรบูรณ์', 'Phetchaburi': 'เพชรบุรี',
    'Phichit': 'พิจิตร', 'Phitsanulok': 'พิษณุโลก', 'Phra Nakhon Si Ayudhya': 'พระนครศรีอยุธยา',
    'Phrae': 'แพร่', 'Phuket': 'ภูเก็ต', 'Prachuap Khilikhan': 'ประจวบคีรีขันธ์',
    'Ranong': 'ระนอง', 'Ratchaburi': 'ราชบุรี', 'Rayong': 'ระยอง', 'Roi Et': 'ร้อยเอ็ด',
    'Sa Kaeo': 'สระแก้ว', 'Sakon Nakhon': 'สกลนคร', 'Samut Prakarn': 'สมุทรปราการ',
    'Samut Sakhon': 'สมุทรสาคร', 'Samut Songkham': 'สมุทรสงคราม', 'Saraburi': 'สระบุรี',
    'Satun': 'สตูล', 'Si Saket': 'ศรีสะเกษ', 'Singburi': 'สิงห์บุรี', 'Songkhla': 'สงขลา',
    'Sukhothai': 'สุโขทัย', 'Suphanburi': 'สุพรรณบุรี', 'Surat Thani': 'สุราษฎร์ธานี',
    'Surin': 'สุรินทร์', 'Tak': 'ตาก', 'Trad': 'ตราด', 'Trang': 'ตรัง',
    'Ubon Ratchathani': 'อุบลราชธานี', 'Udon Thani': 'อุดรธานี', 'Uthai Thani': 'อุทัยธานี',
    'Uttaradit': 'อุตรดิตถ์', 'Yala': 'ยะลา', 'Yasothon': 'ยโสธร',
}

_province_names = None
_viirs_scale_cache = None


# ---------------------------------------------------------------- แคชผลลัพธ์

_cache = {}
_cache_lock = threading.Lock()


def cached(key, fn, ttl=6 * 3600):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _cache_lock:
        if len(_cache) > 400:
            _cache.clear()
        _cache[key] = (now, val)
    return val


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
    """MODIS Aqua รายวัน -> LST (°C) ช่วง 12-14 น. ที่ความคลาดเคลื่อนไม่เกิน 2 K"""
    raw = img.select('LST_Day_1km')
    t = img.select('Day_view_time').multiply(0.1)          # ชั่วโมง (เวลาสุริยะท้องถิ่น)
    qc = img.select('QC_Day')
    # รับทั้ง "คุณภาพดี" และ "คุณภาพอื่น" ถ้าความคลาดเคลื่อนของ LST ไม่เกิน 2 K
    # (ถ้ารับเฉพาะคุณภาพดี ย่านเมืองหนาแน่นจะหายไปมาก เพราะวัสดุหลากหลาย)
    good = (qc.bitwiseAnd(3).lte(1)                        # bits 0-1: ผลิตค่าได้
            .And(qc.rightShift(6).bitwiseAnd(3).lte(1))    # bits 6-7: error <= 2 K
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
            print('[VIIRS] ตรวจ scale:', stats, '->', (lst_k, time_k), flush=True)
        except Exception as e:
            print('[VIIRS] ตรวจ scale ไม่สำเร็จ:', e, flush=True)
        _viirs_scale_cache = (lst_k, time_k)
    return _viirs_scale_cache


def viirs_day_factory():
    lst_k, time_k = viirs_scales()

    def viirs_day(img):
        raw = img.select('LST_1KM').multiply(lst_k)
        t = img.select('View_Time').multiply(time_k)
        qc = img.select('QC')
        # เกณฑ์เดียวกับ Aqua: ผลิตค่าได้, ท้องฟ้าโปร่ง, ความแม่นยำ LST ไม่แย่กว่า 2 K
        good = (qc.bitwiseAnd(3).lte(1)                        # bits 0-1: ผลิตค่าได้
                .And(qc.rightShift(4).bitwiseAnd(3).eq(0))     # bits 4-5: ไม่มีเมฆ
                .And(qc.rightShift(14).bitwiseAnd(3).gte(1))   # bits 14-15: error <= 2 K
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


def source_of(year):
    return 'Aqua' if year <= AQUA_LAST_YEAR else 'VIIRS'


def lst_raw_mean(year, season):
    """LST เฉลี่ย (°C) ของปี/ฤดู จากดาวเทียมของปีนั้น (ยังไม่ปรับเทียบ)"""
    s, e = f'{year}-01-01', f'{year + 1}-01-01'
    col = aqua_col(s, e, season) if year <= AQUA_LAST_YEAR else viirs_col(s, e, season)
    empty = ee.Image.constant(0).rename('LST').updateMask(0)
    return ee.Image(ee.Algorithms.If(col.size().gt(0), col.mean(), empty)).rename('LST')


def vbias(season):
    """ส่วนต่าง VIIRS − Aqua รายพิกเซล ในช่วงที่วัดซ้อนกัน"""
    s, e = f'{BIAS_YEARS[0]}-01-01', f'{BIAS_YEARS[1] + 1}-01-01'
    return (viirs_col(s, e, season).mean()
            .subtract(aqua_col(s, e, season).mean()).rename('VB'))


def lst_level(lst):
    """แบ่ง LST เป็นระดับ 0-4 ตาม LST_BREAKS"""
    level = None
    for b in LST_BREAKS:
        step = lst.gt(b)
        level = step if level is None else level.add(step)
    return level


# ---------------------------------------------------------------- พื้นผิว / ประชากร

def dw_year_of(year):
    return max(DW_FIRST_YEAR, min(year, LAST_YEAR))


def pop_epoch_of(year):
    """GHSL มีข้อมูลทุก 5 ปี ใช้ปีที่ใกล้ที่สุด"""
    e = int(round(year / 5.0)) * 5
    return max(POP_EPOCHS[0], min(POP_EPOCHS[-1], e))


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


def dw_agg(year, region):
    """พื้นผิวเฉลี่ยที่พร้อมรวมเป็นช่อง 1 กม. (ขั้นกลาง 300 ม. อ่านจาก pyramid แบบเฉลี่ย)"""
    return (dw_mean(year, region).reproject(crs='EPSG:4326', scale=300)
            .reduceResolution(reducer=ee.Reducer.mean(), maxPixels=1024))


def worldcover_open():
    """ที่โล่ง 10 ม. จาก ESA WorldCover 2021: พุ่มไม้ ทุ่งหญ้า ดินเปล่า"""
    wc = ee.ImageCollection('ESA/WorldCover/v200').first().select('Map')
    out = None
    for c in OPEN_CLASSES:
        out = wc.eq(c) if out is None else out.Or(wc.eq(c))
    return out.rename('OPEN')


def open_agg(scale=30):
    """สัดส่วนที่โล่งต่อช่อง: asset ใช้ 30 ม. (ละเอียด), การคำนวณสดใช้ 100 ม. (เร็วกว่า ~10 เท่า)"""
    return (worldcover_open().reproject(crs='EPSG:4326', scale=scale)
            .reduceResolution(reducer=ee.Reducer.mean(), maxPixels=1024).rename('OPEN'))


def built_pct(epoch):
    """สัดส่วนอาคาร (%) ต่อเซลล์ 100 ม. (เซลล์ละ 10,000 ตร.ม.)"""
    return (ee.Image(f'JRC/GHSL/P2023A/GHS_BUILT_S/{epoch}')
            .select('built_surface').divide(100).rename('BUILT'))


def pop_density(epoch):
    """ความหนาแน่นประชากร (คน/ตร.กม.) - เซลล์ 100 ม. = 1 เฮกตาร์ จึงคูณ 100"""
    return (ee.Image(f'JRC/GHSL/P2023A/GHS_POP/{epoch}')
            .select('population_count').multiply(100).rename('DENS'))


def mean_agg(img):
    return img.reduceResolution(reducer=ee.Reducer.mean(), maxPixels=1024)


# ---------------------------------------------------------------- คำนวณล่วงหน้า (assets)

def needed_assets():
    """เรียงตามความสำคัญ: ปีที่เปิดเป็นค่าเริ่มต้นก่อน แล้วข้อมูลย้อนหลัง แล้วปีอื่น ๆ"""
    d = DEFAULT_YEAR
    names = ['static', f'lst_{d}', f'dw_{d}']
    names += [f'lst_{y}' for y in range(FIRST_YEAR, AQUA_LAST_YEAR + 1) if y != d]
    names += ['vbias'] + [f'lst_{y}' for y in range(AQUA_LAST_YEAR + 1, LAST_YEAR + 1) if y != d]
    names += [f'dw_{y}' for y in range(LAST_YEAR, DW_FIRST_YEAR - 1, -1) if y != d]
    names += ['vcheck']
    return names


def export_region():
    return (ee.FeatureCollection('FAO/GAUL/2015/level0')
            .filter(ee.Filter.eq('ADM0_NAME', 'Thailand'))
            .geometry().simplify(2000).buffer(5000))


def build_asset(name):
    if name == 'static':
        bands = [open_agg()]
        for e in POP_EPOCHS:
            bands.append(mean_agg(built_pct(e)).rename(f'GB{e}'))
            bands.append(mean_agg(pop_density(e)).rename(f'DENS{e}'))
        return ee.Image.cat(bands).toFloat()
    if name == 'vbias':
        return ee.Image.cat([vbias(s).rename(f'VB_{s}') for s in SEASONS]).toFloat()
    if name == 'vcheck':
        # ตรวจสอบการปรับเทียบด้วยปีที่ไม่ได้ใช้ปรับเทียบ: (VIIRS − ส่วนต่าง) − Aqua
        s, e = f'{CHECK_YEARS[0]}-01-01', f'{CHECK_YEARS[1] + 1}-01-01'
        bands = []
        for se in SEASONS:
            v = viirs_col(s, e, se).mean().subtract(vbias(se))
            bands.append(v.subtract(aqua_col(s, e, se).mean()).rename(f'D_{se}'))
        return ee.Image.cat(bands).toFloat()
    kind, year = name.split('_')
    year = int(year)
    if kind == 'lst':
        return ee.Image.cat([lst_raw_mean(year, s).rename(f'LST_{s}') for s in SEASONS]).toFloat()
    if kind == 'dw':
        return dw_agg(year, export_region()).toFloat()
    raise ValueError(name)


_assets = {'t': 0, 'ids': set()}
_pre = {'last_run': 0, 'started': [], 'error': None, 'running': False}


def existing_assets(force=False):
    if force or time.time() - _assets['t'] > 120:
        try:
            res = ee.data.listAssets({'parent': ASSET_FOLDER})
            ids = set()
            for a in res.get('assets', []):
                aid = a.get('id') or a.get('name') or ''
                ids.add(aid.rsplit('/', 1)[-1])
            _assets['ids'] = ids
        except Exception:
            _assets['ids'] = set()
        _assets['t'] = time.time()
    return _assets['ids']


def assets_ready(names):
    have = existing_assets()
    return all(n in have for n in names)


def ee_tasks():
    out = []
    try:
        for op in ee.data.listOperations():
            md = op.get('metadata', {})
            out.append({'desc': md.get('description', ''), 'state': md.get('state', ''),
                        'error': (op.get('error') or {}).get('message'),
                        'name': op.get('name'), 'create': md.get('createTime', '')})
    except Exception:
        try:
            for t in ee.data.getTaskList():
                out.append({'desc': t.get('description', ''), 'state': t.get('state', ''),
                            'error': t.get('error_message'), 'name': t.get('id'),
                            'create': str(t.get('creation_timestamp_ms', ''))})
        except Exception as e:
            out.append({'desc': '', 'state': 'LIST_ERROR', 'error': str(e)})
    return [t for t in out if t['desc'].startswith(TASK_PREFIX)]


def cancel_task(t):
    try:
        ee.data.cancelOperation(t['name'])
    except Exception:
        try:
            ee.data.cancelTask(str(t['name']).rsplit('/', 1)[-1])
        except Exception as e:
            print('[precompute] cancel failed', t['desc'], e, flush=True)


def ensure_folder():
    try:
        ee.data.getAsset(ASSET_FOLDER)
    except Exception:
        try:
            ee.data.createFolder(ASSET_FOLDER)
        except AttributeError:
            ee.data.createAsset({'type': 'FOLDER'}, ASSET_FOLDER)


def ensure_precompute():
    """สั่ง export ชั้นข้อมูลที่ยังไม่มี (ไม่สั่งซ้ำถ้ากำลังทำอยู่)"""
    if _pre['running']:
        return
    _pre['running'] = True
    _pre['last_run'] = time.time()
    try:
        ensure_folder()
        have = existing_assets(force=True)
        tasks = ee_tasks()
        rank = {TASK_PREFIX + n: i for i, n in enumerate(needed_assets())}
        pending = [t for t in tasks if t['state'] in ('PENDING', 'READY')]
        running = {t['desc'] for t in tasks if t['state'] == 'RUNNING'}
        # ถ้างานที่รอคิวเรียงไม่ตรงลำดับความสำคัญ ยกเลิกแล้วสั่งใหม่ตามลำดับ (งานที่วิ่งอยู่ไม่แตะ)
        order = [rank.get(t['desc'], 999) for t in sorted(pending, key=lambda t: t['create'] or '')]
        if order != sorted(order):
            for t in pending:
                cancel_task(t)
            print('[precompute] reordered', len(pending), 'pending tasks', flush=True)
            pending = []
        active = running | {t['desc'] for t in pending}
        region = export_region()
        for name in needed_assets():
            desc = TASK_PREFIX + name
            if name in have or desc in active:
                continue
            task = ee.batch.Export.image.toAsset(
                image=build_asset(name), description=desc,
                assetId=f'{ASSET_FOLDER}/{name}', region=region,
                crs=GRID_CRS, crsTransform=GRID_TRANSFORM, maxPixels=1e10,
                pyramidingPolicy={'.default': 'mean'})
            task.start()
            _pre['started'].append(name)
            print('[precompute] started', name, flush=True)
        _pre['error'] = None
    except Exception as e:
        _pre['error'] = repr(e)
        print('[precompute] error', repr(e), flush=True)
    finally:
        _pre['running'] = False


def maybe_precompute():
    if PRECOMPUTE and time.time() - _pre['last_run'] > 1800:
        threading.Thread(target=ensure_precompute, daemon=True).start()


def A(name):
    return ee.Image(f'{ASSET_FOLDER}/{name}')


# ---------------------------------------------------------------- ชั้นข้อมูลบนกริด

def layers_fast(year):
    """ข้อมูลของปีนี้คำนวณล่วงหน้าครบแล้วหรือยัง"""
    need = ['static', f'lst_{year}', f'dw_{dw_year_of(year)}']
    if source_of(year) == 'VIIRS':
        need.append('vbias')
    return assets_ready(need)


def layers(year, season, region):
    dwy, epoch, source = dw_year_of(year), pop_epoch_of(year), source_of(year)
    fast = layers_fast(year)
    if fast:
        st = A('static')
        lst = A(f'lst_{year}').select(f'LST_{season}').rename('LST')
        if source == 'VIIRS':
            lst = lst.subtract(A('vbias').select(f'VB_{season}')).rename('LST')
        cover = A(f'dw_{dwy}')
        dens = st.select(f'DENS{epoch}').rename('DENS')
        open_ = st.select('OPEN')
    else:
        lst = lst_raw_mean(year, season)
        if source == 'VIIRS':
            lst = lst.subtract(vbias(season)).rename('LST')
        cover = dw_agg(dwy, region).reproject(GRID)
        dens = mean_agg(pop_density(epoch)).reproject(GRID)
        open_ = open_agg(100).reproject(GRID)
    green = cover.select('TREES').add(cover.select('GRASS')).rename('GREEN')
    return {'lst': lst, 'source': source, 'dw_year': dwy, 'pop_epoch': epoch,
            'cover': cover, 'dens': dens, 'green': green, 'open': open_, 'fast': fast}


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


def rr(img, reducer, region):
    return img.reduceRegion(reducer=reducer, geometry=region, crs=GRID_CRS,
                            crsTransform=GRID_TRANSFORM, maxPixels=1e9, tileScale=4)


def grouped_rows(groups, labels):
    found = {int(r['g']): r for r in (groups or [])}
    return [{'label': lab,
             'mean': found.get(i, {}).get('mean'),
             'count': int(found.get(i, {}).get('count') or 0)}
            for i, lab in enumerate(labels)]


MEAN_COUNT = (ee.Reducer.mean()
              .combine(reducer2=ee.Reducer.count(), sharedInputs=True))


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
        geom = ee.Geometry(g)
        area = cached('area|' + json.dumps(g, sort_keys=True),
                      lambda: geom.area(maxError=100).divide(1e6).getInfo())
        if area > MAX_AREA_KM2:
            raise ValueError(f'พื้นที่ที่วาดใหญ่เกินไป ({area:,.0f} ตร.กม.) '
                             f'วาดให้ไม่เกิน {MAX_AREA_KM2:,} ตร.กม. หรือเลือกเป็นจังหวัดแทน')
        return geom
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


def region_key(data):
    return json.dumps({k: data.get(k) for k in ('province', 'geometry', 'lat', 'lon')},
                      sort_keys=True)


def error_response(e):
    if isinstance(e, ValueError) and str(e) and not str(e).isascii():
        return jsonify({'error': str(e)}), 400
    if isinstance(e, (ValueError, KeyError, TypeError)):
        return jsonify({'error': 'ข้อมูลที่ส่งมาไม่ถูกต้อง: ' + str(e)}), 400
    msg = str(e)
    print('[ERROR]', repr(e), flush=True)
    low = msg.lower()
    if 'memory limit' in low or 'timed out' in low or 'too many' in low or 'capacity' in low:
        msg = ('พื้นที่นี้คำนวณหนักเกินไปในตอนนี้ ลองพื้นที่เล็กลง หรือรอสักพัก '
               '(ระบบกำลังเตรียมข้อมูลล่วงหน้า เมื่อเสร็จจะเร็วขึ้นมาก)')
    return jsonify({'error': msg}), 500


# ---------------------------------------------------------------- หน้าเว็บ / API

@app.route('/')
def index():
    maybe_precompute()
    return render_template('index.html')


def precompute_progress():
    need = needed_assets()
    have = existing_assets()
    return {'ready': sum(1 for n in need if n in have), 'total': len(need)}


@app.route('/api/config')
def config():
    maybe_precompute()
    return jsonify({
        'version': 4,
        'first_year': FIRST_YEAR, 'last_year': LAST_YEAR,
        'default_year': DEFAULT_YEAR, 'aqua_last_year': AQUA_LAST_YEAR,
        'dw_first_year': DW_FIRST_YEAR, 'bias_years': BIAS_YEARS,
        'lst_breaks': LST_BREAKS, 'lst_palette': LST_PALETTE,
        'hot': HOT, 'very_hot': VERY_HOT,
        'hot_level': LST_BREAKS.index(VERY_HOT) + 1,
        'pop_any': POP_ANY, 'pop_dense': POP_DENSE,
        'green_low': GREEN_LOW, 'open_min': OPEN_MIN,
        'prio_colors': PRIO_COLORS, 'seasons': SEASONS,
        'point_radius_m': POINT_RADIUS_M, 'max_area_km2': MAX_AREA_KM2,
        'built_labels': BUILT_LABELS, 'green_labels': GREEN_LABELS,
        'change_labels': CHANGE_LABELS,
        'precompute': precompute_progress(),
    })


@app.route('/api/status')
def status():
    """สถานะการคำนวณล่วงหน้า (ไว้ตรวจสอบ)"""
    try:
        have = existing_assets(force=True)
        need = needed_assets()
        return jsonify({
            'folder': ASSET_FOLDER, 'enabled': PRECOMPUTE,
            'ready': sorted(n for n in need if n in have),
            'missing': [n for n in need if n not in have],
            'tasks': ee_tasks()[:80],
            'started_this_run': _pre['started'], 'error': _pre['error'],
        })
    except Exception as e:
        return error_response(e)


@app.route('/api/coverage')
def coverage():
    try:
        def calc():
            latest = (ee.ImageCollection('NASA/VIIRS/002/VNP21A1D')
                      .aggregate_max('system:time_start'))
            return ee.Date(latest).format('YYYY-MM-dd').getInfo()
        return jsonify({'viirs_last': cached('viirs_last', calc, ttl=12 * 3600),
                        'precompute': precompute_progress()})
    except Exception as e:
        return error_response(e)


@app.route('/api/tiles')
def tiles():
    """URL ของชั้นแผนที่ทีละชั้น (โหลดเฉพาะชั้นที่เปิดดู)"""
    try:
        layer = request.args.get('layer', 'priority')
        year = read_year(request.args)
        season = read_season(request.args)
        if layer not in ('lst', 'priority', 'green', 'open', 'built', 'built_change'):
            raise ValueError('ไม่รู้จักชั้นข้อมูลนี้')

        def calc():
            out = {'layer': layer, 'year': year, 'season': season}
            if layer in ('lst', 'priority'):
                L = layers(year, season, THAILAND)
                out.update(source=L['source'], fast=L['fast'])
                if layer == 'lst':
                    img = lst_level(L['lst'])
                    vis = {'min': 0, 'max': len(LST_BREAKS), 'palette': LST_PALETTE}
                    out['min_zoom'] = 5
                else:
                    out.update(dw_year=L['dw_year'], pop_epoch=L['pop_epoch'])
                    img = priority(L).selfMask()
                    vis = {'min': 1, 'max': 3, 'palette': PRIO_COLORS}
                    out['min_zoom'] = 5 if L['fast'] else 8
            elif layer in ('green', 'built'):
                dwy = dw_year_of(year)
                out['dw_year'] = dwy
                c = dw_mean(dwy, THAILAND)
                if layer == 'green':
                    g = c.select('TREES').add(c.select('GRASS'))
                    img = g.updateMask(g.gte(0.2))
                    vis = {'min': 0.2, 'max': 0.9,
                           'palette': ['#d9f0a3', '#78c679', '#238443', '#004529']}
                else:
                    b = c.select('BUILT')
                    img = b.updateMask(b.gte(0.2))
                    vis = {'min': 0.2, 'max': 0.9,
                           'palette': ['#dadaeb', '#9e9ac8', '#6a51a3', '#3f007d']}
                out['min_zoom'] = 9
            elif layer == 'open':
                epoch = pop_epoch_of(year)
                if assets_ready(['static']):
                    dens = A('static').select(f'DENS{epoch}')
                else:
                    dens = mean_agg(pop_density(epoch)).reproject(GRID)
                o = worldcover_open()
                img = o.selfMask().updateMask(dens.gte(POP_ANY))
                vis = {'min': 0, 'max': 1, 'palette': ['#c8873a', '#c8873a']}
                out['min_zoom'] = 10
            else:  # built_change
                d = built_pct(2020).subtract(built_pct(2005))
                img = d.updateMask(d.gt(2))
                vis = {'min': 2, 'max': 40,
                       'palette': ['#fde0ef', '#f1b6da', '#de77ae', '#c51b7d', '#8e0152']}
                out['min_zoom'] = 6
            out['url'] = img.getMapId(vis)['tile_fetcher'].url_format
            return out

        fast_flag = layers_fast(year)
        return jsonify(cached(f'tiles|{layer}|{year}|{season}|{fast_flag}', calc, ttl=3600))
    except Exception as e:
        return error_response(e)


@app.route('/api/provinces')
def provinces():
    try:
        names = get_province_names()
        return jsonify([{'en': n, 'th': PROVINCE_TH.get(n, n)} for n in names])
    except Exception as e:
        return error_response(e)


@app.route('/api/province_shape')
def province_shape():
    try:
        name = request.args.get('name', '')
        if name not in get_province_names():
            return jsonify({'error': 'ไม่พบจังหวัดนี้'}), 404

        def calc():
            return (provinces_fc().filter(ee.Filter.eq('ADM1_NAME', name))
                    .geometry().simplify(500).getInfo())
        return jsonify(cached('shape|' + name, calc, ttl=24 * 3600))
    except Exception as e:
        return error_response(e)


@app.route('/api/summary', methods=['POST'])
def summary():
    """สรุปพื้นที่: พื้นผิว, ระดับความเร่งด่วน, ที่โล่ง, คนกับความร้อน"""
    try:
        data = request.get_json(force=True)
        year, season = read_year(data), read_season(data)
        region = build_region(data)

        def calc():
            L = layers(year, season, region)
            km2 = ee.Image.pixelArea().divide(1e6)
            people = L['dens'].multiply(km2).rename('PEOPLE')
            open_km2 = L['open'].multiply(km2).rename('OPENKM2')
            cls = priority(L)
            lived = L['dens'].gte(POP_ANY)

            prio_reducer = (ee.Reducer.sum()
                            .combine(reducer2=ee.Reducer.sum(), outputPrefix='open_')
                            .combine(reducer2=ee.Reducer.sum(), outputPrefix='area_')
                            .group(groupField=3, groupName='g'))
            prio_stack = (people.addBands(open_km2)
                          .addBands(km2.rename('AREA')).addBands(cls))
            expo_stack = people.addBands(lst_level(L['lst']).toInt().rename('g'))

            res = ee.Dictionary({
                'prio': rr(prio_stack, prio_reducer, region).get('groups'),
                'expo': rr(expo_stack, ee.Reducer.sum().group(groupField=1, groupName='g'),
                           region).get('groups'),
                'cover_all': rr(L['cover'], ee.Reducer.mean(), region),
                'cover_lived': rr(L['cover'].updateMask(lived), ee.Reducer.mean(), region),
                'pop_total': rr(people, ee.Reducer.sum(), region).get('PEOPLE'),
                'lst_lived': rr(L['lst'].updateMask(lived), ee.Reducer.mean(),
                                region).get('LST'),
                'area_km2': region.area(maxError=100).divide(1e6),
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
            return {
                'year': year, 'season': season, 'source': L['source'],
                'dw_year': L['dw_year'], 'pop_epoch': L['pop_epoch'], 'fast': L['fast'],
                'area_km2': res.get('area_km2'),
                'cover_all': res.get('cover_all') or {},
                'cover_lived': res.get('cover_lived') or {},
                'pop_total': res.get('pop_total') or 0,
                'lst_lived': res.get('lst_lived'),
                'prio': prio, 'expo': expo,
            }

        fast_flag = layers_fast(year)
        return jsonify(cached(f'summary|{region_key(data)}|{year}|{season}|{fast_flag}', calc))
    except Exception as e:
        return error_response(e)


@app.route('/api/bins', methods=['POST'])
def bins():
    """อุณหภูมิในย่านที่มีคนอยู่ แยกตามสัดส่วนพื้นที่สีเขียว และสัดส่วนสิ่งปลูกสร้าง"""
    try:
        data = request.get_json(force=True)
        year, season = read_year(data), read_season(data)
        region = build_region(data)

        def calc():
            L = layers(year, season, region)
            lst = L['lst'].updateMask(L['dens'].gte(POP_ANY))
            reducer = MEAN_COUNT.group(groupField=1, groupName='g')
            built_idx = bin_index(L['cover'].select('BUILT'), BUILT_EDGES).rename('g')
            green_idx = bin_index(L['green'], GREEN_EDGES).rename('g')
            res = ee.Dictionary({
                'built': rr(lst.addBands(built_idx), reducer, region).get('groups'),
                'green': rr(lst.addBands(green_idx), reducer, region).get('groups'),
            }).getInfo()
            return {'year': year, 'season': season, 'source': L['source'],
                    'dw_year': L['dw_year'], 'fast': L['fast'],
                    'built': grouped_rows(res.get('built'), BUILT_LABELS),
                    'green': grouped_rows(res.get('green'), GREEN_LABELS)}

        fast_flag = layers_fast(year)
        return jsonify(cached(f'bins|{region_key(data)}|{year}|{season}|{fast_flag}', calc))
    except Exception as e:
        return error_response(e)


def lst_block(y0, y1, season, fast):
    """LST เฉลี่ยของช่วงปี (Aqua)"""
    if fast:
        imgs = [A(f'lst_{y}').select(f'LST_{season}') for y in range(y0, y1 + 1)]
        return ee.ImageCollection(imgs).mean().rename('LST')
    return aqua_col(f'{y0}-01-01', f'{y1 + 1}-01-01', season).mean().rename('LST')


def history_inputs(fast):
    if fast:
        st = A('static')
        gb = {e: st.select(f'GB{e}').rename('BUILT') for e in POP_EPOCHS}
        lived = st.select('DENS2020').gte(POP_ANY)
    else:
        gb = {e: mean_agg(built_pct(e)).reproject(GRID) for e in POP_EPOCHS}
        lived = mean_agg(pop_density(2020)).reproject(GRID).gte(POP_ANY)
    return gb, lived


def history_fast():
    return assets_ready(['static'] + [f'lst_{y}' for y in range(FIRST_YEAR, AQUA_LAST_YEAR + 1)])


@app.route('/api/trend', methods=['POST'])
def trend():
    """ย้อนหลัง 20 ปี: สัดส่วนอาคาร vs อุณหภูมิ ทุก 5 ปี (เฉพาะย่านที่มีคนอยู่)"""
    try:
        data = request.get_json(force=True)
        season = read_season(data)
        region = build_region(data)
        fast = history_fast()

        def calc():
            gb, lived = history_inputs(fast)
            out = {}
            for epoch, y0, y1 in BLOCKS:
                lst = lst_block(y0, y1, season, fast)
                img = lst.addBands(gb[epoch]).updateMask(lst.mask().And(lived))
                out[str(epoch)] = rr(img, ee.Reducer.mean(), region)
            res = ee.Dictionary(out).getInfo()
            rows = [{'epoch': e, 'label': BLOCK_LABELS[e],
                     'lst': (res.get(str(e)) or {}).get('LST'),
                     'built': (res.get(str(e)) or {}).get('BUILT')}
                    for e, _, _ in BLOCKS]
            return {'season': season, 'rows': rows, 'fast': fast}

        return jsonify(cached(f'trend|{region_key(data)}|{season}|{fast}', calc))
    except Exception as e:
        return error_response(e)


@app.route('/api/change', methods=['POST'])
def change():
    """อุณหภูมิที่เปลี่ยนไป (2018-22 เทียบ 2003-07) แยกตามระดับที่อาคารเพิ่ม (2005 -> 2020)"""
    try:
        data = request.get_json(force=True)
        season = read_season(data)
        region = build_region(data)
        fast = history_fast()

        def calc():
            gb, _ = history_inputs(fast)
            d_lst = (lst_block(2018, 2022, season, fast)
                     .subtract(lst_block(2003, 2007, season, fast)).rename('DLST'))
            b05, b20 = gb[2005], gb[2020]
            d_b = b20.subtract(b05)
            cls = ee.Image(-1)
            cls = cls.where(d_b.lte(1).And(b05.lt(5)), 0)      # กลุ่มเทียบ
            cls = cls.where(d_b.gt(1).And(d_b.lte(4)), 1)
            cls = cls.where(d_b.gt(4).And(d_b.lte(8)), 2)
            cls = cls.where(d_b.gt(8), 3)
            cls = cls.where(d_b.lte(1).And(b05.gte(25)), 4)    # เมืองหนาแน่นอยู่แล้ว
            cls = cls.updateMask(cls.gte(0)).rename('g').toInt()
            groups = ee.List(rr(d_lst.addBands(cls), MEAN_COUNT.group(groupField=1, groupName='g'),
                                region).get('groups')).getInfo()
            return {'season': season, 'rows': grouped_rows(groups, CHANGE_LABELS), 'fast': fast}

        return jsonify(cached(f'change|{region_key(data)}|{season}|{fast}', calc))
    except Exception as e:
        return error_response(e)


@app.route('/api/validate', methods=['POST'])
def validate():
    """ความคลาดเคลื่อนของ VIIRS ที่ปรับเทียบแล้วเทียบกับ Aqua ในย่านที่มีคนอยู่"""
    try:
        data = request.get_json(force=True)
        season = read_season(data)
        region = build_region(data)
        if not assets_ready(['vcheck', 'static']):
            return jsonify({'ready': False})

        def calc():
            d = A('vcheck').select(f'D_{season}').rename('D')
            d = d.updateMask(A('static').select('DENS2020').gte(POP_ANY))
            stats = rr(d.addBands(d.abs().rename('ABS')),
                       ee.Reducer.mean().combine(ee.Reducer.count(), sharedInputs=True),
                       region).getInfo()
            return {'ready': True, 'season': season, 'years': CHECK_YEARS,
                    'bias_years': BIAS_YEARS, 'mean': stats.get('D_mean'),
                    'mae': stats.get('ABS_mean'), 'count': stats.get('D_count')}
        return jsonify(cached(f'validate|{region_key(data)}|{season}', calc))
    except Exception as e:
        return error_response(e)


maybe_precompute()

if __name__ == '__main__':
    app.run(debug=True)
