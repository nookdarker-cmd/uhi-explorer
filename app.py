"""UHI Explorer - backend (Flask + Google Earth Engine)

เป้าหมาย: หาย่านที่ควรปรับปรุงเพื่อลดความร้อนให้ผู้อยู่อาศัย
  เร่งด่วน = ผิวร้อนมาก + คนอยู่หนาแน่น + พื้นที่สีเขียวน้อย
  แล้วดูต่อว่ามี "พื้นที่ปลูกต้นไม้เพิ่มได้" (หญ้า พุ่มไม้ ดินเปล่า) ในย่านนั้นหรือไม่
และเทียบความร้อนของเมืองกับชนบทรอบ ๆ (SUHII) ระหว่างกรุงเทพฯ กับขอนแก่น

ข้อมูล (วิเคราะห์บนกริด 30 ฟิลิปดา ≈ 1 กม.)
  - อุณหภูมิพื้นผิวช่วงบ่าย 12-14 น. : MODIS Aqua (2003-2022), VIIRS (2023 ขึ้นไป
    ปรับเทียบให้อยู่ระดับเดียวกับ Aqua รายพิกเซล จากวันที่วัดได้ทั้งสองดาวเทียม 2013-2020
    และตรวจสอบด้วยปี 2021-2022) ใช้เฉพาะพิกเซลที่ฟ้าโปร่งอย่างน้อย N_MIN วันในฤดู/ปีนั้น
  - พื้นผิว (สิ่งปลูกสร้างรวมถนน, ต้นไม้, หญ้า, ดินเปล่า) : Dynamic World 10 ม. (2016 ขึ้นไป)
    ใช้ประเภทที่พบบ่อยที่สุดของแต่ละพิกเซลในปีนั้น (mode) แล้วนับสัดส่วนพิกเซลในช่อง
  - พื้นที่ปลูกต้นไม้เพิ่มได้ (หญ้า พุ่มไม้ ดินเปล่า) และแหล่งน้ำ : ESA WorldCover 2021 10 ม.
  - สิ่งปลูกสร้างย้อนหลัง ประชากร และระดับความเป็นเมือง : GHSL (ทุก 5 ปี)
  - ความสูงพื้นดิน : SRTM 30 ม.

ความเร็ว: เซิร์ฟเวอร์สั่ง Earth Engine คำนวณชั้นข้อมูลล่วงหน้าเก็บเป็น asset ใน
projects/<project>/assets/uhi_v2 ระหว่างที่ยังไม่เสร็จจะใช้ชุดเก่า (uhi_v1) หรือคำนวณสดแทน
"""
import hashlib
import json
import math
import os
import threading
import time
import traceback
from collections import OrderedDict, deque
from datetime import date, datetime, timezone

import ee
from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import RequestEntityTooLarge

PROJECT_ID = os.environ.get('EE_PROJECT', 'hicm-510809')
ASSET_V1 = f'projects/{PROJECT_ID}/assets/uhi_v1'   # ชุดเก่า ใช้สำรองระหว่างรอชุดใหม่ (ห้ามลบอัตโนมัติ)
ASSET_V2 = f'projects/{PROJECT_ID}/assets/uhi_v2'   # ชุดปัจจุบัน
ASSET_VERSION = 2
TASK_PREFIX = 'uhi_v2_'
# ติดไว้ที่ทุก asset ที่ export (property 'uhi_code_version') เพื่อรู้ว่าสร้างจากโค้ดรุ่นไหน
CODE_VERSION = 'v2.0'
EE_DEADLINE_MS = 240_000         # คำขอ Earth Engine ที่ค้างนานกว่านี้ให้ยกเลิก (gunicorn timeout = 300 วิ)
# สั่งคำนวณล่วงหน้าอัตโนมัติบนเว็บจริง (Render ตั้งค่า RENDER=true ให้เอง)
PRECOMPUTE = os.environ.get('UHI_PRECOMPUTE', '1' if os.environ.get('RENDER') else '0') == '1'
PRECOMPUTE_EVERY_S = 30 * 60     # ตรวจ/สั่งงานใหม่ได้ไม่เกินทุก 30 นาที
MAX_RETRIES = 2                  # งานที่ล้มเหลว สั่งใหม่ได้อีกไม่เกิน 2 ครั้ง
ACTIVE_STATES = ('PENDING', 'READY', 'RUNNING')


def init_earth_engine():
    """บนเว็บจริง: ใช้ service account key จาก environment variable
    บนเครื่องตัวเอง: ใช้การล็อกอินจาก `earthengine authenticate`"""
    key_json = os.environ.get('EE_SERVICE_ACCOUNT_KEY')
    if key_json:
        info = json.loads(key_json)
        # ใช้ key จากหน่วยความจำโดยตรง ไม่เขียนไฟล์ key ลงดิสก์
        credentials = ee.ServiceAccountCredentials(info['client_email'], key_data=key_json)
        ee.Initialize(credentials, project=PROJECT_ID)
    else:
        ee.Initialize(project=PROJECT_ID)
    ee.data.setDeadline(EE_DEADLINE_MS)


init_earth_engine()

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 256 * 1024     # กันคำขอขนาดใหญ่ผิดปกติ

THAILAND = ee.Geometry.Rectangle([97.3, 5.6, 105.7, 20.5])
GRID_CRS = 'EPSG:4326'
GRID_TRANSFORM = [1 / 120, 0, 97.3, 0, -1 / 120, 20.5]   # 30 ฟิลิปดา ≈ 0.9 กม. (ช่องละ ≈ 0.85 ตร.กม.)
GRID = ee.Projection(GRID_CRS, GRID_TRANSFORM)

HOUR_MIN, HOUR_MAX = 12, 14      # ช่วงเวลาสังเกต (เวลาสุริยะท้องถิ่น)
FIRST_YEAR = 2003                # ปีแรกที่ Aqua มีข้อมูลเต็มปี
AQUA_LAST_YEAR = 2022            # หลังจากนี้ Aqua เลื่อนเวลาผ่านจนเกิน 14 น.
DW_FIRST_YEAR = 2016             # ปีแรกที่ Dynamic World มีข้อมูลเต็มปี
BIAS_YEARS = (2013, 2020)        # ช่วงที่ใช้ปรับเทียบ VIIRS กับ Aqua
CHECK_YEARS = (2021, 2022)       # ช่วงที่ใช้ตรวจสอบการปรับเทียบ (ไม่ได้ใช้ตอนปรับเทียบ)
DEFAULT_YEAR = 2022              # ปีที่เปิดเป็นค่าเริ่มต้น (ปีสุดท้ายของ Aqua)
POP_EPOCHS = (2005, 2010, 2015, 2020)
MAX_AREA_KM2 = 30000             # พื้นที่ที่วาดได้มากสุด

# ------------------------------------------------------------ เกณฑ์
LST_BREAKS = [30, 34, 38, 42]    # ระดับสีอุณหภูมิพื้นผิว (°C)
LST_PALETTE = ['#2b83ba', '#abdda4', '#ffffbf', '#fdae61', '#d7191c']
HOT = 34                         # ผิวร้อน (°C) -> เฝ้าระวัง
VERY_HOT = 38                    # ผิวร้อนมาก (°C) -> เร่งด่วน
POP_ANY = 300                    # คน/ตร.กม. ขึ้นไป = มีคนอยู่ (นิยาม urban cluster ของ GHSL)
POP_DENSE = 1500                 # คน/ตร.กม. ขึ้นไป = หนาแน่น (นิยาม urban centre ของ GHSL)
GREEN_LOW = 0.15                 # พื้นที่สีเขียว (ต้นไม้+หญ้า) น้อยกว่านี้ = เขียวน้อย
# พื้นที่ปลูกต้นไม้เพิ่มได้ตั้งแต่ 5% ของช่อง (ช่อง ≈ 0.85 ตร.กม. ≈ 530 ไร่ -> 5% ≈ 26 ไร่)
PLANT_MIN = 0.05
PLANT_CLASSES = (20, 30, 60)     # WorldCover: พุ่มไม้, ทุ่งหญ้า, ดินเปล่า/พืชพรรณบาง (ยังไม่มีต้นไม้)
WATER_CLASS = 80                 # WorldCover: แหล่งน้ำถาวร
N_MIN = 5                        # ต้องมีวันฟ้าโปร่งอย่างน้อยเท่านี้ต่อพิกเซลต่อฤดู/ปี
# เฝ้าระวัง / เร่งด่วน (ไม่มีที่ปลูกเพิ่ม) / เร่งด่วน (มีที่ปลูกเพิ่ม) - เร่งด่วนใช้สีแดงเดียวกัน
PRIO_COLORS = ['#f2c14e', '#d1352b', '#d1352b']
POINT_RADIUS_M = 2000            # คลิกจุด = วิเคราะห์วงกลมรัศมีนี้

# ทดสอบความไวของผลต่อเกณฑ์: อุณหภูมิ x สัดส่วนพื้นที่สีเขียว
SENS_TEMPS = [36, 38, 40]
SENS_GREENS = [0.10, 0.15, 0.20]

# ปรับเทียบ VIIRS
VB_MIN_PAIRS = 10                # ต้องมีวันที่วัดคู่กันอย่างน้อยเท่านี้
VB_FILL_M = 10000                # เติมช่องว่างด้วยค่าเฉลี่ยรอบ ๆ รัศมี 10 กม.

# SUHII: เขตเมือง (GHSL SMOD) เทียบชนบทรอบ ๆ
SUHI_URBAN_CODE = 30             # urban centre
SUHI_CLUSTER_MIN = 22            # สำรอง: semi-dense urban cluster ขึ้นไป
SUHI_RURAL_CODES = [11, 12, 13]  # ชนบท
SUHI_RING_M = (2000, 20000)      # ชนบทต้องห่างเขตเมือง 2-20 กม.
SUHI_SEARCH_M = 25000            # ค้นหาชนบทในระยะ 25 กม. จากขอบจังหวัด
SUHI_ELEV_TOL = 50               # ความสูงต่างจากเขตเมืองไม่เกิน 50 ม.
SUHI_WATER_MAX = 0.3             # ไม่นับช่องที่เป็นน้ำ 30% ขึ้นไป
SUHI_MIN_URBAN_CELLS = 3         # ถ้าจังหวัดมี urban centre น้อยกว่านี้ ใช้ urban cluster แทน
SUHI_MIN_RURAL = 10              # ช่องชนบทน้อยกว่านี้ ไม่คำนวณ SUHII
COMPARE_CITIES = ['Bangkok', 'Khon Kaen']

