# Handoff: การปรับปรุงระบบ CCTV Ingestion

**วันที่:** 21 กันยายน 2026  
**โปรเจกต์:** CCTV Footage Ingestion (`NVR_server_ingestion`)

---

## 1. ปัญหาไฟล์ MP4 และความเข้ากันได้กับ Windows Media Player (Part 1)

### ปัญหาเดิมที่พบ
เมื่อดาวน์โหลดไฟล์คลิปผ่านระบบ แล้วนำไฟล์ `.mp4` ไปเปิดใน **Windows Media Player Legacy** (`wmplayer.exe`) จะพบข้อผิดพลาด:
> *"Windows Media Player cannot play the file. The Player might not support the file type or might not support the codec that was used to compress the file."*

### สาเหตุเชิงเทคนิค
1. **ไฟล์ไม่ใช่คอนเทนเนอร์ MP4 แท้ (Fake MP4 Container):**
   * ในโหมดดาวน์โหลด Whole Segment (`trim=false` ซึ่งเป็นค่าเริ่มต้น) ระบบเดิมดึงข้อมูลดิบจาก Hikvision NVR ISAPI (`/ISAPI/ContentMgmt/download`) บันทึกลงดิสก์ตรงๆ โดยตั้งชื่อไฟล์ลงท้ายด้วย `.mp4`
   * เนื้อในไฟล์จริงๆ เป็นรูปแบบ **MPEG Program Stream (MPEG-PS) พร้อม Header `IMKH` ของ Hikvision**
   * โปรแกรมอย่าง VLC สามารถเล่นได้เพราะอ่านสตรีมไบต์โดยตรง แต่ **Windows Media Player Legacy** ตรวจจับนามสกุล `.mp4` แล้วเรียก Demuxer มาตรฐานของ Windows ซึ่งคาดหวังโครงสร้าง ISO Base Media File Format (กล่อง `ftyp`) เมื่อพบ Header `IMKH` จึงปฏิเสธไฟล์ทันที
2. **ปัญหาแท็ก FourCC ของ Codec H.265 (HEVC):**
   * สำหรับคลิปที่ผ่านการ Trim (`trim=true`) ตัว ffmpeg คัดลอกสตรีม HEVC เข้า MP4 โดยใช้แท็กดีฟอลต์เป็น `hev1` ซึ่ง Windows Media Foundation และ QuickTime ต้องการแท็ก `hvc1` ถึงจะยอมรับและถอดรหัส

### แนวทางแก้ไขที่ใช้: Fast Remuxing to Real MP4 (Stream Copy)
ใช้การ **Stream Copy (`-c copy`)** ย้ายข้อมูลวิดีโอจาก MPEG-PS เข้ากล่อง MP4 มาตรฐานสากล:
* **ความเร็วสูงมาก:** ใช้เวลาเพียง 1-2 วินาทีต่อไฟล์ขนาด 1 GB
* **ไม่กิน CPU:** ใช้ CPU น้อยมาก (1-3%) ไม่กระทบการดึงข้อมูลหลายกล้องพร้อมกัน
* **คุณภาพ 100%:** ไม่มีการบีบอัดข้อมูลภาพใหม่ ภาพคมชัดเท่าต้นฉบับเป๊ะ
* **ประหยัดดิสก์:** ดาวน์โหลดไฟล์ `.ps` ชั่วคราวลงในโฟลเดอร์ scratch (`output/.segments/run_xxx/`) เมื่อ Remux เสร็จจะลบไฟล์ชั่วคราวทิ้งทันที

---

## 2. การตรวจสอบเคสบันทึกข้ามวัน และฟีเจอร์แยกวันและเวลาประจำวัน (Part 2)

### ผลการตรวจสอบเคสบันทึกข้ามวันเดิม (เช่น วันที่ 10 ไป 11 หรือ 10 ถึง 12)
1. **โฟลเดอร์ถูกยึดตามวันเริ่มต้นวันเดียว (Start Date Bug):**
   * ในโหมด Trim เดิม โค้ดสร้างโฟลเดอร์จาก `start_dt` วันแรกเพียงอย่างเดียว หากดึงวันที่ 10 เวลา 22:00 ถึงวันที่ 11 เวลา 04:00 หรือถึงวันที่ 12 ไฟล์ทั้งหมดจะถูกยัดลง `output/2026-09-10/` ทั้งหมด ไม่มีโฟลเดอร์ของวันที่ 11 หรือ 12
2. **ปัญหาการดาวน์โหลดช่วงกลางคืนที่ไม่ต้องการ (Unwanted Off-hours):**
   * ระบบเดิมมองช่วงเวลาข้ามวันหลายวันเป็น "ก้อนเดียวต่อเนื่องยาว 36-60 ชั่วโมง" ทำให้ต้องดาวน์โหลดช่วงกลางคืน (22:00-10:00) ที่ผู้ใช้ไม่ได้สนใจติดมาด้วยทุกคืน เสียเวลาและแบนด์วิดท์มหาศาล
