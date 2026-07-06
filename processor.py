import os
import re
from PIL import Image
os.environ["OMP_NUM_THREADS"] = "1"

import shutil
from database import DEFAULT_OWNER_USER_ID, record_exists, insert_record
from paddleocr import PaddleOCR

UPLOAD_DIR = 'uploaded_receipts'
PROCESSED_DIR = 'processed_receipts'
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(PROCESSED_DIR, exist_ok=True)

_ocr_engine = None

def get_ocr_engine():
    global _ocr_engine
    if _ocr_engine is not None:
        return _ocr_engine

    init_candidates = [
        {"lang": "en", "enable_mkldnn": False},
        {"lang": "ch", "enable_mkldnn": False},
        {"lang": "en", "enable_mkldnn": False, "use_angle_cls": True},
        {"lang": "ch", "enable_mkldnn": False, "use_angle_cls": True},
    ]

    last_error = None
    for kwargs in init_candidates:
        try:
            _ocr_engine = PaddleOCR(**kwargs)
            return _ocr_engine
        except TypeError as exc:
            last_error = exc
            continue
        except Exception as exc:
            last_error = exc
            continue

    raise RuntimeError(f"Failed to initialize PaddleOCR: {last_error}")

def _extract_text(result):
    if not result:
        return ""
    
    if isinstance(result, list) and len(result) > 0 and isinstance(result[0], dict):
        dict_data = result[0]
        if 'rec_texts' in dict_data:
            return "\n".join([str(t).strip() for t in dict_data['rec_texts'] if str(t).strip()])

    parts = []
    if isinstance(result, list):
        for image_result in result:
            if not isinstance(image_result, list):
                continue
            for line in image_result:
                if isinstance(line, (list, tuple)) and len(line) == 2:
                    text_info = line[1]
                    if isinstance(text_info, (list, tuple)) and len(text_info) >= 1:
                        parts.append(str(text_info[0]).strip())
                        
    return "\n".join(parts)

def parse_receipt_data(raw_text):
    lines = [line.strip() for line in raw_text.split('\n') if line.strip()]
    merchant = "Unknown"
    amount, subtotal, tax = 0.0, 0.0, 0.0
    date = ""

    if not lines:
        return merchant, amount, date, subtotal, tax

    merchant = lines[0]
    date_pattern = r'(\d{2,4}[-/\.]\d{1,2}[-/\.]\d{2,4})'

    def extract_money(idx):
        for offset in range(3):
            if idx + offset < len(lines):
                match = re.search(r'\$?(\d+\.\d{2})', lines[idx+offset])
                if match:
                    return float(match.group(1))
        return 0.0

    for i, line in enumerate(lines):
        line_lower = line.lower()

        if not date:
            match_date = re.search(date_pattern, line)
            if match_date:
                date = match_date.group(1)

        if 'subtotal' in line_lower or 'sub-total' in line_lower:
            found_sub = extract_money(i)
            if found_sub > 0 and subtotal == 0.0:
                subtotal = found_sub

        elif 'tax' in line_lower or 'gst' in line_lower or 'pst' in line_lower or 'vat' in line_lower:
            if 'total' not in line_lower:
                found_tax = extract_money(i)
                if found_tax > 0:
                    tax += found_tax

        elif 'total' in line_lower and 'sub' not in line_lower:
            found_tot = extract_money(i)
            if found_tot > 0 and amount == 0.0:
                amount = found_tot

    if amount == 0.0 and subtotal > 0:
        amount = round(subtotal + tax, 2)

    return merchant, amount, date, subtotal, tax

def process_receipt_file(file_path, filename=None, crop_x=0, crop_y=0, crop_w=0, crop_h=0, owner_user_id=DEFAULT_OWNER_USER_ID):
    filename = filename or os.path.basename(file_path)

    if not filename.lower().endswith(('.png', '.jpg', '.jpeg')):
        return {"ok": False, "status": "unsupported_file", "raw_text": "", "filename": filename}

    if record_exists(filename, owner_user_id):
        shutil.move(file_path, os.path.join(PROCESSED_DIR, filename))
        return {"ok": True, "status": "duplicate", "raw_text": "", "filename": filename}

    try:
        print(f"\n[DEBUG] === 开始处理图片: {filename} ===")
        
        target_path = file_path
        temp_crop_path = None
        
        if crop_w > 0 and crop_h > 0:
            print(f"[DEBUG] 执行按需裁剪 -> X:{crop_x} Y:{crop_y} W:{crop_w} H:{crop_h}")
            with Image.open(file_path) as img:
                cropped = img.crop((crop_x, crop_y, crop_x + crop_w, crop_y + crop_h))
                if cropped.mode != 'RGB':
                    cropped = cropped.convert('RGB')
                temp_crop_path = file_path + "_crop.jpg"
                cropped.save(temp_crop_path, "JPEG")
            target_path = temp_crop_path

        ocr = get_ocr_engine()
        result = ocr.ocr(target_path)
        
        if temp_crop_path and os.path.exists(temp_crop_path):
            os.remove(temp_crop_path)
            
        raw_text = _extract_text(result)
        
        # 将默认分类统一写入数字 '0'
        if not raw_text.strip():
            raw_text = "OCR_EMPTY"
            merchant, amount, date, subtotal, tax, category = "Unknown", 0.0, "", 0.0, 0.0, "0"
        else:
            merchant, amount, date, subtotal, tax = parse_receipt_data(raw_text)
            category = "0" 
            
        print(f"[DEBUG] 提取结果 -> 默认代码:{category}, 商户:{merchant}, 总额:{amount}")
        print(f"[DEBUG] === 处理结束 ===\n")

        insert_record(filename, amount, merchant, date, subtotal, tax, category, raw_text, status="processed", owner_user_id=owner_user_id)
        shutil.move(file_path, os.path.join(PROCESSED_DIR, filename))
        return {"ok": True, "status": "processed", "raw_text": raw_text, "filename": filename}
    except Exception as exc:
        error_text = f"OCR_ERROR: {exc}"
        insert_record(filename, 0.0, "Unknown", "", 0.0, 0.0, "0", error_text, status="ocr_failed", owner_user_id=owner_user_id)
        print(f"[!] OCR failed for {filename}: {exc}")

        shutil.move(file_path, os.path.join(PROCESSED_DIR, filename))
        return {"ok": False, "status": "ocr_failed", "raw_text": error_text, "filename": filename}

def run_processing():
    for filename in os.listdir(UPLOAD_DIR):
        file_path = os.path.join(UPLOAD_DIR, filename)

        if not os.path.isfile(file_path):
            continue

        process_receipt_file(file_path, filename)

if __name__ == "__main__":
    run_processing()