MAX_LIVE_TREND_YEARS = 6         # คำนวณสดได้ไม่เกินกี่ปีต่อคำขอ (กันหมดเวลา)

SEASONS = {'all': 'ทั้งปี', 'hot': 'หน้าร้อน (มี.ค.–พ.ค.)',
           'wet': 'หน้าฝน (มิ.ย.–ต.ค.)', 'cool': 'หน้าหนาว (พ.ย.–ก.พ.)'}
SEASON_ORDER = ['all', 'hot', 'wet', 'cool']

BUILT_EDGES = [0.2, 0.4, 0.6, 0.8]
BUILT_LABELS = ['ต่ำกว่า 20%', '20–40%', '40–60%', '60–80%', '80% ขึ้นไป']
GREEN_EDGES = [0.1, 0.2, 0.3, 0.5]
GREEN_LABELS = ['ต่ำกว่า 10%', '10–20%', '20–30%', '30–50%', '50% ขึ้นไป']

# อุณหภูมิที่เปลี่ยนไป: เฉลี่ย 2018-22 เทียบ 2003-07 (Aqua ทั้งสองช่วง)
CHANGE_BLOCKS = ((2003, 2007), (2018, 2022))
CONTROL_GAP_M = 2000             # กลุ่มเทียบต้องห่างย่านที่อาคารเพิ่มเกิน 4 จุด อย่างน้อย 2 กม.
# กลุ่มตามพื้นที่อาคารที่เพิ่มขึ้น 2005 -> 2020 (จุดเปอร์เซ็นต์ของพื้นที่ช่อง)
CHANGE_LABELS = ['ไม่มีอาคารและไม่เปลี่ยน (กลุ่มเทียบ)', 'อาคารเพิ่มเล็กน้อย (1–4 จุด)',
                 'อาคารเพิ่มปานกลาง (4–8 จุด)', 'อาคารเพิ่มมาก (เกิน 8 จุด)',
                 'เมืองหนาแน่นอยู่แล้ว (คงเดิม)']

COVER_BANDS = ['BUILT', 'TREES', 'GRASS', 'BARE', 'CROPS', 'WATER']

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


# ---------------------------------------------------------------- ข้อผิดพลาด

class BadInput(ValueError):
    """ข้อมูลที่ผู้ใช้ส่งมาไม่ถูกต้อง (ข้อความภาษาไทยแสดงให้ผู้ใช้เห็นได้เลย)"""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def error_kind(e):
    """'busy' = โควตา/ผู้ใช้พร้อมกันมาก, 'heavy' = คำนวณหนัก/หมดเวลา, None = อื่น ๆ"""
    low = str(e).lower()
    if ('quota' in low or 'rate limit' in low or 'concurrent' in low
            or 'too many requests' in low or '429' in low):
        return 'busy'
    if (isinstance(e, TimeoutError) or 'memory limit' in low or 'timed out' in low
            or 'timeout' in low or 'deadline' in low or 'too many' in low or 'capacity' in low):
        return 'heavy'
    return None


# ---------------------------------------------------------------- แคชผลลัพธ์
# LRU จำกัดขนาด + TTL, คำขอเดียวกันที่มาพร้อมกันคำนวณครั้งเดียว (คนอื่นรอผล),
# และจำความล้มเหลวจากงานหนัก/โควตาไว้ 3 นาที กันการกดซ้ำแล้วเผาโควตา

CACHE_MAX = 500
NEG_TTL = 180                    # วินาที
WAIT_S = 250                     # คำขอที่รอผลคนอื่นรอได้นานสุด

_cache = OrderedDict()           # key -> (หมดอายุเมื่อ, ok, ค่า หรือ exception)
_inflight = {}                   # key -> _Flight
_cache_lock = threading.Lock()


class _Flight:
    def __init__(self):
        self.event = threading.Event()
        self.ok = False
        self.value = None


def cached(key, fn, ttl=6 * 3600):
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            if hit[0] > time.time():
                _cache.move_to_end(key)
                if hit[1]:
                    return hit[2]
                raise hit[2]
            del _cache[key]
        flight = _inflight.get(key)
        leader = flight is None
        if leader:
            flight = _inflight[key] = _Flight()
    if not leader:
        if not flight.event.wait(WAIT_S):
            raise TimeoutError('timed out waiting for the same request')
        if flight.ok:
            return flight.value
        raise flight.value
    try:
        flight.value, flight.ok = fn(), True            # คำนวณนอก lock (อาจนาน)
    except Exception as e:
        flight.value, flight.ok = e, False
    with _cache_lock:
        if flight.ok:
            _cache[key] = (time.time() + ttl, True, flight.value)
        elif not isinstance(flight.value, BadInput) and error_kind(flight.value):
            _cache[key] = (time.time() + NEG_TTL, False, flight.value)
        while len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)
        _inflight.pop(key, None)
    flight.event.set()
    if flight.ok:
        return flight.value
    raise flight.value


# ---------------------------------------------------------------- ปีล่าสุดที่ข้อมูลครบ
# ต้นเดือนมกราคม ข้อมูลของปีที่เพิ่งจบยังมาไม่ครบ ถ้า export ไปตอนนั้นจะได้ asset ที่ขาดข้อมูลค้างไว้ถาวร
# ปี Y นับว่าครบเมื่อภาพ VIIRS (VNP21A1D) ล่าสุดไม่ก่อนวันที่ 25 ธ.ค. ของปี Y

_years = {'last': datetime.now(timezone.utc).year - 1, 'verified': False, 't': 0.0}
_years_lock = threading.Lock()


def complete_year_from(latest_ms, today):
    d = datetime.fromtimestamp(latest_ms / 1000, tz=timezone.utc).date()
    y = d.year if d >= date(d.year, 12, 25) else d.year - 1
    return min(y, today.year - 1)


def refresh_years(force=False):
    """ตรวจปีล่าสุดที่ข้อมูลครบ (getInfo เบา ๆ ครั้งเดียว) จำไว้ 12 ชม.; ถ้าตรวจไม่ได้ใช้ปีที่แล้ว
    แบบ 'ยังไม่ยืนยัน' (แสดงได้แต่จะไม่ export ปีนั้น) แล้วลองใหม่ใน 10 นาที"""
    ttl = 12 * 3600 if _years['verified'] else 600
    if not force and time.time() - _years['t'] < ttl:
        return
    if not _years_lock.acquire(blocking=False):
        return
    try:
        _years['t'] = time.time()
        latest = (ee.ImageCollection('NASA/VIIRS/002/VNP21A1D')
                  .aggregate_max('system:time_start').getInfo())
        if not isinstance(latest, (int, float)):
            raise ValueError('no VIIRS dates')
        _years['last'] = complete_year_from(latest, datetime.now(timezone.utc).date())
        _years['verified'] = True
    except Exception as e:
        print('[years] check failed:', repr(e)[:200], flush=True)
        if not _years['verified']:
            _years['last'] = datetime.now(timezone.utc).year - 1
    finally:
        _years_lock.release()


def last_year():
    refresh_years()
    return _years['last']


def all_years():
    return list(range(FIRST_YEAR, last_year() + 1))


refresh_years(force=True)


# ---------------------------------------------------------------- ฤดู

def season_window(year, season):
    """ช่วงวันที่ของฤดูในปีนั้น (หน้าหนาวของปี Y = พ.ย. ปี Y-1 ถึง ก.พ. ปี Y)"""
    if season == 'hot':
        return f'{year}-03-01', f'{year}-06-01'
    if season == 'wet':
        return f'{year}-06-01', f'{year}-11-01'
    if season == 'cool':
        return f'{year - 1}-11-01', f'{year}-03-01'
    return f'{year}-01-01', f'{year + 1}-01-01'


def season_filter(col, season):
    """กรองตามเดือนของฤดู (ใช้กับช่วงหลายปี เช่น ตอนปรับเทียบ)"""
    if season == 'hot':
        return col.filter(ee.Filter.calendarRange(3, 5, 'month'))
    if season == 'wet':
        return col.filter(ee.Filter.calendarRange(6, 10, 'month'))
    if season == 'cool':
        return col.filter(ee.Filter.Or(
            ee.Filter.calendarRange(11, 12, 'month'),
            ee.Filter.calendarRange(1, 2, 'month')))
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


# VNP21A1D ใน Earth Engine: LST_1KM เป็นเคลวินแล้ว และ View_Time เป็นชั่วโมงแล้ว
# (ตรวจจากข้อมูลจริงบนเว็บที่ใช้งานอยู่) จึงไม่ต้องคูณ scale
VIIRS_LST_SCALE = 1.0
VIIRS_TIME_SCALE = 1.0


def viirs_day(img):
    """VIIRS รายวัน -> LST (°C) ช่วง 12-14 น. เกณฑ์เดียวกับ Aqua"""
    raw = img.select('LST_1KM').multiply(VIIRS_LST_SCALE)
    t = img.select('View_Time').multiply(VIIRS_TIME_SCALE)
    qc = img.select('QC')
    # ผลิตค่าได้, ท้องฟ้าโปร่ง, ความแม่นยำ LST ไม่แย่กว่า 2 K
    good = (qc.bitwiseAnd(3).lte(1)                        # bits 0-1: ผลิตค่าได้
            .And(qc.rightShift(4).bitwiseAnd(3).eq(0))     # bits 4-5: ไม่มีเมฆ
            .And(qc.rightShift(14).bitwiseAnd(3).gte(1))   # bits 14-15: error <= 2 K
            .And(t.gte(HOUR_MIN)).And(t.lte(HOUR_MAX))
            .And(raw.gt(200)))
    lst = raw.subtract(273.15).rename('LST')
    return lst.updateMask(good).copyProperties(img, ['system:time_start'])