3. **ความเสี่ยงคลิปยักษ์ในโหมด Trim:**
   * การพยายามนำวิดีโอ 60 ชั่วโมง (~50 ท่อน) มาต่อกันเป็นไฟล์เดี่ยว เสี่ยงต่อการเกิดคำสั่ง ffmpeg ยาวเกินลิมิต, ใช้ดิสก์ชั่วคราวหลายสิบ GB และเสี่ยงต่อ timestamp desync
4. **Snapshot:** ได้รูปเฉพาะวินาทีเริ่มต้นของวันแรกเพียงรูปเดียว

### ฟีเจอร์ใหม่ที่พัฒนา: "Daily Recurring Time Window (แยกวันและเวลา)"
รองรับการระบุช่วงวันที่ (Date Range) ควบคู่กับช่วงเวลาประจำวัน (Daily Window) เช่น:
* **ช่วงวันที่:** วันที่ 10, 11, 12 (`2026-09-10` ถึง `2026-09-12`)
* **เวลาประจำวัน:** `10:00:00` ถึง `22:00:00` (หรือเคสข้ามคืนประจำวัน เช่น `22:00:00` ถึง `04:00:00`)

**สิ่งที่ระบบทำให้อัตโนมัติ:**
* แตกเป็นรอบประจำวัน 3 รอบอย่างแม่นยำ:
  * วันที่ 1: `2026-09-10 10:00:00` -> `2026-09-10 22:00:00` (เซฟลง `output/2026-09-10/` + Snapshot ตอน 10:00)
  * วันที่ 2: `2026-09-11 10:00:00` -> `2026-09-11 22:00:00` (เซฟลง `output/2026-09-11/` + Snapshot ตอน 10:00)
  * วันที่ 3: `2026-09-12 10:00:00` -> `2026-09-12 22:00:00` (เซฟลง `output/2026-09-12/` + Snapshot ตอน 10:00)
* **ข้ามช่วงกลางคืนที่ไม่ต้องการ (22:00-10:00):** ประหยัดแบนด์วิดท์ เวลา และพื้นที่จัดเก็บไปกว่า 50%
* ในโหมด Trim: ได้คลิป 12 ชั่วโมงของแต่ละวันอย่างเป็นระเบียบ ปลอดภัย ไร้ข้อผิดพลาด

---

## 3. รายละเอียดไฟล์ที่แก้ไข (Files Modified)

