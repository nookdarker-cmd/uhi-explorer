# แผนที่ความร้อนเมือง · Urban Heat Explorer

เว็บแอปสำหรับโครงงาน YSC ศึกษาความสัมพันธ์ระหว่างสิ่งปลูกสร้างกับอุณหภูมิพื้นผิว และหาย่านที่ควรปรับปรุงเพื่อลดความร้อนให้ผู้อยู่อาศัย โดยใช้ข้อมูลดาวเทียมผ่าน Google Earth Engine

- **ย่านที่ควรปรับปรุง:** ร้อนมาก + คนอยู่หนาแน่น + พื้นที่สีเขียวน้อย และดูว่ามีที่โล่งที่อาจทำเป็นพื้นที่สีเขียวได้หรือไม่
- **เปรียบเทียบอุณหภูมิ** ตามสัดส่วนพื้นที่สีเขียวและสิ่งปลูกสร้าง เลือกปีและฤดูได้
- **ย้อนหลัง 20 ปี:** สิ่งปลูกสร้างที่เพิ่มขึ้นกับอุณหภูมิที่เปลี่ยนไป

## ข้อมูลที่ใช้

| ข้อมูล | แหล่ง |
|---|---|
| อุณหภูมิพื้นผิวช่วงบ่าย 12–14 น. | NASA MODIS Aqua (MYD11A1) 2003–2022, VIIRS (VNP21A1D) 2023 เป็นต้นไป (ปรับเทียบกับ Aqua รายพิกเซลจากช่วง 2013–2020) |
| ที่โล่ง | ESA WorldCover 2021 (10 ม.) |
| พื้นผิว (สิ่งปลูกสร้าง ถนน ต้นไม้ หญ้า ดินเปล่า) | Dynamic World V1 (Google, National Geographic Society, World Resources Institute) |
| สิ่งปลูกสร้างย้อนหลังและประชากร | GHSL, European Commission JRC |
| ขอบเขตจังหวัด | FAO GAUL 2015 (ใช้เพื่อการศึกษา ไม่ใช่เชิงพาณิชย์) |

## ความเร็ว

บนเว็บจริง เซิร์ฟเวอร์จะสั่ง Earth Engine คำนวณชั้นข้อมูลรายปีบนกริด 30 ฟิลิปดา (≈1 กม.) เก็บไว้ใน `projects/<project>/assets/uhi_v1` ครั้งแรกใช้เวลาราว 1–3 ชั่วโมง ระหว่างนั้นจะคำนวณสดแทน ดูความคืบหน้าได้ที่ `/api/status`

## รันบนเครื่องตัวเอง (Windows)

```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
earthengine authenticate
python app.py
```

แล้วเปิด http://127.0.0.1:5000

## รันเป็นเว็บ (Render)

- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn app:app --workers 1 --threads 4 --timeout 300`
- Environment variables:
  - `EE_SERVICE_ACCOUNT_KEY` = เนื้อหาไฟล์ JSON ของ service account ทั้งไฟล์
  - `EE_PROJECT` = Project ID ของ Google Cloud (ถ้าไม่ใส่จะใช้ค่าในโค้ด)
  - `PYTHON_VERSION` = `3.12.3`

**ห้ามอัปโหลดไฟล์ key ของ service account ขึ้น GitHub**