def aqua_col(start, end, season='all'):
    col = ee.ImageCollection('MODIS/061/MYD11A1').filterDate(start, end)
    return season_filter(col, season).map(aqua_day)


def viirs_col(start, end, season='all'):
    col = ee.ImageCollection('NASA/VIIRS/002/VNP21A1D').filterDate(start, end)
    return season_filter(col, season).map(viirs_day)


def source_of(year):
    return 'Aqua' if year <= AQUA_LAST_YEAR else 'VIIRS'


def year_col(year, season):
    """ภาพรายวันของฤดู/ปี จากดาวเทียมของปีนั้น (ยังไม่ปรับเทียบ)"""
    start, end = season_window(year, season)
    return aqua_col(start, end) if source_of(year) == 'Aqua' else viirs_col(start, end)


def col_mean_count(col):
    """ค่าเฉลี่ย ('LST') และจำนวนวันที่มีค่า ('N') รายพิกเซล - ปลอดภัยถ้าไม่มีภาพเลย"""
    has = col.size().gt(0)
    zero = ee.Image.constant(0).toFloat()
    mean = ee.Image(ee.Algorithms.If(has, col.mean(), zero.updateMask(0))).rename('LST')
    count = ee.Image(ee.Algorithms.If(has, col.count(), zero)).rename('N')
    return mean, count


def paired_diffs(start, end, season, bias=None):
    """ส่วนต่าง VIIRS − Aqua เฉพาะวันที่ทั้งสองดาวเทียมวัดได้ที่พิกเซลเดียวกัน ('D')
    (ถ้าให้ bias มาด้วย จะได้ (VIIRS − bias) − Aqua ไว้ตรวจสอบการปรับเทียบ)"""
    a = aqua_col(start, end, season)
    v = viirs_col(start, end, season)
    # ภาพรายวันของทั้งสองชุดเริ่มเวลา 00:00 UTC ของวันนั้น -> ต่างกันไม่เกิน 12 ชม. = วันเดียวกัน
    same_day = ee.Filter.maxDifference(difference=12 * 3600 * 1000,
                                       leftField='system:time_start',
                                       rightField='system:time_start')
    joined = ee.ImageCollection(ee.Join.saveFirst('match').apply(v, a, same_day))

    def diff(img):
        d = img.subtract(ee.Image(img.get('match')))       # มีค่าเฉพาะเมื่อวัดได้ทั้งคู่
        if bias is not None:
            d = d.subtract(bias)
        return d.rename('D')

    return joined.map(diff)


def vbias_live(season):
    """ส่วนต่างแบบคร่าว (คำนวณสด) ใช้เฉพาะตอนที่ยังไม่มีผลปรับเทียบแบบจับคู่วัน"""
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


# ---------------------------------------------------------------- พื้นผิว / ประชากร / ภูมิประเทศ

def dw_year_of(year):
    return max(DW_FIRST_YEAR, min(year, last_year()))


def pop_epoch_of(year):
    """GHSL มีข้อมูลทุก 5 ปี ใช้ปีที่ใกล้ที่สุด"""
    e = int(round(year / 5.0)) * 5
    return max(POP_EPOCHS[0], min(POP_EPOCHS[-1], e))


def dw_mode_label(year, region):
    """ประเภทพื้นผิวที่พบบ่อยที่สุดของแต่ละพิกเซลในปีนั้น (Dynamic World 10 ม.)
    0 น้ำ 1 ต้นไม้ 2 หญ้า 3 พืชน้ำท่วมขัง 4 พืชไร่ 5 พุ่มไม้ 6 สิ่งปลูกสร้าง 7 ดินเปล่า 8 หิมะ"""
    return (ee.ImageCollection('GOOGLE/DYNAMICWORLD/V1')
            .filterBounds(region)
            .filterDate(f'{year}-01-01', f'{year + 1}-01-01')
            .select('label')
            .reduce(ee.Reducer.mode()))


def dw_mode_fractions(year, region, scale=100):
    """สัดส่วนพิกเซลแต่ละประเภท (0-1) พร้อมรวมเป็นช่อง 1 กม. (ใช้ reproject/export ไปที่ GRID)"""
    lab = dw_mode_label(year, region).reproject(crs='EPSG:4326', scale=scale)
    return ee.Image.cat([
        lab.eq(6).rename('BUILT'),
        lab.eq(1).rename('TREES'),
        lab.eq(2).Or(lab.eq(5)).rename('GRASS'),
        lab.eq(7).rename('BARE'),
        lab.eq(4).rename('CROPS'),
        lab.eq(0).Or(lab.eq(3)).rename('WATER'),
    ]).reduceResolution(reducer=ee.Reducer.mean(), maxPixels=1024)


def worldcover():
    return ee.ImageCollection('ESA/WorldCover/v200').first().select('Map')


def worldcover_plant():
    """พื้นที่ปลูกต้นไม้เพิ่มได้ 10 ม.: พุ่มไม้ ทุ่งหญ้า ดินเปล่า (ยังไม่มีต้นไม้)"""
    wc = worldcover()
    out = None
    for c in PLANT_CLASSES:
        out = wc.eq(c) if out is None else out.Or(wc.eq(c))
    return out.rename('PLANT')


def fraction_agg(binary, scale):
    """สัดส่วนต่อช่อง 1 กม.: asset ใช้ขั้นกลาง 30 ม. (ละเอียด), คำนวณสดใช้ 100 ม. (เร็วกว่า)"""
    return (binary.reproject(crs='EPSG:4326', scale=scale)
            .reduceResolution(reducer=ee.Reducer.mean(), maxPixels=1024))


def plant_agg(scale=30):
    return fraction_agg(worldcover_plant(), scale).rename('PLANT')


def water_agg(scale=30):
    return fraction_agg(worldcover().eq(WATER_CLASS), scale).rename('WATERF')


def built_pct(epoch):
    """สัดส่วนอาคาร (%) ต่อเซลล์ 100 ม. (เซลล์ละ 10,000 ตร.ม.)"""
    return (ee.Image(f'JRC/GHSL/P2023A/GHS_BUILT_S/{epoch}')
            .select('built_surface').divide(100).rename('BUILT'))


def pop_density(epoch):
    """ความหนาแน่นประชากร (คน/ตร.กม.) - เซลล์ 100 ม. = 1 เฮกตาร์ จึงคูณ 100"""
    return (ee.Image(f'JRC/GHSL/P2023A/GHS_POP/{epoch}')
            .select('population_count').multiply(100).rename('DENS'))


def smod_img(epoch):
    """ระดับความเป็นเมืองของ GHSL (1 กม.): 30 = urban centre, 21-23 = urban cluster, 11-13 = ชนบท"""
    return ee.Image(f'JRC/GHSL/P2023A/GHS_SMOD_V2-0/{epoch}').select('smod_code')


def elev_img():
    return ee.Image('USGS/SRTMGL1_003').select('elevation')


def mean_agg(img):
    return img.reduceResolution(reducer=ee.Reducer.mean(), maxPixels=1024)


# ---------------------------------------------------------------- assets ที่คำนวณล่วงหน้า

_assets = {}                     # folder -> (เวลา, set ของชื่อ asset)
_assets_lock = threading.Lock()


def _list_folder(folder):
    """รายชื่อ asset ในโฟลเดอร์ (ตาม nextPageToken จนครบ)"""
    ids, token = set(), None
    while True:
        params = {'parent': folder, 'pageSize': 1000}
        if token:
            params['pageToken'] = token
        res = ee.data.listAssets(params) or {}
        for a in res.get('assets') or []:
            aid = a.get('id') or a.get('name') or ''
            ids.add(aid.rsplit('/', 1)[-1])
        token = res.get('nextPageToken')
        if not token:
            return ids


def _is_not_found(e):
    low = str(e).lower()
    return 'not found' in low or 'does not exist' in low or '404' in low


def existing_assets(folder=ASSET_V2, force=False, strict=False):
    """ชื่อ asset ที่มีแล้ว (แคชแยกต่อโฟลเดอร์ 120 วินาที) - โฟลเดอร์ที่ยังไม่มีถือว่าว่าง
    strict=True: ถ้าอ่านรายการไม่สำเร็จ (ที่ไม่ใช่ 'ไม่พบโฟลเดอร์') ให้โยน error"""
    now = time.time()
    with _assets_lock:
        hit = _assets.get(folder)
    if hit and not force and now - hit[0] < 120:
        return hit[1]
    stamp = time.time()
    try:
        ids = _list_folder(folder)
    except Exception as e:
        if _is_not_found(e):
            ids = set()
        elif strict:
            raise
        else:
            # ผิดพลาดชั่วคราว: ใช้รายการเดิมไปก่อน แล้วลองใหม่ในอีก ~20 วินาที
            print('[assets] list failed', folder, repr(e), flush=True)
            ids = hit[1] if hit else set()
            stamp = time.time() - 100
    with _assets_lock:
        _assets[folder] = (stamp, ids)
    return ids


def A1(name):
    return ee.Image(f'{ASSET_V1}/{name}')


def A2(name):
    return ee.Image(f'{ASSET_V2}/{name}')


def needed_assets():
    """เรียงตามความสำคัญ: ปีเริ่มต้นก่อน แล้วข้อมูลย้อนหลัง แล้วปีอื่น ๆ"""
    d, last = DEFAULT_YEAR, last_year()
    names = ['static', f'lst_{d}', f'dw_{dw_year_of(d)}']
    names += [f'lst_{y}' for y in range(FIRST_YEAR, AQUA_LAST_YEAR + 1) if y != d]
    names += ['vbias']
    names += [f'lst_{y}' for y in range(AQUA_LAST_YEAR + 1, last + 1) if y != d]
    names += [f'dw_{y}' for y in range(last, DW_FIRST_YEAR - 1, -1)
              if f'dw_{y}' not in names]
    names += ['vcheck']
    return names


def precompute_progress():
    need = needed_assets()
    have = existing_assets(ASSET_V2)
    return {'ready': sum(1 for n in need if n in have), 'total': len(need),
            'version': ASSET_VERSION}