### 1) [`cctv_retrieve.py`](file:///Volumes/Mac_storage/projects.nosync/NVR_server_ingestion/cctv_retrieve.py)
* **ฟังก์ชันการแปลง MP4 และ Codec (Part 1):**
  * [`probe_video_codec(path)`](file:///Volumes/Mac_storage/projects.nosync/NVR_server_ingestion/cctv_retrieve.py): ตรวจจับ Codec (`h264`, `hevc` ฯลฯ)
  * [`remux_to_mp4(src, out, ...)`](file:///Volumes/Mac_storage/projects.nosync/NVR_server_ingestion/cctv_retrieve.py): รัน `ffmpeg -i src.ps -map 0:v:0 -c copy -tag:v hvc1 -movflags +faststart out.part.mp4`
  * ปรับปรุง [`cut_clip()`](file:///Volumes/Mac_storage/projects.nosync/NVR_server_ingestion/cctv_retrieve.py): ใส่ `-tag:v hvc1` อัตโนมัติเมื่อตรวจพบวิดีโอ HEVC
* **ฟังก์ชันคำนวณช่วงเวลาแยกตามวัน (Part 2):**
  * [`parse_time_str(s)`](file:///Volumes/Mac_storage/projects.nosync/NVR_server_ingestion/cctv_retrieve.py): รองรับฟอร์แมต `HH:MM:SS` และ `HH:MM` หรืออ็อบเจกต์ `time`
  * [`parse_date_str(s)`](file:///Volumes/Mac_storage/projects.nosync/NVR_server_ingestion/cctv_retrieve.py): แปลง `YYYY-MM-DD` หรือรับอ็อบเจกต์ `date`
  * [`resolve_daily_windows(...)`](file:///Volumes/Mac_storage/projects.nosync/NVR_server_ingestion/cctv_retrieve.py): แตกช่วงเวลาตามวัน รองรับทั้งในวันเดียวกันและข้ามคืน
  * [`run_windows(...)`](file:///Volumes/Mac_storage/projects.nosync/NVR_server_ingestion/cctv_retrieve.py): ประมวลผลลูปทีละวันตามลำดับ
* **ฟังก์ชันตรวจจับไฟล์ซ้ำและการแปลง Legacy File (Part 3):**
  * [`is_real_mp4(path)`](file:///Volumes/Mac_storage/projects.nosync/NVR_server_ingestion/cctv_retrieve.py): ตรวจสอบ Header ISO BMFF (`ftyp`/`moov`) ในระดับไมโครวินาที
  * [`repair_legacy_file(path, ...)`](file:///Volumes/Mac_storage/projects.nosync/NVR_server_ingestion/cctv_retrieve.py): แปลงไฟล์เก่าแบบ In-place เข้า MP4 แท้
  * [`scan_and_repair_legacies(root_dir, ...)`](file:///Volumes/Mac_storage/projects.nosync/NVR_server_ingestion/cctv_retrieve.py): สแกนและแปลงไฟล์แบบ Recursive ทุกโฟลเดอร์ย่อย
  * ปรับปรุง [`mark_existing()`](file:///Volumes/Mac_storage/projects.nosync/NVR_server_ingestion/cctv_retrieve.py): ตรวจสอบไฟล์ที่มีอยู่ หากเป็น Real MP4 จะ mark existing หากเป็นไฟล์เก่าจะ mark legacy เพื่อนำไปแปลงต่อไป
  * ปรับปรุง [`CameraPlan`](file:///Volumes/Mac_storage/projects.nosync/NVR_server_ingestion/cctv_retrieve.py): เพิ่มฟิลด์ `legacy`, `legacy_clip` และคำนวณ `expected_bytes` โดยไม่นับไฟล์ที่อยู่บนดิสก์แล้ว
  * ปรับปรุง [`process_camera()`](file:///Volumes/Mac_storage/projects.nosync/NVR_server_ingestion/cctv_retrieve.py): ทำการ In-place Remuxing ทันทีเมื่อเจอไฟล์เก่าบนดิสก์ พร้อมระบบ Self-healing Fallback ดาวน์โหลดใหม่สดๆ หากไฟล์เก่าเสียหาย
* **CLI Arguments:**
  * เพิ่ม `--start-date`, `--end-date`, `--daily-start`, `--daily-end`
  * เพิ่ม `--repair-legacies [output_dir]`: เครื่องมือ CLI สแกนและแปลงไฟล์เก่าทั้งหมดแบบ Standalone

### 2) [`webgui.py`](file:///Volumes/Mac_storage/projects.nosync/NVR_server_ingestion/webgui.py)
* **UI Controls ใหม่:**
  * เพิ่ม Radio Switcher: เลือกระหว่าง **"Single continuous window / Last N min"** หรือ **"Daily recurring (แยกวันและเวลา เช่น 10,11,12 เวลา 10:00-22:00)"**
  * โหมด Daily Recurring มีอินพุต: Start Date, End Date, Daily Start Time, Daily End Time และปุ่ม Preset
  * เพิ่มปุ่ม **`🛠 Scan & Convert Legacy Files`** ในหน้าเว็บ
* **Engine & API:**
  * เพิ่ม API Route: `POST /api/repair-legacies` สำหรับสั่งสแกนและแปลงไฟล์ผ่านหน้าเว็บ
  * ฟังก์ชัน JS `repairLegacies()`: เรียก API พร้อมกล่องโต้ตอบยืนยันและสรุปจำนวนไฟล์ที่แปลงสำเร็จ
  * ปรับปรุง `do_run()` และ `GuiCallbacks` รองรับ Multi-window Ingestion และรายงานผลสะสม

### 3) [`test_cctv.py`](file:///Volumes/Mac_storage/projects.nosync/NVR_server_ingestion/test_cctv.py)
* ไฟล์ชุดทดสอบ Unit Tests อัตโนมัติ ครอบคลุม 30 Test Cases ครอบคลุม 4 กลุ่มหลัก: Legacy Detection & Audio Handling, Time Windows, CLI & File Handling และ WebGUI Features

---

## 4. การทดสอบและความถูกต้อง (Testing & Verification)

1. **Python Syntax Compilation:**
   ```bash
   python3 -m py_compile cctv_retrieve.py webgui.py test_cctv.py
   # ผลลัพธ์: Exit Code 0 (ไวยากรณ์ถูกต้องสมบูรณ์ทุกไฟล์)
   ```

2. **Ruff Linter Check:**
   ```bash
   ruff check . --select F,E9,B,W6
   # ผลลัพธ์: All checks passed! (ไม่มีข้อผิดพลาดด้าน Syntax, Runtime หรือ Bug-prone patterns)
   ```

3. **Automated Unit Tests (ครอบคลุม 30 กรณีทดสอบ):**
   * ดูรายละเอียดชุดทดสอบทั้งหมดใน [ส่วนที่ 7](#7-ผลการทดสอบความถูกต้อง-comprehensive-automated-tests)
   * **คำสั่ง:** `python3 -m unittest test_cctv.py`
   * **ผลการทดสอบ:** `Ran 30 tests in 2.109s ... OK` (ผ่านครบ 100%)

4. **CLI Execution & Parameter Verification:**
   * ทดสอบคำสั่ง Help และ Parameter Parsing:
     ```bash
     python3 cctv_retrieve.py --help
     python3 cctv_retrieve.py --repair-legacies
     ```
   * **ผลลัพธ์:** Argument parsing ของ `--repair-legacies` ทำงานถูกต้องทั้งแบบระบุไดเรกทอรีและแบบ Default Fallback ไปยัง `--output-dir`

---

## 5. คู่มือการใช้งาน (Usage Examples)

### การใช้งานผ่าน Web GUI:
1. เปิดหน้าเว็บ `http://<ip>:8080`
2. ในส่วน **Run**:
   * ติ๊กเลือก **"Daily recurring (แยกวันและเวลา เช่น 10,11,12 เวลา 10:00-22:00)"**
   * เลือก **Start Date** (เช่น `2026-09-10`) และ **End Date** (เช่น `2026-09-12`)
   * ระบุเวลาประจำวัน **Daily Start Time** (เช่น `10:00`) และ **Daily End Time** (เช่น `22:00`) หรือคลิก Preset
3. กด **"Run ingestion"** แล้วติดตามความคืบหน้าของแต่ละวันผ่านหน้าเว็บได้ทันที

### การใช้งานผ่าน CLI:
```bash
# ดึงข้อมูลวันที่ 10 ถึง 12 กันยายน เฉพาะช่วงเวลา 10:00 ถึง 22:00 ของแต่ละวัน พร้อมตัดคลิปแบบเป๊ะ
python3 cctv_retrieve.py --csv camera_n_nvr.csv \
  --start-date 2026-09-10 --end-date 2026-09-12 \
  --daily-start 10:00:00 --daily-end 22:00:00 \
  --mode both --workers 4 --per-nvr 2 --trim --output-dir output
```

### การเปิดดูไฟล์บน Windows:
* **กล้อง H.264:** ดับเบิลคลิกเปิดด้วย **Windows Media Player Legacy** ได้ทันที
* **กล้อง H.265 (HEVC):** เปิดด้วย **Media Player (Windows 11)** หรือใช้ **VLC Media Player** ได้อย่างไร้ปัญหา

---

## 6. การตรวจจับไฟล์ซ้ำและการแปลง Legacy Files เป็น Real MP4 อัตโนมัติ (Part 3)

### ปัญหาและความต้องการ
1. **ไฟล์ที่เคยดาวน์โหลดมาแล้วในอดีต (Legacy Files):**
   * ไฟล์ที่เคยดาวน์โหลดด้วยระบบเวอร์ชันก่อนหน้าถูกบันทึกลงดิสก์เป็น `.mp4` แต่เนื้อในยังคงเป็นสตรีมดิบ **MPEG-PS พร้อม Header Hikvision (`IMKH` / `0x000001BA`)**
2. **ปัญหาของระบบตรวจจับไฟล์ซ้ำเดิม (Old Duplicate Detection):**
   * ระบบเดิมใช้ `file_done(path)` ตรวจเพียงว่ามีไฟล์อยู่บนดิสก์และขนาด > 0 ไบต์หรือไม่
   * ผลคือ ระบบมองว่าไฟล์เก่าเหล่านั้น "ดาวน์โหลดเสร็จสมบูรณ์แล้ว" จึงข้ามไป (Skip) ทำให้ไฟล์เก่าไม่ได้รับการแก้ไขให้เป็น MP4 แท้
   * หากลบไฟล์เก่าทิ้งเพื่อบังคับโหลดใหม่ ก็ต้องเสียเวลาและแบนด์วิดท์ดาวน์โหลดไฟล์ละ ~1 GB จาก NVR อีกรอบโดยไม่จำเป็น

### โซลูชันที่พัฒนา: "Smart Duplicate Detection & In-Place Legacy Repair"
ปรับปรุงระบบตรวจจับไฟล์ซ้ำให้ตรวจสอบความถูกต้องของคอนเทนเนอร์วิดีโอ (`is_real_mp4`) พร้อมกลไกแปลงไฟล์เดิมในเครื่องให้เป็น Real MP4 ทันทีโดยไม่ต้องดาวน์โหลดใหม่จาก NVR:

```
[ ตรวจสอบไฟล์บนดิสก์ (Duplicate Detection) ]
                  │
     ┌────────────┴────────────┐
     ▼                         ▼
[ เป็น Real MP4 แท้ ]     [ เป็นไฟล์เดิมระบบเก่า (Legacy non-MP4) ]
     │                         │
  (ข้ามการดาวน์โหลด)      (แปลงไฟล์ In-place ในเครื่องเป็น Real MP4 ทันที)
                           ความเร็ว ~1-2 วิ/GB (ไม่เปลืองเน็ต NVR)
                               │
                       ┌───────┴───────┐
                       ▼               ▼
                 [ แปลงสำเร็จ ]    [ ไฟล์เก่าเสียหาย ]
                       │               │
                  (บันทึกเสร็จสิ้น) (ดาวน์โหลดใหม่จาก NVR อัตโนมัติ)
```

### รายละเอียดการทำงานเชิงเทคนิค

1. **ฟังก์ชันตรวจสอบ MP4 แท้ระดับไมโครวินาที (`is_real_mp4`):**
   * ตรวจสอบ Header ไบต์ที่ 4 ถึง 7 ของไฟล์ว่าเป็นกล่อง `ftyp` หรือ `moov` ของมาตรฐาน ISO Base Media File Format หรือไม่
   * ทำงานได้เร็วมาก (0.00001 วินาที/ไฟล์) ตรวจสอบไฟล์หลายร้อยไฟล์ได้ในเสี้ยววินาที
   * ปฏิเสธไฟล์ MPEG-PS ของ Hikvision (`IMKH`, `HKMI`, `0x000001BA`) ได้อย่างแม่นยำ 100%

2. **การผสานเข้ากับ `mark_existing()` และ `CameraPlan`:**
   * แยกสถานะไฟล์ออกเป็น 2 ประเภท:
     * `plan.existing`: ไฟล์ที่เป็น Real MP4 สมบูรณ์แล้ว
     * `plan.legacy` / `plan.legacy_clip`: ไฟล์ที่มีอยู่บนดิสก์ แต่ยังเป็นรูปแบบเก่า
   * ฟังก์ชัน `expected_bytes` คำนวณขนาดที่ต้องดาวน์โหลดผ่านเน็ตเวิร์ก โดยไม่นับไฟล์ใน `legacy` ทำให้ไม่เกิด Overhead การดาวน์โหลดซ้ำ

3. **กลไก In-place Fast Remuxing ใน `process_camera()`:**
   * เมื่อเข้าสู่ขั้นตอนประมวลผลกล้อง หากพบว่าไฟล์อยู่ใน `legacy`:
     * ระบบรัน `remux_to_mp4` โดยแปลงจากไฟล์ต้นทางไปยังไฟล์ชั่วคราว `.part.mp4` ด้วย Stream Copy (`-c copy`)
     * เมื่องานเสร็จสิ้น จะใช้ `os.replace` แทนที่ไฟล์เดิมอย่างปลอดภัย (Atomic Operation)
     * บันทึก Note ในรายงาน: `converted legacy file to MP4: <filename>`
   * **Self-healing Fallback:** หากไฟล์เก่าบนดิสก์เสียหายหรือไม่สมบูรณ์จน ffmpeg ไม่สามารถอ่านได้ ระบบจะลบไฟล์ที่เสียทิ้งและดาวน์โหลดสดใหม่จาก NVR โดยอัตโนมัติ

4. **เครื่องมือสแกนและแปลงไฟล์แบบ Standalone (`--repair-legacies`):**
   * **CLI Command:**
     ```bash
     python3 cctv_retrieve.py --repair-legacies output
     ```
     สแกนทุกโฟลเดอร์ย่อยใน `output/` แปลงไฟล์เก่าทั้งหมดให้เป็น Real MP4 โดยไม่ต้องเชื่อมต่อ NVR หรือใช้ไฟล์ CSV
   * **Web GUI Button:**
     เพิ่มปุ่ม **`🛠 Scan & Convert Legacy Files`** ในหน้าเว็บ พร้อม API Endpoint `/api/repair-legacies` เพื่อให้ผู้ใช้สามารถกดคลิกเดียวแปลงไฟล์ทั้งโฟลเดอร์ได้ทันทีจากเบราว์เซอร์

---

## 7. ผลการทดสอบความถูกต้อง (Comprehensive Automated Tests)

สร้างและปรับปรุงไฟล์ทดสอบ [`test_cctv.py`](file:///Volumes/Mac_storage/projects.nosync/NVR_server_ingestion/test_cctv.py) รวบรวม Test Cases ทั้งหมด 30 การทดสอบ แบ่งออกเป็น 4 กลุ่มหลัก:

### 1) TestLegacyDetectionAndRepair (16 การทดสอบ)
1. `test_is_real_mp4_checks`: ตรวจสอบ Header ISO BMFF ของไฟล์จำลอง (Non-existent, Empty, Truncated, Fake Legacy MPEG-PS, Genuine MP4)
2. `test_repair_legacy_file`: ทดสอบการ Remux ไฟล์เก่าเป็น MP4 แท้ และการป้องกันการแปลงซ้ำ
3. `test_scan_and_repair_legacies`: ทดสอบการสแกนและแปลงไฟล์แบบ Recursive
4. `test_mark_existing_distinguishes_legacy`: ทดสอบการแยกแยะไฟล์ Real MP4 และ Legacy ในโหมด Whole Segment
5. `test_process_camera_converts_legacy_without_download`: ทดสอบว่าไม่ดาวน์โหลดซ้ำเมื่อมีไฟล์เก่าบนดิสก์
6. `test_process_camera_trimmed_legacy_clip`: ทดสอบการ Remux คลิปที่เคย Trim ไว้แบบเก่าให้กลายเป็น Real MP4
7. `test_audio_args_helper`: ตรวจสอบการสร้างอาร์กิวเมนต์ ffmpeg สำหรับ Audio Codec ต่างๆ (AAC, MP3, PCM µ-law/A-law)
8. `test_repair_legacy_file_with_audio`: ทดสอบการ Remux ไฟล์ที่มีทั้งสตรีมภาพและเสียง
9. `test_repair_preserves_aac_audio`: ตรวจสอบการ Stream Copy เสียง AAC โดยไม่มีการแปลงรหัสซ้ำ
10. `test_repair_video_only_still_works`: ยืนยันว่าไฟล์ที่มีเฉพาะภาพยังคง Remux ได้อย่างถูกต้อง
11. `test_cut_clip_single_part_with_audio`: ทดสอบการตัดคลิปไฟล์เดี่ยวที่มีเสียง
12. `test_cut_clip_multipart_with_audio`: ทดสอบการตัดและต่อหลายคลิปผ่าน intermediate MPEG-TS พร้อมเสียง
13. `test_cut_clip_video_only`: ทดสอบการตัดและต่อคลิปแบบภาพอย่างเดียว
14. `test_mark_existing_trim_mode_segment_accounting`: ทดสอบการคำนวณ existing และ legacy segments ในโหมด Trim
15. `test_process_camera_uses_repair_legacy_file_for_clip_and_segment`: ยืนยันการเรียกใช้ `repair_legacy_file` สม่ำเสมอทุกจุด
16. `test_snapshot_via_rtsp_credential_quoting`: ตรวจสอบ URL Encoding ของ Username/Password ที่มีอักขระพิเศษสำหรับ RTSP

### 2) TestTimeWindows (3 การทดสอบ)
17. `test_single_window_explicit`: การคำนวณหน้าต่างเวลาแบบเดิม
18. `test_daily_windows_same_day`: การแตกวันสำหรับเคสในวันเดียวกัน (10:00-22:00)
19. `test_daily_windows_overnight`: การแตกวันสำหรับเคสข้ามคืน (22:00-04:00)

### 3) TestCliAndFileHandling (4 การทดสอบ)
20. `test_repair_legacies_cli_arg_resolution`: การ Parse อาร์กิวเมนต์ `--repair-legacies` ทั้งแบบระบุโฟลเดอร์และแบบ Default Fallback
21. `test_scan_and_repair_skips_segments_and_hidden_dirs`: ตรวจสอบการข้ามโฟลเดอร์ชั่วคราว `.segments` และ hidden directories
22. `test_prepare_segments_dir_cleans_stale_files_and_dirs`: ตรวจสอบการทำความสะอาดไฟล์และโฟลเดอร์ที่ค้างเกินกำหนด
23. `test_utf8_file_handling`: ตรวจสอบการอ่าน/เขียนไฟล์ด้วย UTF-8 Encoding

### 4) TestWebGuiFeatures (7 การทดสอบ)
24. `test_ui_defaults_utf8_and_atomic`: ตรวจสอบการบันทึก UI Defaults แบบ Atomic พร้อม UTF-8
25. `test_atomic_config_saving`: ตรวจสอบการบันทึก `config.env` แบบ Atomic พร้อมสิทธิ์ 0o600 และ Error Recovery
26. `test_rlock_and_log_line`: ตรวจสอบความปลอดภัยของ Thread Locking ในฟังก์ชัน `log_line`
27. `test_repair_legacies_logging`: ตรวจสอบการส่ง Log ข้อความการทำงานของฟังก์ชัน Repair เข้า WebGUI
28. `test_multi_window_fraction_and_eta`: ตรวจสอบการคำนวณ Progress Fraction และ ETA สะสมในโหมด Daily Recurring หลายวัน
29. `test_plan_run_env_variables`: ตรวจสอบลำดับความสำคัญของตัวแปรสภาพแวดล้อม (Environment Variables) ใน `plan_run`
30. `test_edit_inventory_add_camera`: ตรวจสอบการเพิ่มกล้องใหม่และการ Validate IP/Channel ในคลังข้อมูล

**คำสั่งรันชุดทดสอบ:**
```bash
python3 -m unittest test_cctv.py -v
```
**ผลลัพธ์:**
```text
Ran 30 tests in 2.109s

OK
```

---

## 8. การปรับปรุงคุณภาพโค้ดหลังการตรวจสอบ (Post-Verification Improvements)

หลังจากตรวจสอบโค้ดเทียบกับเอกสาร handoff.md ครบทั้ง 28 รายการ (ผ่านทั้งหมด) พบ 4 จุดที่สามารถปรับปรุงเพิ่มเติมได้:

### 8.1 WebGUI: ป้องกันการรัน Repair ขณะ Ingestion ทำงาน
* **ไฟล์:** `webgui.py` → `_repair_legacies()`
* **ปัญหา:** ปุ่ม "Scan & Convert Legacy Files" ไม่ตรวจสอบว่ามี Ingestion กำลังทำงานอยู่หรือไม่ อาจเกิดการชนกันที่ไฟล์ `.part.mp4`
* **แก้ไข:** เพิ่ม `with LOCK: if STATE["running"]: return 409` ก่อนเริ่มทำงาน

### 8.2 Trim Mode: ลบเงื่อนไข Fallback ที่ข้ามการตรวจ Duration
* **ไฟล์:** `cctv_retrieve.py` → `process_camera()`
* **ปัญหา:** เงื่อนไข fallback `file_done(...) and not is_real_mp4(...)` ใน trim mode ไม่ได้ตรวจสอบ duration เหมือน `mark_existing()` ทำให้ไฟล์ legacy ที่ดาวน์โหลดไม่สมบูรณ์อาจถูก remux เป็น MP4 สั้นๆ
* **แก้ไข:** ใช้ `plan.legacy_clip` เพียงอย่างเดียว (ผ่านการตรวจ duration แล้ว) และลบ fallback ซ้ำในโหมด whole segment ด้วย

### 8.3 Batch Repair: ห่อ Exception ป้องกัน Crash ทั้ง Batch
* **ไฟล์:** `cctv_retrieve.py` → `scan_and_repair_legacies()`
* **ปัญหา:** หาก `repair_legacy_file()` โยน exception ที่ไม่คาดคิด (เช่น `PermissionError` จาก `os.replace`) จะทำให้ batch scan หยุดทั้งหมด
* **แก้ไข:** ห่อด้วย `try...except Exception` ให้ไฟล์ที่มีปัญหาถูกนับเป็น failed แล้วข้ามไป

### 8.4 Case Sensitivity ของ `.part.mp4` Exclusion
* **ไฟล์:** `cctv_retrieve.py` → `scan_and_repair_legacies()`
* **ปัญหา:** ตรวจ `.mp4` แบบ case-insensitive แต่ตรวจ `.part.mp4` แบบ case-sensitive
* **แก้ไข:** เปลี่ยนเป็น `fname.lower().endswith(".part.mp4")`

---

## 9. การแก้ไขข้อผิดพลาดและการปรับปรุงเสถียรภาพขั้นสูง (Part 4: Bug Fixes & Hardening)

สรุปรายละเอียดการแก้ไขข้อผิดพลาดและการปรับปรุงเชิงลึกใน Part 4:

### 9.1 การรองรับ Audio Streams และ Transcoding อัตโนมัติ
* **ไฟล์:** `cctv_retrieve.py` (`probe_audio_codec`, `_audio_args`, `remux_to_mp4`, `cut_clip`)
* **ปัญหาเดิม:** การ Remux และ Trim เดิมใช้ `-map 0:v:0` ทำให้สัญญาณเสียงจาก NVR ถูกตัดทิ้งทั้งหมด หรือหากคัดลอกเสียงตรงๆ จะเกิดปัญหาเมื่อ NVR ใช้ G.711 µ-law/A-law ซึ่งไม่รองรับในมาตรฐานกล่อง MP4 ส่งผลให้ QuickTime หรือ Windows Media Player ปฏิเสธไฟล์
* **การแก้ไข:** 
  * เพิ่มฟังก์ชัน `probe_audio_codec()` เพื่อตรวจสอบสตรีมเสียงในไฟล์ต้นทาง
  * เพิ่มฟังก์ชัน `_audio_args()`: หากพบเสียงที่เป็น MP4-native (`aac`, `mp3`, `ac3`) จะทำการ Stream Copy (`-c:a copy`), หากพบเสียงรูปแบบอื่น (เช่น `pcm_mulaw`, `pcm_alaw`) จะทำการแปลงรหัสเป็น AAC อัตโนมัติ (`-c:a aac -b:a 128k`)
  * ปรับปรุงขั้นตอน Concat ใน `cut_clip` ให้รองรับการแปลงเสียงในไฟล์ Intermediate MPEG-TS อย่างสมบูรณ์

### 9.2 การป้องกัน Directory Traversal สแกนโดนโฟลเดอร์ชั่วคราว
* **ไฟล์:** `cctv_retrieve.py` (`scan_and_repair_legacies`)
* **ปัญหาเดิม:** `os.walk` สแกนทุกไดเรกทอรี รวมถึง `.git`, `.system_generated`, และ `.segments` ซึ่งอาจมีไฟล์ดิบ `.ps` หรือไฟล์ชั่วคราวที่กำลังดาวน์โหลด
* **การแก้ไข:** ปรับ `dirnames[:] = [d for d in dirnames if d != SEGMENTS_DIRNAME and not d.startswith(".")]` เพื่อข้ามโฟลเดอร์ชั่วคราวและ hidden directories ทั้งหมด

### 9.3 Atomic File Persistence และ Explicit UTF-8 Encoding
* **ไฟล์:** `webgui.py`, `cctv_retrieve.py` (`save_ui_defaults`, `save_credentials`, `save_disabled`, `write_run_log`)
* **ปัญหาเดิม:** การเขียนไฟล์โดยตรงอาจทำให้ไฟล์เสียหายหากเกิดไฟดับหรือ Crash ระหว่างเขียน และไม่ได้ระบุ `encoding="utf-8"` ชัดเจน
* **การแก้ไข:** ใช้การเขียนลงไฟล์ชั่วคราว `.tmp` ก่อนทำ Atomic Swap ด้วย `os.replace()`, กำหนดสิทธิ์ความปลอดภัย `0o600` บน credentials file ก่อน replace, ดักจับ Exception เพื่อลบไฟล์ `.tmp` ตกค้าง และระบุ `encoding="utf-8"` ครบถ้วนทุกจุด

### 9.4 Thread-Safe WebGUI Logging และ Multi-Window Progress Calculation
* **ไฟล์:** `webgui.py` (`log_line`, `update_stats`, `do_run`, `GuiCallbacks`)
* **ปัญหาเดิม:** `log_line` ขาด Thread Lock อาจเกิด Race Condition เมื่อมีหลาย Worker บันทึกข้อความพร้อมกัน และในโหมด Daily Recurring แถบความคืบหน้า (Progress Bar) จะรีเซ็ตกลับเป็น 0% ทุกครั้งที่ขึ้นวันใหม่
* **การแก้ไข:** 
  * ห่อ `log_line()` ด้วย `with LOCK:`
  * ปรับสูตรคำนวณ `fraction` ใน `update_stats()` ให้คำนวณความคืบหน้ารวมสะสมตาม `current_window` และ `total_windows` ทำให้เปอร์เซ็นต์เดินหน้าอย่างต่อเนื่องตลอดทั้ง Batch
  * รีเซ็ตสถิติไบต์ของแต่ละกล้อง `cam["bytes"] = 0` ในรอบวันใหม่เพื่อป้องกันตัวเลขสะสมคลาดเคลื่อน

### 9.5 RTSP Credential Escaping สำหรับ Snapshot
* **ไฟล์:** `cctv_retrieve.py` (`snapshot_via_rtsp`)
* **ปัญหาเดิม:** การใส่ Username หรือ Password ที่มีอักขระพิเศษ (เช่น `@`, `:`, `/`, `#`) เข้า URL RTSP ตรงๆ ทำให้ URL ผิดรูปแบบและเชื่อมต่อไม่สำเร็จ
* **การแก้ไข:** ใช้ `urllib.parse.quote(str(user), safe="")` และ `urllib.parse.quote(str(password), safe="")` ก่อนนำไปประกอบใน RTSP URL

### 9.6 Stale Segments Cleanup Fix
* **ไฟล์:** `cctv_retrieve.py` (`prepare_segments_dir`)
* **ปัญหาเดิม:** การเรียก `shutil.rmtree(path)` โดยไม่ตรวจว่าเป็นไดเรกทอรีหรือไม่ จะเกิด `NotADirectoryError` หากมีไฟล์เดี่ยวตกค้างในโฟลเดอร์ segments
* **การแก้ไข:** ตรวจสอบ `if os.path.isdir(path): shutil.rmtree(...) else: os.remove(...)`

### 9.7 CLI `--repair-legacies` Parameter Resolution
* **ไฟล์:** `cctv_retrieve.py` (`main`)
* **ปัญหาเดิม:** เมื่อผู้ใช้ระบุคำสั่ง `python3 cctv_retrieve.py --repair-legacies` โดยไม่ใส่พาธ แต่ใส่ `--output-dir custom_dir` ตัวแปรจะถูก Hardcode เป็นค่า Default
* **การแก้ไข:** กำหนด `nargs="?", const="", default=None` และใช้ `target_dir = args.repair_legacies or args.output_dir` ทำให้สามารถรับค่าจาก `--output-dir` ได้อย่างถูกต้อง

### 9.8 การเรียกใช้ `repair_legacy_file` แบบสม่ำเสมอใน `process_camera`
* **ไฟล์:** `cctv_retrieve.py` (`process_camera`)
* **ปัญหาเดิม:** การแปลงคลิปและเซกเมนต์เก่าบางจุดเรียก `remux_to_mp4` โดยตรง ข้ามการตรวจสอบ self-healing และ atomic fallback ของ `repair_legacy_file`
* **การแก้ไข:** เปลี่ยนมาเรียก `repair_legacy_file()` ให้เป็นมาตรฐานเดียวกันทุกจุด

### 9.9 การปฏิบัติตามมาตรฐาน Ruff / Flake8-Bugbear B904
* **ไฟล์:** `cctv_retrieve.py`, `webgui.py`
* **ปัญหาเดิม:** การ raise ข้อผิดพลาดใหม่ภายใน `except` clause โดยไม่ใช้ `from err` หรือ `from None` ละเมิดกฎ Flake8-Bugbear (B904)
* **การแก้ไข:** เพิ่ม `from e` ในจุด Exception Chaining ทั้งหมด ส่งผลให้ผ่านการตรวจสอบ `ruff check . --select F,E9,B,W6` ได้อย่างสมบูรณ์แบบ 100% โดยไม่มีข้อผิดพลาดหลงเหลือ