# ---------------------------------------------------------------- ตัวเลือกแหล่งข้อมูล (เร็ว/ช้า)
# ทุกฟังก์ชัน *_info / *_version ดูจากรายการ asset ที่แคชไว้เท่านั้น (ไม่เรียกคำนวณบน Earth Engine)

def lst_info(year, season):
    """เลือกแหล่ง LST: v2 > v1 (เฉพาะทั้งปี เพราะฤดูของ v1 ไม่ตรงกับฤดูใหม่) > คำนวณสด
    ปี VIIRS ต้องลบส่วนต่าง: v2 (จับคู่วัน รายฤดู) > v1 (ทั้งปี) > คำนวณสดแบบคร่าว"""
    v2, v1 = existing_assets(ASSET_V2), existing_assets(ASSET_V1)
    name = f'lst_{year}'
    if name in v2:
        raw_v = 2
    elif season == 'all' and name in v1:
        raw_v = 1
    else:
        raw_v = 0
    source = source_of(year)
    bias_v = None
    if source == 'VIIRS':
        bias_v = 2 if 'vbias' in v2 else (1 if 'vbias' in v1 else 0)
    return {'source': source, 'fast': raw_v > 0 and bias_v != 0, 'v': raw_v, 'bv': bias_v}


def lst_key(info):
    return f"{info['v']}{info['bv'] if info['bv'] is not None else '-'}"


def bias_image(season, bias_v):
    if bias_v == 2:
        return A2('vbias').select(f'VB_{season}')
    if bias_v == 1:
        return A1('vbias').select('VB_all')
    return vbias_live(season)


def lst_image(year, season):
    """LST (°C) ของปี/ฤดู ระดับเดียวกับ Aqua -> (ee.Image 'LST', info)"""
    info = lst_info(year, season)
    if info['v'] == 2:
        a = A2(f'lst_{year}')
        lst = a.select(f'LST_{season}').updateMask(a.select(f'N_{season}').gte(N_MIN))
    elif info['v'] == 1:
        lst = A1(f'lst_{year}').select('LST_all')
    else:
        mean, count = col_mean_count(year_col(year, season))
        lst = mean.updateMask(count.gte(N_MIN))
    if info['source'] == 'VIIRS':
        lst = lst.subtract(bias_image(season, info['bv']))
    return lst.rename('LST'), info


def cover_info(year):
    """(dw_year, method, fast, version) - v2 = mode, v1 = ค่าเฉลี่ยความน่าจะเป็น (ชุดเก่า)"""
    dwy = dw_year_of(year)
    if f'dw_{dwy}' in existing_assets(ASSET_V2):
        return dwy, 'mode', True, 2
    if f'dw_{dwy}' in existing_assets(ASSET_V1):
        return dwy, 'prob', True, 1
    return dwy, 'mode', False, 0


def cover_image(year, region):
    """สัดส่วนพื้นผิวบนกริด -> (ee.Image 6 แบนด์, method, fast, dw_year)"""
    dwy, method, fast, v = cover_info(year)
    if v == 2:
        img = A2(f'dw_{dwy}')
    elif v == 1:
        img = A1(f'dw_{dwy}')
    else:
        img = dw_mode_fractions(dwy, region, scale=300).reproject(GRID)
    return img.select(COVER_BANDS), method, fast, dwy


def static_version():
    if 'static' in existing_assets(ASSET_V2):
        return 2
    if 'static' in existing_assets(ASSET_V1):
        return 1
    return 0


def live_static_band(name):
    """แบนด์ของ static ที่คำนวณสดบนกริด (ช้ากว่า asset)"""
    if name == 'PLANT':
        return plant_agg(100).reproject(GRID)
    if name == 'WATERF':
        return water_agg(100).reproject(GRID)
    if name == 'ELEV':
        return elev_img().reproject(GRID).rename('ELEV')      # อ่านจาก pyramid แบบเฉลี่ย
    if name.startswith('SMOD'):
        return smod_img(int(name[4:])).reproject(GRID).rename(name)
    if name.startswith('GB'):
        return mean_agg(built_pct(int(name[2:]))).reproject(GRID).rename(name)
    if name.startswith('DENS'):
        return mean_agg(pop_density(int(name[4:]))).reproject(GRID).rename(name)
    raise KeyError(name)


def static_band(name, sv=None):
    """แบนด์ข้อมูลคงที่บนกริด: PLANT, WATERF, ELEV, GB<e>, DENS<e>, SMOD<e>
    v2 มีครบ / v1 มี OPEN(=PLANT), GB, DENS ส่วนที่เหลือคำนวณสด (เร็วพอ)"""
    sv = static_version() if sv is None else sv
    if sv == 2:
        return A2('static').select(name)
    if sv == 1 and (name == 'PLANT' or name.startswith('GB') or name.startswith('DENS')):
        return A1('static').select('OPEN' if name == 'PLANT' else name).rename(name)
    return live_static_band(name)


# ---------------------------------------------------------------- ชั้นข้อมูลบนกริด

def layers_key(year, season):
    """กุญแจแคชที่เปลี่ยนเมื่อแหล่งข้อมูล (asset/สด) เปลี่ยน"""
    _, _, _, cv = cover_info(year)
    return f'{lst_key(lst_info(year, season))}{cv}{static_version()}'


def layers(year, season, region):
    lst, li = lst_image(year, season)
    cover, method, cover_fast, dwy = cover_image(year, region)
    sv = static_version()
    epoch = pop_epoch_of(year)
    green = cover.select('TREES').add(cover.select('GRASS')).rename('GREEN')
    return {'lst': lst, 'source': li['source'], 'dw_year': dwy, 'pop_epoch': epoch,
            'cover': cover, 'cover_method': method, 'green': green,
            'dens': static_band(f'DENS{epoch}', sv).rename('DENS'),
            'plant': static_band('PLANT', sv).rename('PLANT'),
            'fast': bool(li['fast'] and cover_fast and sv > 0)}


def urgent_mask(L, temp=VERY_HOT, green_max=GREEN_LOW):
    return (L['lst'].gte(temp).And(L['dens'].gte(POP_DENSE))
            .And(L['green'].lt(green_max)))


def priority(L):
    """0 = ไม่เข้าเกณฑ์, 1 = เฝ้าระวัง, 2 = เร่งด่วน (ไม่มีที่ปลูกเพิ่ม), 3 = เร่งด่วน (มีที่ปลูกเพิ่ม)"""
    hot = L['lst'].gte(HOT).And(L['dens'].gte(POP_ANY))
    urgent = urgent_mask(L)
    return (hot.toInt()
            .where(urgent, 2)
            .where(urgent.And(L['plant'].gte(PLANT_MIN)), 3)
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


MEAN_COUNT = ee.Reducer.mean().combine(reducer2=ee.Reducer.count(), sharedInputs=True)
MEAN_SD_COUNT = (ee.Reducer.mean()
                 .combine(reducer2=ee.Reducer.stdDev(), sharedInputs=True)
                 .combine(reducer2=ee.Reducer.count(), sharedInputs=True))


def stat(d, band, out):
    """อ่านผล reduceRegion: ตัวลดหลายผลลัพธ์ตั้งชื่อเป็น '<band>_<out>'"""
    d = d or {}
    v = d.get(f'{band}_{out}')
    return d.get(out) if v is None else v


def as_int(v):
    return int(round(v)) if isinstance(v, (int, float)) else 0


def grouped_rows(groups, labels):
    found = {int(r['g']): r for r in (groups or []) if r.get('g') is not None}
    return [{'label': lab,
             'mean': found.get(i, {}).get('mean'),
             'sd': found.get(i, {}).get('stdDev'),
             'count': as_int(found.get(i, {}).get('count'))}
            for i, lab in enumerate(labels)]


# ---------------------------------------------------------------- คำนวณล่วงหน้า (export เป็น asset)

def export_region():
    return (ee.FeatureCollection('FAO/GAUL/2015/level0')
            .filter(ee.Filter.eq('ADM0_NAME', 'Thailand'))
            .geometry().simplify(2000).buffer(5000))


def build_static():
    bands = [plant_agg(30), water_agg(30)]
    for e in POP_EPOCHS:
        bands += [mean_agg(built_pct(e)).rename(f'GB{e}'),
                  mean_agg(pop_density(e)).rename(f'DENS{e}'),
                  smod_img(e).rename(f'SMOD{e}')]           # รหัสชั้น: ไม่เฉลี่ย (ใกล้สุด)
    bands.append(mean_agg(elev_img()).rename('ELEV'))
    return ee.Image.cat(bands).toFloat()


def build_lst(year):
    """LST_<s> = ค่าเฉลี่ยดิบของดาวเทียมปีนั้น, N_<s> = จำนวนวันที่มีค่า"""
    bands = []
    for s in SEASON_ORDER:
        mean, count = col_mean_count(year_col(year, s))
        bands += [mean.rename(f'LST_{s}'), count.rename(f'N_{s}')]
    return ee.Image.cat(bands).toFloat()


def build_vbias():
    """VB_<s> = ค่าเฉลี่ยส่วนต่าง VIIRS − Aqua แบบจับคู่วัน 2013-2020 (ต้องมีคู่ ≥ 10 วัน
    ช่องที่ไม่พอเติมด้วยค่าเฉลี่ยรอบ ๆ 10 กม.), NP_<s> = จำนวนคู่"""
    s0, s1 = f'{BIAS_YEARS[0]}-01-01', f'{BIAS_YEARS[1] + 1}-01-01'
    bands = []
    for s in SEASON_ORDER:
        d = paired_diffs(s0, s1, s)
        pairs = d.count().rename(f'NP_{s}')
        vb = d.mean().updateMask(pairs.gte(VB_MIN_PAIRS))
        # skipMasked=False: ให้คำนวณค่าเฉลี่ยรอบ ๆ ที่พิกเซลที่ตัวเองไม่มีค่าด้วย
        around = vb.reduceNeighborhood(reducer=ee.Reducer.mean(),
                                       kernel=ee.Kernel.circle(VB_FILL_M, 'meters'),
                                       skipMasked=False)
        bands += [vb.unmask(around).rename(f'VB_{s}'), pairs]
    return ee.Image.cat(bands).toFloat()


def build_vcheck():
    """D_<s> = ค่าเฉลี่ย ((VIIRS − VB_s) − Aqua) แบบจับคู่วัน 2021-2022 (ไม่ได้ใช้ตอนปรับเทียบ)"""
    s0, s1 = f'{CHECK_YEARS[0]}-01-01', f'{CHECK_YEARS[1] + 1}-01-01'
    vbias = A2('vbias')
    bands = []
    for s in SEASON_ORDER:
        d = paired_diffs(s0, s1, s, bias=vbias.select(f'VB_{s}'))
        bands += [d.mean().rename(f'D_{s}'), d.count().rename(f'N_{s}')]
    return ee.Image.cat(bands).toFloat()


def build_asset(name, region):
    if name == 'static':
        return build_static()
    if name == 'vbias':
        return build_vbias()
    if name == 'vcheck':
        return build_vcheck()
    kind, year = name.split('_')
    year = int(year)
    if kind == 'lst':
        return build_lst(year)
    if kind == 'dw':
        return dw_mode_fractions(year, region, scale=100).toFloat()
    raise ValueError(name)


_pre = {'last_run': 0, 'running': False, 'started': [], 'error': None,
        'failed': {}, 'gave_up': [], 'held': []}


def short(e, n=200):
    """ข้อความผิดพลาดแบบสั้น (ไว้ใน log / หน้า status)"""
    if e is None:
        return None
    s = e if isinstance(e, str) else f'{type(e).__name__}: {e}'
    return s if len(s) <= n else s[:n] + '…'
_pre_run_lock = threading.Lock()      # กันการรันซ้อนกัน
_pre_state_lock = threading.Lock()    # กันการตัดสินใจเริ่มรันพร้อมกันหลายคำขอ


def ee_tasks(prefix='uhi_', strict=False):
    """งาน export ของแอปนี้ (กรองตามคำนำหน้าชื่อ)
    strict=True: อ่านรายการงานไม่สำเร็จให้โยน error (กันสั่งงานซ้ำโดยไม่รู้ว่ามีงานค้างอยู่)"""
    out = []
    try:
        for op in ee.data.listOperations() or []:
            md = op.get('metadata') or {}
            out.append({'desc': md.get('description', ''), 'state': md.get('state', ''),
                        'error': short((op.get('error') or {}).get('message')),
                        'name': op.get('name'), 'create': md.get('createTime', ''),
                        'update': md.get('updateTime', '')})
    except Exception:
        out = []
        try:
            for t in ee.data.getTaskList() or []:
                out.append({'desc': t.get('description', ''), 'state': t.get('state', ''),
                            'error': short(t.get('error_message')), 'name': t.get('id'),
                            'create': str(t.get('creation_timestamp_ms', '')),
                            'update': str(t.get('update_timestamp_ms', ''))})
        except Exception as e:
            if strict:
                raise
            out.append({'desc': prefix, 'state': 'LIST_ERROR', 'error': short(e),
                        'name': None, 'create': '', 'update': ''})
    return [t for t in out if (t['desc'] or '').startswith(prefix)]


def ensure_folder(folder):
    try:
        ee.data.getAsset(folder)
    except Exception:
        try:
            ee.data.createFolder(folder)
        except AttributeError:
            ee.data.createAsset({'type': 'FOLDER'}, folder)


def start_export(name, region):
    policy = {'.default': 'mean'}
    if name == 'static':
        policy.update({f'SMOD{e}': 'mode' for e in POP_EPOCHS})
    image = build_asset(name, region).set('uhi_code_version', CODE_VERSION)
    task = ee.batch.Export.image.toAsset(
        image=image, description=TASK_PREFIX + name,
        assetId=f'{ASSET_V2}/{name}', region=region,
        crs=GRID_CRS, crsTransform=GRID_TRANSFORM, maxPixels=1e10,
        pyramidingPolicy=policy)
    task.start()


def ensure_precompute():
    """สั่ง export ชั้นข้อมูลที่ยังไม่มี: ข้ามชื่อที่มีแล้ว/กำลังทำ, งานที่ล้มเหลวสั่งใหม่ได้ไม่เกิน 2 ครั้ง
    (ไม่ยกเลิกหรือเรียงคิวใหม่)"""
    if not _pre_run_lock.acquire(blocking=False):
        return
    _pre['running'] = True
    try:
        refresh_years()
        # ถ้ายังยืนยันไม่ได้ว่าปีล่าสุดข้อมูลครบ ห้าม export ปีนั้นในรอบนี้ (กันได้ asset ขาดข้อมูลถาวร)
        last = last_year()
        hold = set() if _years['verified'] else {f'lst_{last}', f'dw_{last}'}
        ensure_folder(ASSET_V2)
        have = existing_assets(ASSET_V2, force=True, strict=True)
        tasks = ee_tasks(TASK_PREFIX, strict=True)
        active = {t['desc'] for t in tasks if t['state'] in ACTIVE_STATES}
        failed = {}
        for t in sorted(tasks, key=lambda t: t['create'] or ''):
            if t['state'] == 'FAILED':
                f = failed.setdefault(t['desc'][len(TASK_PREFIX):], {'count': 0, 'error': None})
                f['count'] += 1
                f['error'] = t['error']
        region = None
        gave_up = []
        for name in needed_assets():
            if name in have or TASK_PREFIX + name in active or name in hold:
                continue
            if name == 'vcheck' and 'vbias' not in have:
                continue                                 # ต้องใช้ผลปรับเทียบที่เสร็จแล้ว
            if failed.get(name, {}).get('count', 0) > MAX_RETRIES:
                gave_up.append(name)
                continue
            try:
                if region is None:
                    region = export_region()
                start_export(name, region)
                _pre['started'] = (_pre['started'] + [name])[-100:]
                print('[precompute] started', name, flush=True)
            except Exception as e:                       # สร้างงานไม่สำเร็จ: ลองใหม่รอบหน้า
                failed.setdefault(name, {'count': 0, 'error': None})['error'] = short(e)
                print('[precompute] cannot start', name, short(e), flush=True)
        _pre['failed'] = {n: f for n, f in failed.items() if n not in have}
        _pre['gave_up'] = gave_up
        _pre['held'] = sorted(hold)
        _pre['error'] = None
    except Exception as e:
        _pre['error'] = short(e)
        print('[precompute] error', short(e), flush=True)
    finally:
        _pre['running'] = False
        _pre_run_lock.release()


def maybe_precompute():
    if not PRECOMPUTE:
        return
    with _pre_state_lock:
        if _pre['running'] or time.time() - _pre['last_run'] < PRECOMPUTE_EVERY_S:
            return
        _pre['last_run'] = time.time()
    threading.Thread(target=ensure_precompute, daemon=True).start()


# ---------------------------------------------------------------- พื้นที่โฟกัส (ตรวจข้อมูลก่อนคำนวณเสมอ)

TH_BBOX = (97.3, 5.6, 105.7, 20.5)    # ตะวันตก ใต้ ตะวันออก เหนือ
MAX_VERTICES = 5000                   # จำนวนจุดสูงสุดของรูปที่วาด
TOO_LARGE_MSG = 'ข้อมูลที่ส่งมาใหญ่เกินไป (เช่น รูปที่วาดมีจุดมากเกินไป) ลองวาดให้ง่ายขึ้น'


def provinces_fc():
    return (ee.FeatureCollection('FAO/GAUL/2015/level1')
            .filter(ee.Filter.eq('ADM0_NAME', 'Thailand')))


def get_province_names():
    global _province_names
    if _province_names is None:
        _province_names = (provinces_fc().aggregate_array('ADM1_NAME')
                           .sort().getInfo())
    return _province_names


def province_geom(name):
    if not isinstance(name, str) or name not in get_province_names():
        raise BadInput('ไม่พบจังหวัดนี้')
    return provinces_fc().filter(ee.Filter.eq('ADM1_NAME', name)).geometry()


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def check_polygon_coords(gtype, coords):
    """ตรวจพิกัด Polygon/MultiPolygon: เป็นตัวเลขจริง อยู่ในช่วงพิกัดโลก และจำนวนจุดไม่เกินกำหนด"""
    bad = BadInput('รูปที่วาดไม่ถูกต้อง ลองวาดใหม่อีกครั้ง')
    polys = [coords] if gtype == 'Polygon' else coords
    if not isinstance(polys, list) or not polys:
        raise bad
    n = 0
    for poly in polys:
        if not isinstance(poly, list) or not poly:
            raise bad
        for ring in poly:
            if not isinstance(ring, list) or len(ring) < 3:
                raise bad
            for pt in ring:
                if (not isinstance(pt, list) or len(pt) < 2 or not _is_num(pt[0])
                        or not _is_num(pt[1]) or not -180 <= pt[0] <= 180
                        or not -90 <= pt[1] <= 90):
                    raise bad
                n += 1
                if n > MAX_VERTICES:
                    raise BadInput('รูปที่วาดมีจุดมากเกินไป ลองวาดให้ง่ายขึ้น')


def read_region(data):
    """ตรวจและจัดรูปพื้นที่ที่ส่งมา -> dict ที่ใช้ทั้งสร้างพื้นที่และเป็นกุญแจแคช
    จังหวัด / รูปที่วาด / จุด (ปัดพิกัดจุดเป็นทศนิยม 3 ตำแหน่ง ≈ 100 ม.)"""
    name = data.get('province')
    if name:
        if not isinstance(name, str) or name not in get_province_names():
            raise BadInput('ไม่พบจังหวัดนี้')
        return {'province': name}
    g = data.get('geometry')
    if g:
        if not isinstance(g, dict) or g.get('type') not in ('Polygon', 'MultiPolygon'):
            raise BadInput('รูปที่วาดไม่ถูกต้อง ลองวาดใหม่อีกครั้ง')
        check_polygon_coords(g['type'], g.get('coordinates'))
        return {'geometry': {'type': g['type'], 'coordinates': g['coordinates']}}
    lat, lon = data.get('lat'), data.get('lon')
    if lat is None or lon is None:
        raise BadInput('กรุณาเลือกพื้นที่ก่อน (จังหวัด วาดรูป หรือคลิกจุดบนแผนที่)')
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        raise BadInput('ตำแหน่งที่เลือกไม่ถูกต้อง')
    if not (math.isfinite(lat) and math.isfinite(lon)
            and TH_BBOX[1] <= lat <= TH_BBOX[3] and TH_BBOX[0] <= lon <= TH_BBOX[2]):
        raise BadInput('ตำแหน่งที่เลือกอยู่นอกประเทศไทย ลองคลิกจุดในประเทศไทย')
    return {'lat': round(lat, 3), 'lon': round(lon, 3)}


def region_key(spec):
    raw = json.dumps(spec, sort_keys=True, separators=(',', ':'))
    if 'geometry' in spec:                       # รูปที่วาดอาจยาว: ใช้ค่า hash แทน
        return 'g:' + hashlib.sha1(raw.encode('utf-8')).hexdigest()
    return raw


def build_region(spec):
    """สร้าง ee.Geometry จากพื้นที่ที่ตรวจแล้ว (รูปที่วาดต้องไม่ใหญ่เกิน MAX_AREA_KM2)"""
    if 'province' in spec:
        return province_geom(spec['province'])
    if 'geometry' in spec:
        try:
            geom = ee.Geometry(spec['geometry'])
        except Exception:
            raise BadInput('รูปที่วาดไม่ถูกต้อง ลองวาดใหม่อีกครั้ง')
        area = cached('area|' + region_key(spec),
                      lambda: geom.area(maxError=100).divide(1e6).getInfo(), ttl=24 * 3600)
        area = area or 0
        if area > MAX_AREA_KM2:
            raise BadInput(f'พื้นที่ที่วาดใหญ่เกินไป ({area:,.0f} ตร.กม.) '
                           f'วาดให้ไม่เกิน {MAX_AREA_KM2:,} ตร.กม. หรือเลือกเป็นจังหวัดแทน')
        return geom
    return ee.Geometry.Point([spec['lon'], spec['lat']]).buffer(POINT_RADIUS_M)


def read_json():
    try:
        data = request.get_json(force=True, silent=True)
    except RequestEntityTooLarge:
        raise BadInput(TOO_LARGE_MSG, 413)
    if not isinstance(data, dict):
        raise BadInput('ข้อมูลที่ส่งมาไม่ถูกต้อง ลองใหม่อีกครั้ง')
    return data


def read_year(d):
    try:
        year = int(d.get('year', DEFAULT_YEAR))
    except (TypeError, ValueError):
        raise BadInput('ปีไม่ถูกต้อง')
    if not FIRST_YEAR <= year <= last_year():
        raise BadInput('ปีไม่ถูกต้อง')
    return year


def read_season(d):
    season = d.get('season') or 'all'
    if not isinstance(season, str) or season not in SEASONS:
        raise BadInput('ฤดูไม่ถูกต้อง')
    return season


def error_response(e):
    """ข้อความผิดพลาดภาษาไทยที่เป็นมิตร (รายละเอียดทางเทคนิคเก็บไว้ใน log เท่านั้น)"""
    if isinstance(e, BadInput):
        return jsonify({'error': str(e)}), e.status
    if isinstance(e, RequestEntityTooLarge):
        return jsonify({'error': TOO_LARGE_MSG}), 413
    kind = error_kind(e)
    if kind is None:
        # ข้อผิดพลาดที่ไม่คาดคิด (รวมบั๊กในโค้ด): เก็บ traceback ไว้ใน log
        traceback.print_exception(type(e), e, e.__traceback__)
    else:
        print('[EE]', kind, short(e), flush=True)
    if kind == 'busy':
        return jsonify({'error': 'ขณะนี้มีผู้ใช้งานพร้อมกันมาก กรุณารอสักครู่แล้วลองใหม่'}), 503
    if kind == 'heavy':
        return jsonify({'error': 'พื้นที่นี้คำนวณหนักเกินไปในตอนนี้ ลองเลือกพื้นที่ที่เล็กลง '
                                 'หรือรอสักครู่แล้วลองใหม่ (ระบบกำลังเตรียมข้อมูลล่วงหน้า '
                                 'เมื่อเสร็จจะเร็วขึ้นมาก)'}), 503
    return jsonify({'error': 'ระบบขัดข้องชั่วคราว ดึงข้อมูลไม่สำเร็จ กรุณาลองใหม่อีกครั้ง'}), 500


@app.errorhandler(RequestEntityTooLarge)
def too_large(e):
    return jsonify({'error': TOO_LARGE_MSG}), 413


# ---------------------------------------------------------------- จำกัดความถี่คำขอ

# ต่อ IP สำหรับคำขอที่ต้องคำนวณ (POST ทั้งหมด และ /api/tiles) - ตั้งไว้สูงพอให้ผู้ชมหลายคน
# ที่ใช้ Wi-Fi เดียวกันในงานประกวด (IP เดียวกัน) ใช้งานพร้อมกันได้ แต่ยังกันการยิงรัว ๆ
RATE_LIMIT_PER_MIN = 300
_rate = {}                       # ip -> deque เวลาของคำขอใน 60 วินาทีล่าสุด
_rate_lock = threading.Lock()


def client_ip():
    # Render อยู่หลัง proxy: IP แรกใน X-Forwarded-For คือผู้ใช้
    fwd = request.headers.get('X-Forwarded-For', '')
    ip = fwd.split(',')[0].strip() if fwd else ''
    return ip or request.remote_addr or '?'


@app.before_request
def rate_limit():
    if not request.path.startswith('/api/'):
        return None
    if request.method != 'POST' and request.path != '/api/tiles':
        return None
    now = time.time()
    ip = client_ip()
    with _rate_lock:
        if ip not in _rate and len(_rate) > 5000:            # ล้าง IP ที่ไม่ได้ใช้แล้ว
            for k in [k for k, q in _rate.items() if not q or q[-1] < now - 60]:
                del _rate[k]
        q = _rate.setdefault(ip, deque())
        while q and q[0] <= now - 60:
            q.popleft()
        if len(q) >= RATE_LIMIT_PER_MIN:
            wait = max(1, int(q[0] + 60 - now) + 1)
        else:
            q.append(now)
            wait = 0
    if wait:
        resp = jsonify({'error': 'มีการเรียกข้อมูลถี่เกินไป กรุณารอประมาณ 1 นาทีแล้วลองใหม่'})
        resp.status_code = 429
        resp.headers['Retry-After'] = str(wait)
        return resp
    return None


# ---------------------------------------------------------------- หน้าเว็บ / API

@app.route('/')
def index():
    maybe_precompute()
    return render_template('index.html')


@app.route('/api/config')
def config():
    try:
        maybe_precompute()
        return jsonify({
            'version': 5,
            'first_year': FIRST_YEAR, 'last_year': last_year(), 'default_year': DEFAULT_YEAR,
            'aqua_last_year': AQUA_LAST_YEAR, 'dw_first_year': DW_FIRST_YEAR,
            'bias_years': list(BIAS_YEARS), 'check_years': list(CHECK_YEARS),
            'lst_breaks': LST_BREAKS, 'lst_palette': LST_PALETTE,
            'hot': HOT, 'very_hot': VERY_HOT,
            'hot_level': LST_BREAKS.index(VERY_HOT) + 1,
            'pop_any': POP_ANY, 'pop_dense': POP_DENSE,
            'green_low': GREEN_LOW, 'plant_min': PLANT_MIN, 'n_min': N_MIN,
            'prio_colors': PRIO_COLORS,
            'seasons': SEASONS, 'season_order': SEASON_ORDER,
            'point_radius_m': POINT_RADIUS_M, 'max_area_km2': MAX_AREA_KM2,
            'built_labels': BUILT_LABELS, 'green_labels': GREEN_LABELS,
            'change_labels': CHANGE_LABELS,
            'sens_temps': SENS_TEMPS, 'sens_greens': SENS_GREENS,
            'compare_cities': [{'en': n, 'th': PROVINCE_TH.get(n, n)} for n in COMPARE_CITIES],
            'suhi': {'urban_code': SUHI_URBAN_CODE, 'rural_codes': SUHI_RURAL_CODES,
                     'ring_km': [SUHI_RING_M[0] // 1000, SUHI_RING_M[1] // 1000],
                     'elev_tol_m': SUHI_ELEV_TOL},
            'precompute': precompute_progress(),
        })
    except Exception as e:
        return error_response(e)


@app.route('/api/status')
def status():
    """สถานะการคำนวณล่วงหน้า (ไว้ตรวจสอบ ไม่ได้ใช้ในหน้าเว็บ) - แคช 60 วินาที"""
    def calc():
        need = needed_assets()
        have2 = existing_assets(ASSET_V2, force=True)
        have1 = existing_assets(ASSET_V1, force=True)
        tasks = sorted(ee_tasks('uhi_'), key=lambda t: t['create'] or '', reverse=True)
        return {
            'enabled': PRECOMPUTE, 'code_version': CODE_VERSION,
            'years': {'last': _years['last'], 'verified': _years['verified']},
            'folders': {'v1': ASSET_V1, 'v2': ASSET_V2},
            'v2': {'ready': [n for n in need if n in have2],
                   'missing': [n for n in need if n not in have2]},
            'v1': {'ready': sorted(have1)},
            'progress': precompute_progress(),
            'active_tasks': [t for t in tasks if t['state'] in ACTIVE_STATES][:40],
            'recent_tasks': tasks[:30],
            'failures': _pre['failed'], 'gave_up': _pre['gave_up'], 'held': _pre['held'],
            'precompute': {'last_run': _pre['last_run'], 'running': _pre['running'],
                           'started': _pre['started'][-40:], 'error': _pre['error']},
        }
    try:
        return jsonify(cached('status', calc, ttl=60))
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


TILE_LAYERS = ('priority', 'lst', 'green', 'plant', 'built', 'built_change')


@app.route('/api/tiles')
def tiles():
    """URL ของชั้นแผนที่ทีละชั้น (โหลดเฉพาะชั้นที่เปิดดู)"""
    try:
        layer = request.args.get('layer', 'priority')
        if layer not in TILE_LAYERS:
            raise BadInput('ไม่รู้จักชั้นข้อมูลนี้')
        year = read_year(request.args)
        season = read_season(request.args)

        def calc():
            out = {'layer': layer, 'year': year, 'season': season}
            if layer == 'lst':
                lst, li = lst_image(year, season)
                img = lst_level(lst)
                vis = {'min': 0, 'max': len(LST_BREAKS), 'palette': LST_PALETTE}
                out.update(source=li['source'], fast=li['fast'], min_zoom=5)
            elif layer == 'priority':
                L = layers(year, season, THAILAND)
                img = priority(L).selfMask()
                vis = {'min': 1, 'max': 3, 'palette': PRIO_COLORS}
                out.update(source=L['source'], fast=L['fast'], dw_year=L['dw_year'],
                           pop_epoch=L['pop_epoch'], cover_method=L['cover_method'],
                           min_zoom=5 if L['fast'] else 8)
            elif layer in ('green', 'built'):
                # ความละเอียดเต็ม 10 ม.: ประเภทที่พบบ่อยที่สุดของปี
                dwy = dw_year_of(year)
                out.update(dw_year=dwy, cover_method='mode', fast=True, min_zoom=9)
                lab = dw_mode_label(dwy, THAILAND)
                if layer == 'green':
                    trees = lab.eq(1)
                    grass = lab.eq(2).Or(lab.eq(5))
                    img = trees.add(grass.multiply(2)).selfMask()    # 1 ต้นไม้, 2 หญ้า/พุ่มไม้
                    vis = {'min': 1, 'max': 2, 'palette': ['#1b7837', '#a6dba0']}
                else:
                    img = lab.eq(6).selfMask()
                    vis = {'min': 0, 'max': 1, 'palette': ['#6a51a3', '#6a51a3']}
            elif layer == 'plant':
                epoch = pop_epoch_of(year)
                sv = static_version()
                dens = static_band(f'DENS{epoch}', sv)
                img = worldcover_plant().selfMask().updateMask(dens.gte(POP_ANY))
                vis = {'min': 0, 'max': 1, 'palette': ['#c8873a', '#c8873a']}
                out.update(pop_epoch=epoch, fast=sv > 0, min_zoom=10)
            else:  # built_change
                d = built_pct(2020).subtract(built_pct(2005))
                img = d.updateMask(d.gt(2))
                vis = {'min': 2, 'max': 40,
                       'palette': ['#fde0ef', '#f1b6da', '#de77ae', '#c51b7d', '#8e0152']}
                out.update(fast=True, min_zoom=6)
            out['url'] = img.getMapId(vis)['tile_fetcher'].url_format
            return out

        if layer == 'lst':
            flags = lst_key(lst_info(year, season))
        elif layer == 'priority':
            flags = layers_key(year, season)
        elif layer == 'plant':
            flags = str(static_version())
        else:
            flags = ''
        return jsonify(cached(f'tiles|{layer}|{year}|{season}|{flags}', calc, ttl=3600))
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


def sens_key(t, g):
    return f'{t}_{int(round(g * 100))}'


@app.route('/api/summary', methods=['POST'])
def summary():
    """สรุปพื้นที่: พื้นผิว, ระดับความเร่งด่วน, พื้นที่ปลูกต้นไม้เพิ่มได้, คนกับความร้อน, ความไวต่อเกณฑ์"""
    try:
        data = read_json()
        year, season = read_year(data), read_season(data)
        spec = read_region(data)
        region = build_region(spec)

        def calc():
            L = layers(year, season, region)
            km2 = ee.Image.pixelArea().divide(1e6)
            people = L['dens'].multiply(km2).rename('PEOPLE')
            plant_km2 = L['plant'].multiply(km2).rename('PLANTKM2')
            cls = priority(L)
            lived = L['dens'].gte(POP_ANY)

            # กลุ่มตามระดับความเร่งด่วน: ผลรวม คน / ที่ปลูกได้ / พื้นที่
            prio_reducer = (ee.Reducer.sum()
                            .combine(reducer2=ee.Reducer.sum(), outputPrefix='plant_')
                            .combine(reducer2=ee.Reducer.sum(), outputPrefix='area_')
                            .group(groupField=3, groupName='g'))
            prio_stack = (people.addBands(plant_km2)
                          .addBands(km2.rename('AREA')).addBands(cls))
            expo_stack = people.addBands(lst_level(L['lst']).toInt().rename('g'))

            # ความไวต่อเกณฑ์: พื้นที่และคนในย่านเร่งด่วน ทุกคู่อุณหภูมิ x พื้นที่สีเขียว (รวมใน reduce เดียว)
            sum_bands = [people]
            for t in SENS_TEMPS:
                for g in SENS_GREENS:
                    u = urgent_mask(L, t, g)
                    k = sens_key(t, g)
                    sum_bands += [km2.updateMask(u).rename('A_' + k),
                                  people.updateMask(u).rename('P_' + k)]

            res = ee.Dictionary({
                'prio': rr(prio_stack, prio_reducer, region).get('groups'),
                'expo': rr(expo_stack, ee.Reducer.sum().group(groupField=1, groupName='g'),
                           region).get('groups'),
                'cover_all': rr(L['cover'], ee.Reducer.mean(), region),
                'cover_lived': rr(L['cover'].updateMask(lived), ee.Reducer.mean(), region),
                'sums': rr(ee.Image.cat(sum_bands), ee.Reducer.sum(), region),
                'lst_lived': rr(L['lst'].updateMask(lived), ee.Reducer.mean(),
                                region).get('LST'),
                'area_km2': region.area(maxError=100).divide(1e6),
            }).getInfo()

            found = {int(r['g']): r for r in (res.get('prio') or []) if r.get('g') is not None}
            prio = [{'cls': c,
                     'people': found.get(c, {}).get('sum') or 0,
                     'plant_km2': found.get(c, {}).get('plant_sum') or 0,
                     'area_km2': found.get(c, {}).get('area_sum') or 0}
                    for c in range(4)]
            efound = {int(r['g']): r.get('sum') or 0
                      for r in (res.get('expo') or []) if r.get('g') is not None}
            expo = [{'level': i, 'people': efound.get(i, 0)}
                    for i in range(len(LST_BREAKS) + 1)]
            sums = res.get('sums') or {}
            sensitivity = [{'t': t, 'g': g,
                            'area_km2': sums.get('A_' + sens_key(t, g)) or 0,
                            'people': sums.get('P_' + sens_key(t, g)) or 0}
                           for t in SENS_TEMPS for g in SENS_GREENS]
            return {
                'year': year, 'season': season, 'source': L['source'],
                'dw_year': L['dw_year'], 'pop_epoch': L['pop_epoch'], 'fast': L['fast'],
                'cover_method': L['cover_method'],
                'area_km2': res.get('area_km2'),
                'lst_lived': res.get('lst_lived'),
                'cover_all': res.get('cover_all') or {},
                'cover_lived': res.get('cover_lived') or {},
                'pop_total': sums.get('PEOPLE') or 0,
                'prio': prio, 'expo': expo, 'sensitivity': sensitivity,
            }

        key = f'summary|{region_key(spec)}|{year}|{season}|{layers_key(year, season)}'
        return jsonify(cached(key, calc))
    except Exception as e:
        return error_response(e)


@app.route('/api/bins', methods=['POST'])
def bins():
    """อุณหภูมิในย่านที่มีคนอยู่ แยกตามสัดส่วนสิ่งปลูกสร้าง และสัดส่วนพื้นที่สีเขียว"""
    try:
        data = read_json()
        year, season = read_year(data), read_season(data)
        spec = read_region(data)
        region = build_region(spec)

        def calc():
            L = layers(year, season, region)
            lst = L['lst'].updateMask(L['dens'].gte(POP_ANY))
            reducer = MEAN_SD_COUNT.group(groupField=1, groupName='g')
            built_idx = bin_index(L['cover'].select('BUILT'), BUILT_EDGES).rename('g')
            green_idx = bin_index(L['green'], GREEN_EDGES).rename('g')
            res = ee.Dictionary({
                'built': rr(lst.addBands(built_idx), reducer, region).get('groups'),
                'green': rr(lst.addBands(green_idx), reducer, region).get('groups'),
            }).getInfo()
            return {'year': year, 'season': season, 'source': L['source'],
                    'dw_year': L['dw_year'], 'fast': L['fast'],
                    'cover_method': L['cover_method'],
                    'built': grouped_rows(res.get('built'), BUILT_LABELS),
                    'green': grouped_rows(res.get('green'), GREEN_LABELS)}

        key = f'bins|{region_key(spec)}|{year}|{season}|{layers_key(year, season)}'
        return jsonify(cached(key, calc))
    except Exception as e:
        return error_response(e)


def busy_history_response():
    return jsonify({'error': 'ข้อมูลย้อนหลังกำลังเตรียมอยู่ ลองอีกครั้งภายหลัง'}), 503


@app.route('/api/trend', methods=['POST'])
def trend():
    """LST รายปี (ย่านที่มีคนอยู่ตาม GHSL 2020) และสัดส่วนอาคาร GHSL ทุก 5 ปี ในช่องเดียวกัน"""
    try:
        data = read_json()
        season = read_season(data)
        spec = read_region(data)
        region = build_region(spec)
        years = all_years()
        infos = {y: lst_info(y, season) for y in years}
        live_years = sum(1 for i in infos.values() if not i['fast'])
        if live_years > MAX_LIVE_TREND_YEARS:
            return busy_history_response()
        sv = static_version()
        fast = live_years == 0 and sv > 0

        def calc():
            lived = static_band('DENS2020', sv).gte(POP_ANY)
            out = {}
            for y in years:
                lst, _ = lst_image(y, season)
                out[f'y{y}'] = rr(lst.updateMask(lived), MEAN_SD_COUNT, region)
            gb = ee.Image.cat([static_band(f'GB{e}', sv).rename(f'GB{e}') for e in POP_EPOCHS])
            out['built'] = rr(gb.updateMask(lived), ee.Reducer.mean(), region)
            res = ee.Dictionary(out).getInfo()
            rows = []
            for y in years:
                r = res.get(f'y{y}') or {}
                rows.append({'year': y, 'lst': stat(r, 'LST', 'mean'),
                             'sd': stat(r, 'LST', 'stdDev'),
                             'count': as_int(stat(r, 'LST', 'count')),
                             'source': infos[y]['source']})
            b = res.get('built') or {}
            built = [{'epoch': e, 'built': b.get(f'GB{e}')} for e in POP_EPOCHS]
            return {'season': season, 'fast': fast, 'rows': rows, 'built': built}

        flags = ''.join(lst_key(infos[y]) for y in years) + str(sv)
        return jsonify(cached(f'trend|{region_key(spec)}|{season}|{flags}', calc))
    except Exception as e:
        return error_response(e)


def lst_block(y0, y1, season):
    """LST เฉลี่ยของช่วงปี = ค่าเฉลี่ยของ LST รายปี (ใช้กับปี Aqua เท่านั้น)"""
    imgs = [lst_image(y, season)[0] for y in range(y0, y1 + 1)]
    return ee.ImageCollection.fromImages(imgs).mean().rename('LST')


@app.route('/api/change', methods=['POST'])
def change():
    """อุณหภูมิที่เปลี่ยนไป (2018-22 เทียบ 2003-07) แยกตามระดับที่อาคารเพิ่ม (2005 -> 2020)"""
    try:
        data = read_json()
        season = read_season(data)
        spec = read_region(data)
        region = build_region(spec)
        (a0, a1), (b0, b1) = CHANGE_BLOCKS
        years = list(range(a0, a1 + 1)) + list(range(b0, b1 + 1))
        infos = [lst_info(y, season) for y in years]
        sv = static_version()
        fast = all(i['fast'] for i in infos) and sv > 0

        def calc():
            d_lst = (lst_block(b0, b1, season)
                     .subtract(lst_block(a0, a1, season)).rename('DLST'))
            b05, b20 = static_band('GB2005', sv), static_band('GB2020', sv)
            d_b = b20.subtract(b05)
            # ช่องที่อยู่ใกล้ย่านที่อาคารเพิ่มเกิน 4 จุด (ภายใน 2 กม.) ไม่ใช้เป็นกลุ่มเทียบ
            near = d_b.gt(4).focalMax(CONTROL_GAP_M, 'circle', 'meters')
            control = d_b.lte(1).And(b05.lt(5)).And(near.Not())
            cls = ee.Image(-1)
            cls = cls.where(control, 0)                         # กลุ่มเทียบ
            cls = cls.where(d_b.gt(1).And(d_b.lte(4)), 1)
            cls = cls.where(d_b.gt(4).And(d_b.lte(8)), 2)
            cls = cls.where(d_b.gt(8), 3)
            cls = cls.where(d_b.lte(1).And(b05.gte(25)), 4)    # เมืองหนาแน่นอยู่แล้ว
            cls = cls.updateMask(cls.gte(0)).rename('g').toInt()
            res = ee.Dictionary({
                'groups': rr(d_lst.addBands(cls), MEAN_SD_COUNT.group(groupField=1, groupName='g'),
                             region).get('groups'),
            }).getInfo()
            return {'season': season, 'fast': fast,
                    'rows': grouped_rows(res.get('groups'), CHANGE_LABELS)}

        flags = ''.join(lst_key(i) for i in infos) + str(sv)
        return jsonify(cached(f'change|{region_key(spec)}|{season}|{flags}', calc))
    except Exception as e:
        return error_response(e)


def suhi_urban_def(name, province, sv):
    """'centre' ถ้าจังหวัดมี urban centre (GHSL 2020) ตั้งแต่ 3 ช่อง ไม่งั้น 'cluster' (ตรวจครั้งเดียว)"""
    def calc():
        urban = static_band('SMOD2020', sv).eq(SUHI_URBAN_CODE).selfMask().rename('U')
        return rr(urban, ee.Reducer.count(), province).get('U').getInfo()
    n = cached(f'urbandef|{name}|{sv}', calc, ttl=7 * 24 * 3600)
    return 'centre' if (n or 0) >= SUHI_MIN_URBAN_CELLS else 'cluster'


def suhi_calc(name, season, sv, years):
    province = province_geom(name)
    urban_def = suhi_urban_def(name, province, sv)
    ring_region = province.simplify(1000).buffer(SUHI_SEARCH_M, 1000)

    def urban_of(smod):
        if urban_def == 'centre':
            return smod.eq(SUHI_URBAN_CODE)
        return smod.gte(SUHI_CLUSTER_MIN)

    elev = static_band('ELEV', sv).rename('ELEV')
    not_water = static_band('WATERF', sv).lt(SUHI_WATER_MAX)
    # ความสูงเฉลี่ยของเขตเมือง (GHSL 2020) ในจังหวัด - คำนวณบนเซิร์ฟเวอร์ ไม่ต้อง getInfo เพิ่ม
    em = rr(elev.updateMask(urban_of(static_band('SMOD2020', sv))),
            ee.Reducer.mean(), province).get('ELEV')
    urban_elev = ee.Number(ee.Algorithms.If(ee.Algorithms.IsEqual(em, None), -9999, em))
    similar_height = elev.subtract(urban_elev).abs().lte(SUHI_ELEV_TOL)

    masks = {}
    for e in POP_EPOCHS:
        smod = static_band(f'SMOD{e}', sv)
        urban = urban_of(smod)
        # ระยะถึงช่องเมืองที่ใกล้ที่สุด: Image.distance กับ ee.Kernel.euclidean(…, 'meters')
        # ให้ค่าเป็นเมตร (0 ในเขตเมือง, ไม่มีค่าถ้าไกลเกินรัศมี 25 กม.)
        # ใช้ unmask(0) ไม่ใช้ selfMask: distance ปิดค่าที่พิกเซลซึ่งข้อมูลเข้าถูกปิด (skipMasked)
        dist = urban.unmask(0).distance(ee.Kernel.euclidean(SUHI_SEARCH_M, 'meters'))
        rural = (smod.gte(min(SUHI_RURAL_CODES)).And(smod.lte(max(SUHI_RURAL_CODES)))
                 .And(dist.gt(SUHI_RING_M[0])).And(dist.lte(SUHI_RING_M[1]))
                 .And(not_water).And(similar_height))
        masks[e] = (urban, rural)

    out = {}
    for y in years:
        lst, _ = lst_image(y, season)
        urban, rural = masks[pop_epoch_of(y)]
        out[f'u{y}'] = rr(lst.updateMask(urban), MEAN_COUNT, province)
        out[f'r{y}'] = rr(lst.updateMask(rural), MEAN_COUNT, ring_region)
    res = ee.Dictionary(out).getInfo()

    rows = []
    for y in years:
        u, r = res.get(f'u{y}') or {}, res.get(f'r{y}') or {}
        um, rm = stat(u, 'LST', 'mean'), stat(r, 'LST', 'mean')
        nu, nr = as_int(stat(u, 'LST', 'count')), as_int(stat(r, 'LST', 'count'))
        ok = um is not None and rm is not None and nr >= SUHI_MIN_RURAL
        rows.append({'year': y, 'source': source_of(y), 'urban': um, 'rural': rm,
                     'suhii': (um - rm) if ok else None, 'n_urban': nu, 'n_rural': nr})
    return {'ready': True, 'province': name, 'season': season,
            'urban_def': urban_def, 'rows': rows}


@app.route('/api/suhi', methods=['POST'])
def suhi():
    """ความร้อนของเมืองเทียบชนบทรอบ ๆ (SUHII) รายปี ของจังหวัดที่เลือก (ใช้เทียบ 2 เมือง)"""
    try:
        data = read_json()
        season = read_season(data)
        name = data.get('province')
        if not isinstance(name, str) or name not in get_province_names():
            raise BadInput('ไม่พบจังหวัดนี้')
        years = all_years()
        infos = [lst_info(y, season) for y in years]
        sv = static_version()
        if sv == 0 or not all(i['fast'] for i in infos):
            return jsonify({'ready': False, 'progress': precompute_progress()})
        flags = ''.join(lst_key(i) for i in infos) + str(sv)
        return jsonify(cached(f'suhi|{name}|{season}|{flags}',
                              lambda: suhi_calc(name, season, sv, years)))
    except Exception as e:
        return error_response(e)


@app.route('/api/validate', methods=['POST'])
def validate():
    """ความคลาดเคลื่อนของ VIIRS ที่ปรับเทียบแล้วเทียบกับ Aqua (2021-22) ในย่านที่มีคนอยู่"""
    try:
        data = read_json()
        season = read_season(data)
        spec = read_region(data)
        region = build_region(spec)
        if 'vcheck' in existing_assets(ASSET_V2):
            v = 2
        elif season == 'all' and 'vcheck' in existing_assets(ASSET_V1):
            v = 1
        else:
            return jsonify({'ready': False})
        sv = static_version()

        def calc():
            if v == 2:
                vc = A2('vcheck')
                d = vc.select(f'D_{season}').updateMask(vc.select(f'N_{season}').gte(N_MIN))
            else:
                d = A1('vcheck').select('D_all')
            d = d.rename('D').updateMask(static_band('DENS2020', sv).gte(POP_ANY))
            stats = rr(d.addBands(d.abs().rename('ABS')), MEAN_COUNT, region).getInfo() or {}
            return {'ready': True, 'season': season, 'years': list(CHECK_YEARS),
                    # v2 ปรับเทียบจากวันที่วัดตรงกัน (paired) / v1 ชุดเดิมจากค่าเฉลี่ยช่วงที่ทำงานพร้อมกัน
                    'method': 'paired' if v == 2 else 'overlap',
                    'bias_years': list(BIAS_YEARS), 'mean': stats.get('D_mean'),
                    'mae': stats.get('ABS_mean'), 'count': as_int(stats.get('D_count'))}
        return jsonify(cached(f'validate|{region_key(spec)}|{season}|{v}{sv}', calc))
    except Exception as e:
        return error_response(e)


maybe_precompute()

if __name__ == '__main__':
    app.run(debug=True)
