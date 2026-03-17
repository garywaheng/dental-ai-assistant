import asyncio
import os
import re
import json
import base64
import uuid
import datetime
import sys
import logging
from typing import Optional, Tuple, List, Dict, Any, cast
from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # type: ignore
from fastapi.responses import HTMLResponse, FileResponse, Response  # type: ignore
from fastapi.staticfiles import StaticFiles  # type: ignore
from fastapi.middleware.cors import CORSMiddleware  # type: ignore
from openai import AsyncOpenAI  # type: ignore
from dotenv import load_dotenv  # type: ignore
import edge_tts  # type: ignore
from config_loader import ClinicConfig  # type: ignore

# Configure logging to always flush
logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(message)s")
logger = logging.getLogger("dental")

load_dotenv()

try:
    import zoneinfo
except ImportError:
    zoneinfo = None

# ── Load config (single source of truth) ──
CFG = ClinicConfig()
validation_issues = CFG.validate()
if validation_issues:
    logger.warning(f"[CONFIG] Validation issues: {validation_issues}")
CFG.ensure_csv_files()

TIMEZONE = CFG.timezone
CLINIC_NAME = CFG.clinic_name
ASSISTANT_NAME = CFG.assistant_name
SLOT_DURATION = datetime.timedelta(minutes=CFG.slot_duration_minutes)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

def get_current_time() -> datetime.datetime:
    """Get the current time in the configured timezone."""
    if zoneinfo:
        try:
            tz = zoneinfo.ZoneInfo(TIMEZONE)
            return datetime.datetime.now(tz)
        except Exception as e:
            logger.info(f"[TIME] ZoneInfo error for {TIMEZONE}: {e}. Falling back to system time.")
    
    # Fallback to local system time
    now = datetime.datetime.now()
    # If we are on a system that might be UTC but we want IST, we should ideally force it
    # but for now, let's just ensure it's at least not crashing.
    return now

def get_current_date() -> datetime.date:
    return get_current_time().date()

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/assets", StaticFiles(directory="assets"), name="assets")

@app.get("/")
async def read_root():
    with open("index.html", "r") as f:
        return HTMLResponse(content=f.read(), headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/styles.css")
async def serve_css():
    return FileResponse("styles.css", media_type="text/css")

@app.get("/demo")
async def serve_demo():
    with open("demo.html", "r") as f:
        return HTMLResponse(content=f.read())

@app.get("/contact")
async def serve_contact():
    with open("contact.html", "r") as f:
        content = f.read()
        content = content.replace("{{ SUPABASE_URL }}", SUPABASE_URL)
        content = content.replace("{{ SUPABASE_KEY }}", SUPABASE_KEY)
        return HTMLResponse(content=content)

# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE LAYER - Pure functions, no LLM involved
# ═══════════════════════════════════════════════════════════════════════════════

def parse_time_12h(time_str):
    """Parse '09:00 AM', '9:00 AM', '8 AM', '4', '4:30' into a datetime.time object."""
    time_str = time_str.strip().lower()
    if time_str == "noon": return datetime.time(12, 0)
    if time_str == "midnight": return datetime.time(0, 0)
    
    # Handle plain numbers like "4" or "4:30"
    res = None
    for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M", "%I %p", "%I%p"):
        try:
            res = datetime.datetime.strptime(time_str.upper(), fmt).time()
            break
        except ValueError:
            continue
            
    # If it's a plain hour like "4", %I%p or %H:%M won't catch it unless we fix the format
    if not res:
        match = re.match(r'^(\d{1,2})(:(\d{2}))?$', time_str)
        if match:
            h = int(match.group(1))
            m = int(match.group(3)) if match.group(3) else 0
            if 0 <= h <= 23 and 0 <= m <= 59:
                res = datetime.time(h, m)

    # Heuristic for ambiguous times (no am/pm specified)
    if res and "am" not in time_str and "pm" not in time_str:
        # If hour is 1-7, it's almost certainly PM in a dental clinic context
        if 1 <= res.hour <= 7:
            res = res.replace(hour=res.hour + 12)
            
    return res

# ── Vague time handling (morning / afternoon / evening) ──
VAGUE_TIME_RANGES = {
    "morning": (datetime.time(8, 0), datetime.time(12, 0)),
    "afternoon": (datetime.time(12, 0), datetime.time(17, 0)),
    "evening": (datetime.time(17, 0), datetime.time(20, 0)),
}

def is_vague_time(time_str):
    """Check if the time string is a vague period like 'morning', 'afternoon', 'evening'."""
    if not time_str:
        return False
    return time_str.strip().lower() in VAGUE_TIME_RANGES

def get_vague_time_range(time_str):
    """Get the (start, end) time range for a vague time period."""
    if not time_str:
        return None
    return VAGUE_TIME_RANGES.get(time_str.strip().lower())

def get_doctor_working_hours(doctor_id: Any, day_name: str) -> Optional[Tuple[datetime.time, datetime.time]]:
    """Return (start_time, end_time) for a doctor on a given day, or None."""
    hours_str = CFG.get_doctor_working_hours(str(doctor_id), day_name)
    if not hours_str:
        return None
    start = parse_time_12h(hours_str[0])
    end = parse_time_12h(hours_str[1])
    if start and end:
        return (start, end)
    return None

def find_matching_doctors(reason: Any) -> List[str]:
    """Find doctor IDs whose specialty matches the reason. Uses config."""
    return CFG.find_matching_doctors(reason or "")

def normalize_date(date_str):
    """Normalize date string (e.g., 'March 12', 'Friday', 'tomorrow', 'March 13th') to 'YYYY-MM-DD'."""
    if not date_str:
        return date_str

    # Pre-clean: remove ordinals (13th -> 13) and extra punctuation
    d_clean = re.sub(r'(\d+)(st|nd|rd|th)', r'\1', date_str)
    d_clean = d_clean.replace(",", "").strip()
    d_lower = d_clean.lower().strip()
    now = get_current_date()

    # Handle relative dates FIRST (before any format parsing)
    if d_lower in ("today", "today's"):
        return now.strftime("%Y-%m-%d")
    if d_lower in ("tomorrow", "tmrw", "tmr"):
        return (now + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    if "tomorrow" in d_lower or "tmrw" in d_lower:
        return (now + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    if "today" in d_lower:
        return now.strftime("%Y-%m-%d")
    if "day after tomorrow" in d_lower:
        return (now + datetime.timedelta(days=2)).strftime("%Y-%m-%d")

    # Try YYYY-MM-DD
    try:
        dt = datetime.datetime.strptime(d_clean, "%Y-%m-%d").date()
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        pass

    # Try M/D/YYYY and M/D formats
    for fmt in ("%m/%d/%Y", "%m/%d"):
        try:
            if fmt == "%m/%d":
                dt = datetime.datetime.strptime(f"{now.year}/{d_clean}", "%Y/%m/%d").date()
            else:
                dt = datetime.datetime.strptime(d_clean, fmt).date()
            if dt < now:
                dt = datetime.date(dt.year + 1, dt.month, dt.day)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    # Try "March 12" variants
    for fmt in ("%B %d", "%b %d", "%d %B", "%d %b"):
        try:
            dt = datetime.datetime.strptime(f"{now.year} {d_clean}", f"%Y {fmt}").date()
            if dt < now:
                dt = datetime.date(dt.year + 1, dt.month, dt.day)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    # Try "Friday March 12" variants (extract the month and day part)
    match = re.search(r'(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+(\d+)', d_lower)
    if match:
        month_str = match.group(1)
        day_str = match.group(2)
        try:
            return normalize_date(f"{month_str} {day_str}")
        except:
            pass

    # Try day of week (Friday, Monday, etc.)
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for i, day in enumerate(days):
        if day in d_lower:
            current_weekday = now.weekday()
            target_weekday = i
            days_ahead = target_weekday - current_weekday
            if days_ahead <= 0:
                days_ahead += 7
            if "next" in d_lower:
                days_ahead += 7
            res_date = now + datetime.timedelta(days=days_ahead)
            return res_date.strftime("%Y-%m-%d")

    return date_str

def check_booking_overlap(date_str, start_time_str, end_time_str, doctor_id, bookings_path="bookings.csv"):
    """Check if a booking overlaps with existing bookings for the same doctor on the same date."""
    new_start = parse_time_12h(start_time_str)
    new_end = parse_time_12h(end_time_str)
    if not new_start or not new_end:
        return True, "Invalid time format"
    if not os.path.exists(bookings_path):
        return False, ""
    
    norm_date = normalize_date(date_str)
    
    with open(bookings_path, "r") as f:
        for line in f.readlines()[1:]:  # type: ignore[index]
            parts = line.strip().split(",")
            if len(parts) >= 5:
                b_date = parts[0].strip()
                b_start = parse_time_12h(parts[1].strip())
                b_end = parse_time_12h(parts[2].strip())
                b_doc = parts[3].strip()
                if b_date == norm_date and b_doc == str(doctor_id) and b_start and b_end:
                    if new_start < b_end and b_start < new_end:  # type: ignore[operator]
                        overlap_msg = f"Overlaps with existing booking {b_start.strftime('%I:%M %p')}-{b_end.strftime('%I:%M %p')} for Doctor {b_doc}"
                        logger.info(overlap_msg)
                        return True, overlap_msg
    logger.info(f"No overlap found for {doctor_id} on {norm_date} at {start_time_str}")
    return False, ""

def is_past_date(date_str):
    """Check if the given date is in the past."""
    norm_date = normalize_date(date_str)
    try:
        booking_date = datetime.datetime.strptime(norm_date, "%Y-%m-%d").date()
        return booking_date < get_current_date()
    except Exception:
        return False

def _find_existing_patient(patient_data: Dict[str, Optional[str]], patients_path="patients.csv"):
    """Check if a patient with matching unique fields already exists. Config-driven."""
    if not os.path.exists(patients_path):
        return None
    
    # Get unique-match field keys and their indices in the CSV
    all_keys = CFG.all_patient_keys
    match_fields = [f for f in CFG.patient_fields if f.unique_match]
    
    with open(patients_path, "r") as f:
        lines = f.readlines()
        if not lines:
            return None
        # Parse header to get column indices
        header = lines.pop(0).strip().split(",")
        col_map = {col.strip(): i for i, col in enumerate(header)}
        
        for line in lines:
            if not line.strip():
                continue
            parts = line.strip().split(",")
            pid: str = ""
            if parts:
                pid = str(parts[0]).strip()
            if not pid:
                continue
            
            all_match = True
            has_any_value = False
            for field in match_fields:
                col_idx = col_map.get(field.key)
                input_val: str = cast(str, patient_data.get(field.key) or "")
                csv_val: str = ""
                if isinstance(col_idx, int) and col_idx < len(parts):
                    csv_val = str(parts[col_idx]).strip()
                else:
                    continue
                
                if field.type == "phone":
                    input_clean = re.sub(r'\D', '', input_val)
                    csv_clean = re.sub(r'\D', '', csv_val)
                    if input_clean:
                        has_any_value = True
                        if input_clean != csv_clean:
                            all_match = False
                            break
                elif field.type == "email":
                    iv: str = input_val
                    cv: str = csv_val
                    iv_lower: str = iv.strip().lower()
                    cv_lower: str = cv.strip().lower()
                    if iv_lower:
                        has_any_value = True
                        if iv_lower != cv_lower:
                            all_match = False
                            break
                else:
                    iv: str = input_val
                    cv: str = csv_val
                    iv_lower: str = iv.strip().lower()
                    cv_lower: str = cv.strip().lower()
                    if iv_lower:
                        has_any_value = True
                        if iv_lower != cv_lower:
                            all_match = False
                            break
            
            if all_match and has_any_value:
                return pid
    return None

def is_valid_reason(reason):
    """Check if the extracted reason is a valid service/issue. Uses config keywords."""
    if not reason: return False
    reason_lower = reason.lower().strip()
    generic = ["appointment", "booking", "schedule", "help", "hi", "hey", "hello", "book", "visit"]
    if reason_lower in generic:
        return False
    # Build keyword set from all configured services
    all_keywords = set()
    for svc in CFG.services:
        all_keywords.update(svc.keywords)
    return any(kw in reason_lower for kw in all_keywords)


def get_readable_date(date_str):
    if not date_str: return date_str
    # First try to normalize the date
    norm = normalize_date(date_str)
    try:
        dt = datetime.datetime.strptime(norm, "%Y-%m-%d")
        return dt.strftime("%B %d, %Y")
    except Exception:
        pass
    return date_str


def check_availability_and_find_alternative(reason, date_str, time_str):
    """Deterministic availability check. Returns (available, message, doctor_id)."""
    logger.info(f"[AVAIL_CHECK] reason='{reason}', date='{date_str}', time='{time_str}'")
    matched_docs = find_matching_doctors(reason)
    logger.info(f"[AVAIL_CHECK] Matched doctors: {matched_docs}")
    if not matched_docs:
        matched_docs = CFG.default_doctor_ids

    book_start = parse_time_12h(time_str)
    if not book_start:
        return False, "I couldn't quite catch a valid time. Could you tell me a specific time, like 10:00 AM or 2:00 PM?", None

    if is_past_date(date_str):
        return False, f"That date has already passed. Could you suggest a future date instead?", None

    try:
        req_date = datetime.datetime.strptime(normalize_date(date_str), "%Y-%m-%d").date()
    except Exception:
        return False, "I couldn't parse the date clearly. Could you please specify it like March 15?", None

    now = get_current_time()

    if req_date == now.date() and book_start <= now.time():
        pass # Will fall through to alternative finding

    day_name = req_date.strftime("%A").lower()

    if not CFG.is_clinic_open(day_name):
        rd = get_readable_date(date_str)
        return False, f"Our clinic is closed on {day_name.title()}s. Could you suggest another day?", None

    # Check clinic working hours (not just the day)
    clinic_hours = CFG.get_clinic_hours(day_name)
    if clinic_hours:
        clinic_start = parse_time_12h(clinic_hours[0])
        clinic_end = parse_time_12h(clinic_hours[1])
        book_end_time = (datetime.datetime.combine(req_date, book_start) + SLOT_DURATION).time()
        if clinic_start and clinic_end:
            if book_start < clinic_start or book_end_time > clinic_end:
                rd = get_readable_date(date_str)
                return False, f"Our clinic is open {clinic_hours[0]} - {clinic_hours[1]} on {day_name.title()}s. That time falls outside our hours. Could you pick a time within our hours?", None

    # Try specialty-matched doctors first, then fall back to ALL doctors
    all_doctor_ids = [d.id for d in CFG.doctors]
    doctors_to_try = matched_docs
    tried_fallback = False

    for doc in doctors_to_try:
        hours = get_doctor_working_hours(doc, day_name)
        logger.info(f"[AVAIL_CHECK] Doctor {doc} hours on {day_name}: {hours}")
        if not hours: continue
        w_start, w_end = hours

        book_start_dt = datetime.datetime.combine(req_date, book_start).replace(tzinfo=now.tzinfo)
        book_end_dt = book_start_dt + SLOT_DURATION
        book_end = book_end_dt.time()

        if book_start < w_start or book_end > w_end:
            logger.info(f"[AVAIL_CHECK] Doctor {doc} out of hours: {book_start} not in {w_start}-{w_end}")
            continue

        if req_date == now.date() and book_start <= now.time():
            logger.info(f"[AVAIL_CHECK] Time {book_start} already passed today ({now.time()})")
            continue

        overlap, msg = check_booking_overlap(date_str, book_start.strftime("%I:%M %p"), book_end.strftime("%I:%M %p"), doc)
        logger.info(f"[AVAIL_CHECK] Doctor {doc} at {book_start} on {date_str} -> overlap={overlap}")
        if not overlap:
            logger.info(f"[AVAIL_CHECK] SUCCESS: Doctor {doc} is available.")
            return True, "", doc

    # If specialty-matched doctors had no availability, try ALL doctors as fallback
    if not tried_fallback:
        tried_fallback = True
        fallback_docs = [d for d in all_doctor_ids if d not in matched_docs]
        if fallback_docs:
            logger.info(f"[AVAIL_CHECK] Trying fallback doctors: {fallback_docs}")
            for doc in fallback_docs:
                hours = get_doctor_working_hours(doc, day_name)
                logger.info(f"[AVAIL_CHECK] Fallback doctor {doc} hours on {day_name}: {hours}")
                if not hours: continue
                w_start, w_end = hours

                book_start_dt = datetime.datetime.combine(req_date, book_start).replace(tzinfo=now.tzinfo)
                book_end_dt = book_start_dt + SLOT_DURATION
                book_end = book_end_dt.time()

                if book_start < w_start or book_end > w_end:
                    continue
                if req_date == now.date() and book_start <= now.time():
                    continue

                overlap, msg = check_booking_overlap(date_str, book_start.strftime("%I:%M %p"), book_end.strftime("%I:%M %p"), doc)
                if not overlap:
                    logger.info(f"[AVAIL_CHECK] SUCCESS: Fallback doctor {doc} is available.")
                    return True, "", doc

    logger.info(f"[AVAIL_CHECK] No immediate match found. Finding alternatives...")

    # Find alternative times — try ALL doctors on the requested day, respecting clinic hours
    rd = get_readable_date(date_str)
    clinic_hours = CFG.get_clinic_hours(day_name)
    c_h_start = parse_time_12h(clinic_hours[0]) if clinic_hours else None
    c_h_end = parse_time_12h(clinic_hours[1]) if clinic_hours else None

    for try_doc in (matched_docs + [d for d in all_doctor_ids if d not in matched_docs]):
        hours = get_doctor_working_hours(try_doc, day_name)
        if not hours:
            continue
        w_start, w_end = hours

        # Effective hours = intersection of clinic and doctor hours
        eff_start = max(w_start, c_h_start) if c_h_start else w_start
        eff_end = min(w_end, c_h_end) if c_h_end else w_end
        if eff_start >= eff_end:
            continue

        curr_dt = datetime.datetime.combine(req_date, eff_start).replace(tzinfo=now.tzinfo)

        if req_date == now.date():
            next_hour = now.replace(minute=0, second=0, microsecond=0) + SLOT_DURATION
            if next_hour > curr_dt:
                curr_dt = next_hour

        end_dt = datetime.datetime.combine(req_date, eff_end).replace(tzinfo=now.tzinfo)
        while curr_dt + SLOT_DURATION <= end_dt:
            c_start = curr_dt.time().strftime("%I:%M %p")
            c_end = (curr_dt + SLOT_DURATION).time().strftime("%I:%M %p")
            overlap, _ = check_booking_overlap(date_str, c_start, c_end, try_doc)
            if not overlap:
                return False, f"That time slot is not available, but {c_start} on {rd} is open. Would that work for you?", None
            curr_dt += SLOT_DURATION

    return False, f"Unfortunately, we are fully booked on {rd}. Could you suggest another day?", None

def find_nearest_slots(reason: Any, preferred_date: Optional[str] = None, start_time_str: Optional[str] = None, count: int = 3, filter_start: Optional[datetime.time] = None, filter_end: Optional[datetime.time] = None) -> List[Dict[str, Any]]:
    """Find the `count` nearest available slots around the given date/time.

    Checks clinic hours, doctor hours, and bookings.csv for conflicts.
    Same-day slots are sorted by proximity to anchor time.
    Future-day slots are in chronological order.
    Returns list of dicts: [{"date", "time", "doctor_id", "day_name", "readable_date"}, ...]
    """
    now = get_current_time()
    tz = zoneinfo.ZoneInfo(TIMEZONE) if zoneinfo else None

    # ── Determine anchor date ──
    if preferred_date:
        try:
            norm = normalize_date(preferred_date)
            anchor_date = datetime.datetime.strptime(norm, "%Y-%m-%d").date()
            if anchor_date < now.date():
                return []
        except Exception:
            anchor_date = now.date()
    else:
        anchor_date = now.date()

    # ── Determine anchor time ──
    anchor_time = None
    if start_time_str and start_time_str != "REJECTED" and not is_vague_time(start_time_str):
        anchor_time = parse_time_12h(start_time_str)
    if not anchor_time:
        if anchor_date == now.date():
            anchor_time = (now.replace(minute=0, second=0, microsecond=0) + SLOT_DURATION).time()
        else:
            anchor_time = datetime.time(9, 0)

    anchor_dt = datetime.datetime.combine(anchor_date, anchor_time, tzinfo=tz)

    # ── Find doctors (specialty-matched first, then all as fallback) ──
    matched_docs = find_matching_doctors(reason)
    if not matched_docs:
        matched_docs = CFG.default_doctor_ids
    all_doc_ids = [d.id for d in CFG.doctors]
    docs_to_try = list(matched_docs) + [d for d in all_doc_ids if d not in matched_docs]

    all_slots: List[Dict[str, Any]] = []

    for day_offset in range(15):
        check_date = anchor_date + datetime.timedelta(days=day_offset)
        if check_date < now.date():
            continue

        day_name_lower = check_date.strftime("%A").lower()
        if not CFG.is_clinic_open(day_name_lower):
            continue

        # Get clinic hours for this day
        clinic_hours = CFG.get_clinic_hours(day_name_lower)
        if not clinic_hours:
            continue
        clinic_start = parse_time_12h(clinic_hours[0])
        clinic_end = parse_time_12h(clinic_hours[1])
        if not clinic_start or not clinic_end:
            continue

        date_str = check_date.strftime("%Y-%m-%d")
        found_times_for_day: set = set()  # avoid duplicate times from multiple doctors

        for doc_id in docs_to_try:
            doc_hours = get_doctor_working_hours(doc_id, day_name_lower)
            if not doc_hours:
                continue
            w_start, w_end = doc_hours

            # Effective hours = intersection of clinic hours and doctor hours
            eff_start = max(w_start, clinic_start)
            eff_end = min(w_end, clinic_end)
            if eff_start >= eff_end:
                continue

            curr = datetime.datetime.combine(check_date, eff_start, tzinfo=tz)
            end_bound = datetime.datetime.combine(check_date, eff_end, tzinfo=tz)

            # Skip past times for today
            if check_date == now.date():
                now_ceil = now.replace(minute=0, second=0, microsecond=0) + SLOT_DURATION
                if now_ceil > curr:
                    curr = now_ceil

            while curr + SLOT_DURATION <= end_bound:
                slot_time = curr.time()
                slot_time_str = slot_time.strftime("%I:%M %p")

                # Apply optional time filters (for vague times like "morning")
                if filter_start and slot_time < filter_start:
                    curr += SLOT_DURATION
                    continue
                if filter_end and slot_time >= filter_end:
                    break

                # Deduplicate — same time on same day already found via another doctor
                if slot_time_str in found_times_for_day:
                    curr += SLOT_DURATION
                    continue

                # Check bookings.csv for conflicts
                end_time_str = (curr + SLOT_DURATION).time().strftime("%I:%M %p")
                overlap, _ = check_booking_overlap(date_str, slot_time_str, end_time_str, doc_id)

                if not overlap:
                    found_times_for_day.add(slot_time_str)
                    if day_offset == 0:
                        distance = abs((curr - anchor_dt).total_seconds())
                    else:
                        distance = float('inf')

                    all_slots.append({
                        "day_offset": day_offset,
                        "distance": distance,
                        "time": slot_time_str,
                        "date": date_str,
                        "doctor_id": doc_id,
                        "day_name": check_date.strftime("%A"),
                        "readable_date": get_readable_date(date_str),
                    })

                curr += SLOT_DURATION

        # Early exit: if past the anchor day and we already have enough
        if day_offset > 0 and len(all_slots) >= count:
            break

    if not all_slots:
        return []

    # Sort: same-day by distance to anchor, then future days chronologically
    same_day = sorted([s for s in all_slots if s["day_offset"] == 0], key=lambda s: s["distance"])
    future = [s for s in all_slots if s["day_offset"] > 0]

    return (same_day + future)[:count]


def format_slots(slots: List[Dict[str, Any]]) -> str:
    """Format a list of slot dicts into a human-readable string."""
    return ", ".join(f"{s['day_name']} {s['readable_date']} at {s['time']}" for s in slots)


def suggest_available_slots(reason: Any, preferred_date: Optional[str] = None, start_time_str: Optional[str] = None, count: int = 3, filter_start: Optional[datetime.time] = None, filter_end: Optional[datetime.time] = None) -> Optional[str]:
    """Convenience wrapper: returns formatted string or None."""
    slots = find_nearest_slots(reason, preferred_date, start_time_str, count, filter_start, filter_end)
    return format_slots(slots) if slots else None

# ═══════════════════════════════════════════════════════════════════════════════
# BOOKING STATE MACHINE - Strict, deterministic steps
# ═══════════════════════════════════════════════════════════════════════════════

class BookingState:
    def __init__(self) -> None:
        self.step: int = 1
        self.retries: int = 0
        self.total_retries: int = 0
        self.reason: Optional[str] = None
        self.date: Optional[str] = None
        self.time: Optional[str] = None
        self.doctor_id: Optional[str] = None
        self.patient_data: Dict[str, Optional[str]] = {f.key: None for f in CFG.patient_fields}
        self.booking_data: Dict[str, Optional[str]] = {f.key: None for f in CFG.booking_fields}
        self.availability_error: Optional[str] = None
        self.alternative_time: Optional[str] = None
        self.suggested_slots: List[Dict[str, Any]] = []  # nearest available slots from find_nearest_slots
        self.patient_id: Optional[str] = None
        self.saved: bool = False
        self.is_modification: bool = False

    def has_all_required_patient_fields(self) -> bool:
        """Check if all required patient fields have been collected."""
        for f in CFG.required_patient_fields:
            if not self.patient_data.get(f.key):
                return False
        return True

    def get_missing_fields_labels(self) -> str:
        """Get human-readable labels of missing required fields."""
        missing: List[str] = []
        for f in CFG.required_patient_fields:
            if not self.patient_data.get(f.key):
                missing.append(f.label)
        if not missing:
            return ""
        if len(missing) <= 2:
            return " and ".join(missing)
        last_str: str = missing.pop()
        return ", ".join(missing) + ", and " + last_str

    def has_all_required_booking_fields(self) -> bool:
        """Check if all required booking extra fields have been collected."""
        for f in CFG.required_booking_fields:
            if not self.booking_data.get(f.key):
                return False
        return True

    def get_missing_booking_field_labels(self) -> str:
        """Get human-readable labels of missing required booking fields."""
        missing: List[str] = []
        for f in CFG.required_booking_fields:
            if not self.booking_data.get(f.key):
                missing.append(f.label)
        if not missing:
            return ""
        if len(missing) <= 2:
            return " and ".join(missing)
        last_str: str = missing.pop()
        return ", ".join(missing) + ", and " + last_str

    # Backward-compat properties for name/phone/email
    @property
    def name(self): return self.patient_data.get("name")
    @name.setter
    def name(self, v): self.patient_data["name"] = v
    @property
    def phone(self): return self.patient_data.get("phone")
    @phone.setter
    def phone(self, v): self.patient_data["phone"] = v
    @property
    def email(self): return self.patient_data.get("email")
    @email.setter
    def email(self, v): self.patient_data["email"] = v


# ═══════════════════════════════════════════════════════════════════════════════
# EXTRACTION - Dedicated LLM call to extract structured data from user text
# ═══════════════════════════════════════════════════════════════════════════════

async def extract_booking_info(client, chat_history, text, state):
    """Extract structured booking data from user text. Returns a dict."""
    current_date = get_current_time().strftime("%A, %B %d, %Y")
    current_time = get_current_time().strftime("%I:%M %p")

    sys_prompt = f"""You are an exact JSON data extractor for a {CFG.specialty_label} clinic.
Analyze the FULL conversation and extract {CFG.specialty_label} appointment details.

CRITICAL DATE RESOLUTION:
- Current Year: 2026.
- Today is {current_date}. 
- The current exact time is {current_time}.
- 'Friday' ALWAYS means the very next Friday after today.
- 'Monday' means the very next Monday after today.
- 'tomorrow' means the day after today.
- 'today' means today's exact date.
- NEVER skip a week unless user says 'next Friday' (meaning the one after the upcoming one).
- If the user agrees to a suggested date/time (says 'yes', 'sure', 'ok', 'works', 'fine', 'sounds good'), you MUST extract that specific date and time.
- ALWAYS extract exactly what the user is asking for in their LATEST message. Do not be lazy and keep the old 'known' values if the user's latest message indicates a change.
- If the user provides a time (e.g., '4pm?'), you MUST also extract the date they are likely referring to based on Sarah's suggestions (e.g., the date Sarah just mentioned). NEVER leave the date as NULL if a time is provided and a date was previously suggested.
- Always assume a time like "5?" or "1pm?" refers to the date currently under discussion unless a new date is mentioned.
- If the user says a single number like "1", "2", or "3" and Sarah just gave a numbered list of slots, extract the date and time of the corresponding slot (e.g., "1" means the first slot Sarah mentioned).
- IMPORTANT: Extract ALL available fields from a single message REGARDLESS of the current step. Even if the current step is about collecting a reason, you MUST still extract date and time if the user mentions them. If the user says "I need a cleaning on March 27 at 10 AM", you MUST extract reason="cleaning", date="2026-03-27", AND time="10:00 AM" all at once. If the user says "Friday morning" at any step, extract date and time.
- If Sarah suggested times for "today" and the user picks one, the date should be today's date: {current_date}.

CURRENT STATE (for context):
- Current step: {state.step}
- Known reason: {state.reason or 'not yet provided'}
- Known date: {state.date or 'not yet provided'}
- Known time: {state.time or 'not yet provided'}
- Alternative offered: {state.alternative_time or 'none'}

BLOCKING:
- ONLY set 'is_modification' to true if the user wants to cancel, reschedule, or modify an EXISTING/ALREADY-CONFIRMED appointment.
- If the user is still in the booking process and wants to change the time or date BEFORE confirming, that is NOT a modification. Just extract the new date/time normally.
- Words like "actually", "instead", "change to", "how about" during the booking process are normal — extract the new values.

FIELDS:
- 'reason': specific {CFG.specialty_label} issue (e.g., {CFG.service_examples}). NULL if not a {CFG.specialty_label} reason.
- 'date': YYYY-MM-DD format. Resolve ALL relative dates. If the user explicitly REJECTS a date, output "REJECTED". Otherwise NULL if unclear.
- 'time': HH:MM AM/PM format. If the user says 'morning', 'afternoon', or 'evening' WITHOUT a specific time, return the exact word (e.g., "morning", "afternoon", "evening") — do NOT convert it to a specific time. If the user explicitly REJECTS a time OR says "later", output "REJECTED". If they say "after [time]", extract that specific [time] so Sarah can check availability following it. Otherwise NULL if unclear.
{CFG.get_extraction_fields_json_schema()}
- 'is_modification': boolean, true if user wants to change/cancel/reschedule.

Return ONLY valid JSON. No markdown, no explanation:
{CFG.get_extraction_json_template()}
"""
    async def call_llm(msgs, json_mode=True):
        try:
            kwargs = {
                "model": CFG.llm_model,
                "messages": msgs,
                "max_tokens": CFG.llm_max_tokens_extract,
                "temperature": CFG.llm_temperature_extract if json_mode else CFG.llm_temperature_respond,
                "timeout": 15.0  # 15 second timeout
            }
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            
            resp = await client.chat.completions.create(**kwargs)
            return resp.choices[0].message.content
        except Exception as e:
            logger.info(f"[LLM] Error: {e}")
            raise e

    history_simple = [{"role": m["role"], "content": m["content"]} for m in chat_history if m["role"] != "system"]
    user_text = text
    current_state_dict = state.__dict__

    for attempt in range(3):  # Retry extraction up to 3 times
        try:
            raw = await call_llm([
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": f"History: {json.dumps(history_simple)}\nUser Text: {user_text}\nCurrent State: {json.dumps(current_state_dict)}"}
            ])
            logger.info(f"[EXTRACT attempt {attempt+1}] Raw: {raw}")
            parsed = json.loads(str(raw))
            # Validate the extraction makes sense
            if isinstance(parsed, dict):
                return parsed
        except Exception as e:
            logger.info(f"[EXTRACT attempt {attempt+1}] Error: {e}")
            await asyncio.sleep(0.5)

    return {}


# ═══════════════════════════════════════════════════════════════════════════════
# RESPONSE GENERATION - With validation and retry
# ═══════════════════════════════════════════════════════════════════════════════

TONES = [
    "Be highly professional, warm, and polite. Use a welcoming tone.",
    "Be slightly more casual but very clear and specific. Keep it friendly.",
    "Be direct and playfully insist on getting the required information. Be charming and very polite.",
    "Be helpful and patient. The patient may be confused. Guide them firmly but with maximum kindness.",
]

DOCTOR_NAMES = CFG.doctor_names
DOCTOR_ID_PATTERNS = [r"doctor\s*(?:id\s*)?\d", r"patient\s*id\s*\d", r"doc\s*\d"]

FORBIDDEN_PHRASES = ["hold on", "let me check", "let me see", "wait while", "one moment while i check",
                      "checking our schedule", "let me look", "pulling up", "i told you",
                      "as i said", "listen to me", "previously mentioned", "like i said",
                      "i mentioned earlier"]


def scrub_response(text):
    """Remove doctor names, IDs, and forbidden phrases from response."""
    result = text
    for name in DOCTOR_NAMES:
        result = re.sub(re.escape(name), "our dentist", result, flags=re.IGNORECASE)
    for pattern in DOCTOR_ID_PATTERNS:
        result = re.sub(pattern, "our dentist", result, flags=re.IGNORECASE)
    for phrase in FORBIDDEN_PHRASES:
        result = re.sub(re.escape(phrase), "", result, flags=re.IGNORECASE)
    # Remove asterisks (action text like *smiles*)
    result = re.sub(r'\*[^*]+\*', '', result)
    return result.strip()


def validate_response(text, state):
    """Validate that the response meets requirements for the current step.
    Returns (is_valid, list_of_issues)."""
    issues = []

    if not text or len(text.strip()) < 5:
        issues.append("Response is empty or too short")
        return False, issues

    # Check for doctor name leaks
    text_lower = text.lower()
    for name in DOCTOR_NAMES:
        if name in text_lower:
            issues.append(f"Contains doctor name: {name}")

    # Check for asterisk actions
    if re.search(r'\*[^*]+\*', text):
        issues.append("Contains asterisk action text")

    # Step-specific validations
    if state.step in [1, 2, 3, 4]:
        # Must end with a question
        if "?" not in text:
            issues.append("Does not end with a question mark")

    if state.step == 5:
        # Must NOT ask a question (it's the final confirmation)
        # (This is softer — we allow "anything else?" but not leading questions)
        pass

    # Check forbidden "hold" phrases
    for phrase in FORBIDDEN_PHRASES:
        if phrase in text_lower:
            issues.append(f"Contains forbidden phrase: '{phrase}'")

    # Check conciseness (max ~3 sentences for steps 1-4)
    if state.step != 5:
        sentence_count = len(re.findall(r'[.!?]+', text))
        if sentence_count > 3:
            issues.append(f"Too verbose: {sentence_count} sentences")

    return len(issues) == 0, issues


def build_system_prompt(state, tone_index=0):
    """Build a highly specific system prompt for the current step."""
    tone = TONES[min(tone_index, len(TONES) - 1)]
    now = get_current_time()
    current_date = now.strftime("%A, %B %d, %Y")
    current_time = now.strftime("%I:%M %p")
    rd = get_readable_date(state.date)
    
    # Pre-render clinic facts so the LLM knows how to answer
    has_parking = "Free parking is available on-site." if CFG.parking else "We do not have dedicated parking, please look for street parking."
    insurance_list = ", ".join(CFG.insurance) if CFG.insurance else "We do not currently accept insurance directly."

    base = f"""You are {ASSISTANT_NAME}, a highly professional {CFG.specialty_label} receptionist at {CLINIC_NAME}. You are on a phone call.

CLINIC FACTS (For answering patient questions):
- Phone: {CFG.clinic_phone}
- Address: {CFG.clinic_address}
- Parking: {has_parking}
- Insurance Accepted: {insurance_list}

ABSOLUTE RULES:
1. Keep answers EXTREMELY concise (1-2 short sentences maximum).
2. Today's date is {current_date}.
3. The current exact time is {current_time}. You MUST use this time if asked.
4. NEVER use asterisks for actions like *smiles* or *checks schedule*.
5. NEVER mention doctor names (Smith, Jones, Davis) or doctor IDs.
6. NEVER say 'hold on', 'let me check', 'wait while I check', or 'one moment'.
7. NEVER confirm that a time slot is available unless the CURRENT INSTRUCTION explicitly tells you it is AVAILABLE.
8. NEVER make up alternative times or dates. Only suggest times if given in the HINT.
9. ALWAYS remain extremely polite, patient, and helpful. NEVER use phrases like "As I said" or "I already told you".
10. If the patient asks a valid question about the clinic (e.g. address, parking, insurance), you MUST answer their question warmly AND THEN immediately ask the question required by your CURRENT INSTRUCTION to keep the booking moving forward.
11. If they ask an unrelated question (e.g. weather), politely redirect them.

TONE: {tone}
"""

    if state.is_modification:
        instruction = f"""CURRENT INSTRUCTION:
The patient wants to change, reschedule, or cancel an existing appointment.
RESPONSE: Politely tell them you can only book NEW appointments, and they should call the front desk directly at {CFG.clinic_phone} for changes or cancellations.
Then ask if they'd like to book a NEW appointment instead.
MUST end with a question mark."""
    elif state.step == 1:
        scheduling_note = ""
        if state.date or state.time:
            parts = []
            if state.date:
                parts.append(get_readable_date(state.date))
            if state.time:
                parts.append(state.time)
            pref = " ".join(parts)
            scheduling_note = f"\nIMPORTANT: The patient has already mentioned they'd prefer {pref}. Acknowledge this scheduling preference warmly (e.g., 'Got it, {pref}!'), then ask what {CFG.specialty_label} issue or service they need."

        instruction = f"""CURRENT INSTRUCTION:
{scheduling_note}
Welcome the patient warmly to {CLINIC_NAME}.
Ask them what {CFG.specialty_label} issue or service they need (e.g., {CFG.service_examples}).
Be very professional and kind.
MUST end your response with a question mark.
Example: "Welcome to {CLINIC_NAME}! I'm Sarah. How can we help your smile today?"
"""
    elif state.step == 2:
        nearest_slot = ""
        missing = []
        if not state.date: missing.append("DATE")
        if not state.time or is_vague_time(state.time): missing.append("TIME")
        missing_str = " and ".join(missing) if missing else "DATE and TIME"

        # Check if time is vague (morning/afternoon/evening)
        has_vague_time = is_vague_time(state.time)

        suggestion = None
        if has_vague_time and state.date:
            # Suggest slots within the vague time range
            time_range = get_vague_time_range(state.time)
            suggestion = suggest_available_slots(state.reason, state.date, filter_start=time_range[0], filter_end=time_range[1])
        elif len(missing) == 2:
            suggestion = suggest_available_slots(state.reason)
        elif not state.time:
            # If no time is set, suggest slots for the given date (optionally starting after whatever they rejected)
            suggestion = suggest_available_slots(state.reason, state.date, state.time)

        if suggestion:
            nearest_slot = f"\nHINT: Some available {state.time + ' ' if has_vague_time else ''}times for you to suggest are: {suggestion}. Offer these choices to the patient!"

        vague_context = ""
        if has_vague_time and state.date:
            vague_context = f"\nThe patient wants a {state.time} slot on {get_readable_date(state.date)}. Suggest the available {state.time} slots from the HINT."

        # When no slots found for a specific date, explain why instead of letting LLM hallucinate
        no_slots_note = ""
        if suggestion is None and state.date:
            rd_note = get_readable_date(state.date)
            try:
                req_d = datetime.datetime.strptime(normalize_date(state.date), "%Y-%m-%d").date()
                day_name_note = req_d.strftime("%A")
            except Exception:
                day_name_note = ""
            # Try to find the next available slots as alternatives
            alt_suggestion = suggest_available_slots(state.reason, count=3)
            alt_hint = ""
            if alt_suggestion:
                alt_hint = f" Suggest these alternatives instead: {alt_suggestion}."
            no_slots_note = f"\nIMPORTANT: We do NOT have any available slots on {rd_note} ({day_name_note}) for this type of appointment. Politely tell the patient we don't have availability on that date and ask if they'd like to try a different day.{alt_hint}\nNEVER say 'fully booked' — just say we don't have availability on that date."

        instruction = f"""CURRENT INSTRUCTION:
The patient needs: '{state.reason}'.{vague_context}
You MUST suggest ONLY the exact slots listed in the HINT below. Do NOT invent or guess any times or day names.
If no HINT is available, ask for their preferred date and time.
MUST end your response with a question mark.{nearest_slot}{no_slots_note}
"""
    elif state.step == 3:
        # CRITICAL: The availability message is DETERMINISTIC, injected directly
        instruction = f"""CURRENT INSTRUCTION:
The patient wanted {rd} at {state.time}, but that is NOT AVAILABLE.

YOU MUST tell the patient EXACTLY this: "{state.availability_error}"
Do NOT change the suggested time. Do NOT make up a different availability.
MUST end your response with a question mark.

Example format: "Unfortunately, that slot is not available. [exact availability error message]. Would that work?"
"""
    elif state.step == 4:
        field_labels = CFG.get_patient_field_labels(required_only=True)
        all_fields = list(CFG.required_patient_fields)
        booking_field_labels = CFG.get_booking_field_labels(required_only=True)
        if booking_field_labels:
            all_fields_list = list(CFG.required_patient_fields) + list(CFG.required_booking_fields)
            combined_labels = f"{field_labels}, {booking_field_labels}"
        else:
            all_fields_list = list(CFG.required_patient_fields)
            combined_labels = field_labels
        field_list = "\n".join([f"{i+1}. Their {f.label}" for i, f in enumerate(all_fields_list)])
        instruction = f"""CURRENT INSTRUCTION:
{rd} at {state.time} IS AVAILABLE for {state.reason}.
Confirm the slot and ask for their {combined_labels} to finalize.
Keep it to 1-2 sentences. MUST end with a question mark.
Example: "{rd} at {state.time} is available! Could I get your {combined_labels} to book this?"
"""
    elif state.step == 5:
        instruction = f"""CURRENT INSTRUCTION:
BOOKING IS CONFIRMED. Say EXACTLY:
"Your appointment is confirmed for {rd} at {state.time} for {state.reason}. Thank you for choosing {CLINIC_NAME}!"
Do NOT ask any further questions. This is the final message.
"""

    return base + "\n" + instruction


# Hardcoded fallback responses for when LLM completely fails validation
FALLBACK_RESPONSES = {
    1: f"Welcome to {CLINIC_NAME}! What {CFG.specialty_label} concern can I help you with today?",
    2: "I'd be happy to help with that! What date and time would work best for you?",
    # 3 and 4 are dynamic, handled in code
    5: None,  # built dynamically
}


async def generate_validated_response(client, chat_history, state, max_retries=5):
    """Generate a response with validation and retry logic.
    Returns the validated response text."""
    logger.info(f"[GENERATE] Starting for step {state.step}")
    for attempt in range(max_retries):
        tone_index = min(state.total_retries + attempt, len(TONES) - 1)
        system_prompt = build_system_prompt(state, tone_index)

        # Build messages: use the system prompt + conversation history (without old system messages)
        msgs = [{"role": "system", "content": system_prompt}]
        for m in chat_history:
            if m["role"] != "system":
                msgs.append({"role": m["role"], "content": m["content"]})

        try:
            resp = await client.chat.completions.create(
                model=CFG.llm_model,
                messages=msgs, max_tokens=CFG.llm_max_tokens_respond, temperature=CFG.llm_temperature_respond,
                timeout=20.0 # 20 second timeout
            )
            raw_text = resp.choices[0].message.content.strip()
            logger.info(f"[GENERATE attempt {attempt+1}] Raw: {raw_text}")

            # Scrub the response
            cleaned = scrub_response(raw_text)

            # Validate
            is_valid, issues = validate_response(cleaned, state)

            if is_valid:
                logger.info(f"[GENERATE] Validated on attempt {attempt+1}")
                return cleaned

            logger.info(f"[GENERATE attempt {attempt+1}] Validation issues: {issues}")

            # Try to fix simple issues
            if "Does not end with a question mark" in str(issues) and state.step in [1, 2, 3, 4]:
                # Append a contextual question
                if state.step == 1:
                    cleaned = cleaned.rstrip('.!') + f" — what {CFG.specialty_label} issue can I help you with?"
                elif state.step == 2:
                    cleaned = cleaned.rstrip('.!') + " — what date and time work best for you?"
                elif state.step == 3:
                    cleaned = cleaned.rstrip('.!') + " — would that alternative time work for you?"
                elif state.step == 4:
                    field_labels = CFG.get_patient_field_labels(required_only=True)
                    cleaned = cleaned.rstrip('.!') + f" — could I get your {field_labels}?"

                # Re-validate after fix
                is_valid2, issues2 = validate_response(cleaned, state)
                if is_valid2:
                    logger.info(f"[GENERATE] Fixed and validated on attempt {attempt+1}")
                    return cleaned

        except Exception as e:
            logger.info(f"[GENERATE attempt {attempt+1}] Error: {e}")
            await asyncio.sleep(0.5)

    # All retries exhausted — use fallback
    logger.info(f"[GENERATE] All {max_retries} attempts failed, using fallback for step {state.step}")
    rd = get_readable_date(state.date)

    if state.is_modification:
        return f"I can only help with booking new appointments. For changes or cancellations, please call our front desk directly at {CFG.clinic_phone}. Would you like to book a new appointment instead?"

    if state.step == 3:
        return f"I'm sorry, that time isn't available. {state.availability_error}"
    elif state.step == 4:
        field_labels = CFG.get_patient_field_labels(required_only=True)
        return f"Great news — {rd} at {state.time} is available for {state.reason}! Could I please get your {field_labels} to finalize the booking?"
    elif state.step == 5:
        return f"Your appointment is confirmed for {rd} at {state.time} for {state.reason}. Thank you for choosing {CLINIC_NAME}!"
    else:
        fb = FALLBACK_RESPONSES.get(state.step)
        if fb:
            return fb
        return f"I'm here to help you book a {CFG.specialty_label} appointment. What can I assist you with?"


# ═══════════════════════════════════════════════════════════════════════════════
# AUDIO
# ═══════════════════════════════════════════════════════════════════════════════

async def generate_audio_base64(sentence: str, request_id: str) -> str:
    filename = f"/tmp/temp_audio_{request_id}.mp3"
    communicate = edge_tts.Communicate(sentence, CFG.assistant_voice, rate=CFG.assistant_voice_rate)
    await communicate.save(filename)
    with open(filename, "rb") as f:
        data = f.read()
    try:
        os.remove(filename)
    except:
        pass
    return base64.b64encode(data).decode('utf-8')


# ═══════════════════════════════════════════════════════════════════════════════
# DB SAVE (Step 5)
# ═══════════════════════════════════════════════════════════════════════════════

def save_booking(state):
    """Save the booking to CSV. Returns True if successful."""
    try:
        # Find or create patient
        pid = _find_existing_patient(state.patient_data)
        if not pid:
            try:
                with open("patients.csv", "r") as pf:
                    lines = pf.readlines()
                    max_id = 0
                    for line in lines[1:]:  # type: ignore[index]
                        if line.strip():
                            parts = line.split(',')
                            if parts[0].strip().isdigit():
                                max_id = max(max_id, int(parts[0].strip()))
                    pid = str(max_id + 1)
                
                # Build the dynamic row values
                row_values = [pid]
                for key in CFG.all_patient_keys:
                    val = state.patient_data.get(key) or ""
                    # quote comma-containing values if necessary
                    if "," in val:
                        val = f'"{val}"'
                    row_values.append(val)
                
                with open("patients.csv", "a") as pf:
                    if not lines[-1].endswith('\n'):
                        pf.write('\n')
                    pf.write(",".join(row_values) + "\n")
            except Exception as e:
                logger.info(f"[SAVE] Adding patient error: {e}")
                return False
        state.patient_id = pid

        # Calculate end time
        try:
            dt_obj = datetime.datetime.strptime(state.time, "%I:%M %p")
            end_time_str = (dt_obj + SLOT_DURATION).strftime("%I:%M %p")
        except:
            end_time_str = state.time

        # Save booking - read first, then write
        with open("bookings.csv", "r") as fr:
            content = fr.read()
        with open("bookings.csv", "a") as f:
            if content and not content.endswith('\n'):
                f.write('\n')
            booking_row = f"{state.date},{state.time},{end_time_str},{state.doctor_id},{state.patient_id},{state.reason}"
            for key in CFG.all_booking_keys:
                val = state.booking_data.get(key) or ""
                if "," in val:
                    val = f'"{val}"'
                booking_row += f",{val}"
            f.write(booking_row + "\n")

        logger.info(f"[SAVE] Booking saved: {state.date} {state.time} doc={state.doctor_id} pid={state.patient_id} reason={state.reason}")

        # Sort bookings by date and time
        try:
            with open("bookings.csv", "r") as f:
                lines = f.readlines()
            if len(lines) > 2:
                header = lines.pop(0)
                data = lines
                def sort_key(line):
                    parts = line.strip().split(',')
                    if len(parts) < 2: return ("9999-99-99", "23:59")
                    d = parts[0].strip()
                    t_str = parts[1].strip()
                    try:
                        t = datetime.datetime.strptime(t_str, "%I:%M %p").time()
                        return (d, t.strftime("%H:%M"))
                    except:
                        return (d, "00:00")
                
                data.sort(key=sort_key)
                with open("bookings.csv", "w") as f:
                    f.write(header)
                    for d_line in data:
                        if d_line.strip():
                            f.write(d_line.strip() + "\n")
        except Exception as se:
            logger.info(f"[SAVE] Sorting error: {se}")

        return True
    except Exception as e:
        logger.info(f"[SAVE] Booking error: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# FALLBACK EXTRACTION - Catches dates/times the LLM misses
# ═══════════════════════════════════════════════════════════════════════════════

def fallback_extract_date_time(text):
    """Fallback extraction of date and time from user text when LLM fails.
    Returns (date_str_or_None, time_str_or_None)."""
    text_lower = text.lower().strip()
    extracted_date = None
    extracted_time = None

    # ── Date extraction ──

    # Relative dates
    if "tomorrow" in text_lower or "tmrw" in text_lower or "tmr" in text_lower:
        extracted_date = normalize_date("tomorrow")
    elif "today" in text_lower:
        extracted_date = normalize_date("today")
    elif "day after tomorrow" in text_lower:
        extracted_date = normalize_date("day after tomorrow")

    # Day of week (full names)
    if not extracted_date:
        days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        for day in days:
            if day in text_lower:
                prefix = "next " if "next" in text_lower else ""
                extracted_date = normalize_date(prefix + day)
                break

    # Day of week (abbreviated: fri, mon, tue, etc.)
    # Note: "sat" and "sun" excluded — they're common English words (past tense of sit, sunshine, etc.)
    if not extracted_date:
        day_abbrevs = {
            "mon": "monday", "tue": "tuesday", "tues": "tuesday",
            "wed": "wednesday", "thu": "thursday", "thur": "thursday",
            "thurs": "thursday", "fri": "friday",
        }
        for abbrev, full in day_abbrevs.items():
            if re.search(r'\b' + abbrev + r'\b', text_lower):
                extracted_date = normalize_date(full)
                break

    # Specific dates: "21 mar", "mar 21", "march 21", "21 march", "3/21"
    if not extracted_date:
        month_pattern = r'(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)'
        # "21 mar" or "21 march"
        match = re.search(r'(\d{1,2})\s+' + month_pattern, text_lower)
        if match:
            extracted_date = normalize_date(f"{match.group(2)} {match.group(1)}")
        else:
            # "mar 21" or "march 21"
            match = re.search(month_pattern + r'\s+(\d{1,2})', text_lower)
            if match:
                extracted_date = normalize_date(f"{match.group(1)} {match.group(2)}")
            else:
                # "3/21" or "3/21/2026"
                match = re.search(r'(\d{1,2})/(\d{1,2})(?:/(\d{4}))?', text_lower)
                if match:
                    date_str = f"{match.group(1)}/{match.group(2)}"
                    if match.group(3):
                        date_str += f"/{match.group(3)}"
                    extracted_date = normalize_date(date_str)

    # ── Time extraction ──

    # Vague times (with typo tolerance: "mornin", "morng", etc.)
    if re.search(r'\bmornin\w*\b', text_lower):
        extracted_time = "morning"
    elif re.search(r'\bafternoo\w*\b', text_lower):
        extracted_time = "afternoon"
    elif re.search(r'\bevenin\w*\b', text_lower):
        extracted_time = "evening"

    # Specific times: "10 AM", "2pm", "10:30 am", "at 4"
    if not extracted_time:
        time_match = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b', text_lower)
        if time_match:
            h = time_match.group(1)
            m = time_match.group(2) or "00"
            ampm = time_match.group(3).upper()
            extracted_time = f"{h}:{m} {ampm}"
        else:
            # "at 4", "at 10"
            time_match = re.search(r'\bat\s+(\d{1,2})(?::(\d{2}))?\b', text_lower)
            if time_match:
                h = int(time_match.group(1))
                m = time_match.group(2) or "00"
                # Dental clinic heuristic: 1-7 → PM, 8-12 → AM
                ampm = "PM" if 1 <= h <= 7 else "AM"
                extracted_time = f"{h}:{m} {ampm}"

    return extracted_date, extracted_time


# ═══════════════════════════════════════════════════════════════════════════════
# WEBSOCKET HANDLER - Main conversation loop
# ═══════════════════════════════════════════════════════════════════════════════

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    api_key = os.getenv("LLM_API_KEY")
    client = AsyncOpenAI(api_key=api_key, base_url=CFG.llm_base_url)

    state = BookingState()
    chat_history: List[Dict[str, str]] = [{"role": "system", "content": build_system_prompt(state)}]
    generation_task: Optional[asyncio.Task[None]] = None

    async def process_message(text: str):
        logger.info(f"\n{'='*60}")
        logger.info(f"USER: {text}")
        logger.info(f"STATE BEFORE: step={state.step}, reason={state.reason}, date={state.date}, time={state.time}")

        # ── STEP 0: Extract structured data from user text ──
        partial: dict = await extract_booking_info(client, chat_history, text, state)  # type: ignore[assignment]
        logger.info(f"EXTRACTED: {partial}")

        # ── STEP 0b: Fallback — catch dates/times the LLM missed or got wrong ──
        fb_date, fb_time = fallback_extract_date_time(text)
        if fb_date:
            if not partial.get("date"):
                partial["date"] = fb_date
                logger.info(f"[FALLBACK] Extracted date from text: {fb_date}")
            elif fb_date != normalize_date(partial["date"]):
                # LLM got the date wrong (e.g. "friday" → Thursday) — override
                logger.info(f"[FALLBACK] Overriding LLM date {partial['date']} → {fb_date}")
                partial["date"] = fb_date
        if fb_time and not partial.get("time"):
            partial["time"] = fb_time
            logger.info(f"[FALLBACK] Extracted time from text: {fb_time}")

        old_step = state.step

        # Save old values BEFORE merging so we can detect changes
        old_reason = state.reason
        old_date = state.date
        old_time = state.time

        # ── STEP 1: Merge extracted fields into state ──
        if partial.get("reason") and is_valid_reason(partial["reason"]):
            state.reason = partial["reason"]
        
        # Dynamically merge all configured patient fields
        for field in CFG.all_patient_keys:
            if partial.get(field):
                state.patient_data[field] = partial[field]

        # Dynamically merge all configured booking extra fields
        for field in CFG.all_booking_keys:
            if partial.get(field):
                state.booking_data[field] = partial[field]

        if "date" in partial:
            val = partial.get("date")
            if val == "REJECTED":
                state.date = None
                state.step = 2
            elif val is not None:
                state.date = normalize_date(val)

        # Fallback date resolution: If time is provided but date is NULL, try to infer from context
        if partial.get("time") and partial.get("time") != "REJECTED" and not partial.get("date") and not state.date:
             if state.step in [2, 3, 4]:
                 now = get_current_time()
                 # Default to today if the time hasn't passed yet, otherwise tomorrow
                 t = parse_time_12h(partial["time"])
                 if t and t > now.time():
                     state.date = now.strftime("%Y-%m-%d")
                 else:
                     state.date = (now + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
                 logger.info(f"[MERGE] Inferred date {state.date} for time {partial.get('time')}")
        if "time" in partial:
            val = partial.get("time")
            if val == "REJECTED":
                state.time = None
                state.step = 2
            elif val is not None:
                state.time = val
        # Only treat as modification if user wants to cancel/reschedule an EXISTING appointment
        # If we're mid-booking (step 2+), user is just changing preferences, not modifying an existing booking
        if partial.get("is_modification", False):
            user_text_lower = text.lower()
            explicit_cancel = any(w in user_text_lower for w in ["cancel", "reschedule", "modify my appointment", "change my appointment", "delete my appointment"])
            if state.step <= 1 and explicit_cancel:
                state.is_modification = True
            elif state.step >= 2:
                # Mid-booking: user is just changing preferences, not modifying existing
                state.is_modification = False
            else:
                state.is_modification = False
        else:
            state.is_modification = False

        # Reset retries if any meaningful field actually changed
        if (state.reason and state.reason != old_reason) or \
           (state.date and state.date != old_date) or \
           (state.time and state.time != old_time):
            state.retries = 0
            state.total_retries = 0
            logger.info("[RETRY] Info changed, resetting retries")

        logger.info(f"STATE MERGED: step={state.step}, reason={state.reason}, date={state.date}, time={state.time}, data={state.patient_data}, is_mod={state.is_modification}")

        # ── STEP 2: State transitions (deterministic, loop to cascade) ──
        if not state.is_modification:
            try:
                max_loops = 5
                for _ in range(max_loops):
                    prev_step = state.step

                    if state.step == 1:
                        if state.reason and is_valid_reason(state.reason):
                            logger.info(f"[TRANSITION] 1→2: reason='{state.reason}'")
                            state.step = 2
                            state.retries = 0

                    elif state.step == 2:
                        if state.date and state.time:
                            if is_vague_time(state.time):
                                # Vague time like "morning" — stay at step 2, prompt will suggest specific slots
                                logger.info(f"[TRANSITION] Step 2: vague time '{state.time}', staying to suggest slots")
                                break

                            # Find the 3 nearest available slots
                            slots = find_nearest_slots(state.reason, state.date, state.time, count=3)
                            state.suggested_slots = slots
                            logger.info(f"[SLOTS] Found {len(slots)} nearest: {format_slots(slots) if slots else 'none'}")

                            if not slots:
                                rd = get_readable_date(state.date)
                                state.availability_error = f"Unfortunately, we have no availability near {rd}. Could you suggest another day?"
                                state.step = 3
                                state.retries = 0
                                logger.info(f"[TRANSITION] 2→3: No slots found")
                            else:
                                # Normalize requested time for comparison
                                req_time_parsed = parse_time_12h(state.time)
                                first_time_parsed = parse_time_12h(slots[0]["time"])
                                first_matches = (slots[0]["date"] == normalize_date(state.date)
                                                 and req_time_parsed and first_time_parsed
                                                 and req_time_parsed == first_time_parsed)

                                if first_matches:
                                    # Exact match — requested slot is available
                                    state.doctor_id = slots[0]["doctor_id"]
                                    state.step = 4
                                    state.retries = 0
                                    logger.info(f"[TRANSITION] 2→4: AVAILABLE (exact match), doc={slots[0]['doctor_id']}")
                                else:
                                    # Not available — show the 3 nearest alternatives
                                    formatted = format_slots(slots)
                                    state.availability_error = f"That slot isn't available. The nearest options are: {formatted}. Which one works for you?"
                                    state.step = 3
                                    state.retries = 0
                                    logger.info(f"[TRANSITION] 2→3: UNAVAILABLE, suggesting: {formatted}")

                    elif state.step == 3:
                        # User responded to slot suggestions — pick one or provide new date/time
                        if partial.get("date"):
                            state.date = normalize_date(partial["date"])
                        if partial.get("time"):
                            state.time = partial["time"]

                        # Check if user agreed to one of the suggested slots
                        user_text_lower = text.lower()
                        agreement_words = ['yes', 'sure', 'ok', 'okay', 'fine', 'works', 'perfect',
                                         'sounds good', 'that works', 'great', 'yeah', 'yep', 'yup',
                                         'first', 'first one', 'that one']
                        user_agreed = any(word in user_text_lower for word in agreement_words)

                        # If user just said "yes"/"sure" without specifying, pick the first suggestion
                        if user_agreed and not partial.get("time") and state.suggested_slots:
                            first = state.suggested_slots[0]
                            state.date = first["date"]
                            state.time = first["time"]
                            logger.info(f"[TRANSITION] 3: User agreed → first suggestion {first['date']} {first['time']}")

                        # Go back to step 2 — the cascade will re-check via find_nearest_slots
                        if state.date and state.time:
                            if is_vague_time(state.time):
                                state.step = 2
                                state.retries = 0
                                logger.info(f"[TRANSITION] 3→2: vague time '{state.time}'")
                                continue
                            state.step = 2
                            state.retries = 0
                            logger.info(f"[TRANSITION] 3→2: re-checking {state.date} {state.time}")
                            continue

                    elif state.step == 4:
                        # Re-verify availability in case the user changed the time in this step
                        slots = find_nearest_slots(state.reason, state.date, state.time, count=3)
                        req_time = parse_time_12h(state.time)
                        first_time = parse_time_12h(slots[0]["time"]) if slots else None
                        still_available = (slots
                                           and slots[0]["date"] == normalize_date(state.date)
                                           and req_time and first_time
                                           and req_time == first_time)

                        if not still_available:
                            state.suggested_slots = slots
                            if slots:
                                formatted = format_slots(slots)
                                state.availability_error = f"That slot is no longer available. Nearest options: {formatted}. Which one works?"
                            else:
                                state.availability_error = "No available slots found near that time. Could you suggest another day?"
                            state.step = 3
                            state.retries = 0
                            logger.info(f"[TRANSITION] 4→3: Slot no longer available")
                        elif state.has_all_required_patient_fields() and state.has_all_required_booking_fields():
                            state.doctor_id = slots[0]["doctor_id"]
                            state.step = 5
                            state.retries = 0
                            logger.info(f"[TRANSITION] 4→5: All required info collected, doc={slots[0]['doctor_id']}")

                    if state.step == prev_step:
                        break
            except Exception as e:
                logger.info(f"[TRANSITION] Error: {e}")

        # ── STEP 3: Handle retries / tone escalation ──
        if state.step == old_step and state.step != 5 and not state.is_modification:
            state.retries += 1
            state.total_retries += 1
            logger.info(f"[RETRY] Step {state.step} retry #{state.retries} (total: {state.total_retries})")
        else:
            state.retries = 0

        logger.info(f"STATE FINAL: step={state.step}, retries={state.retries}")

        # ── STEP 4: Save to DB if we just reached step 5 ──
        if state.step == 5 and not state.saved:
            success = save_booking(state)
            state.saved = success
            if not success:
                logger.info("[SAVE] Failed to save booking!")

        # ── STEP 5: Update chat history with user message ──
        chat_history.append({"role": "user", "content": text})

        # ── STEP 6: Generate validated response ──
        is_greeting = False
        if state.step == 1 and len(chat_history) == 2 and text.strip().lower() == "hello":
            # Skip LLM to make the first greeting immediate
            response_text = f"Hello! I'm {ASSISTANT_NAME}, the AI receptionist at {CLINIC_NAME}. How can I help you today?"
            is_greeting = True
        elif state.step == 3 and state.availability_error:
            # Hardcoded step 3 — relay slot suggestions directly, no LLM hallucination
            response_text = state.availability_error
        elif state.step == 4 and old_step != 4:
            # Hardcoded step 4 first entry — confirm the available slot
            rd = get_readable_date(state.date)
            try:
                req_d = datetime.datetime.strptime(normalize_date(state.date), "%Y-%m-%d").date()
                day_name = req_d.strftime("%A")
            except Exception:
                day_name = ""
            field_labels = CFG.get_patient_field_labels(required_only=True)
            response_text = f"{day_name} {rd} at {state.time} is available! Could I get your {field_labels} to book this?"
        elif state.step == 5:
            # Hardcoded step 5 — prevents LLM from hallucinating extra content
            rd = get_readable_date(state.date)
            response_text = f"Your appointment is confirmed for {rd} at {state.time} for {state.reason}. Thank you for choosing {CLINIC_NAME}!"
        else:
            response_text = await generate_validated_response(client, chat_history, state)

        # ── STEP 7: Send response via WebSocket (streaming simulation + audio) ──
        try:
            if is_greeting:
                # For the first message, immediately send text so the user doesn't stare at loading dots
                await websocket.send_json({"type": "text_delta", "content": response_text})

            # Generate audio FIRST to prevent text-to-speech lag for normal messages
            req_id = str(uuid.uuid4())
            b64_audio = await generate_audio_base64(response_text, req_id)  # type: ignore[arg-type]

            if not is_greeting:
                # Send text and audio together for the rest of the conversation
                await websocket.send_json({"type": "text_delta", "content": response_text})
                
            await websocket.send_json({"type": "audio", "data": b64_audio})

            # Record in history
            chat_history.append({"role": "assistant", "content": response_text})  # type: ignore[arg-type]
            # Update system prompt for next turn
            chat_history[0]["content"] = build_system_prompt(state, min(state.total_retries, len(TONES) - 1))

            await websocket.send_json({"type": "generation_done"})
        except Exception as e:
            logger.info(f"[WS] Send error: {e}")

    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            if message.get("type") == "user_input":
                if generation_task and not generation_task.done():  # type: ignore[union-attr]
                    generation_task.cancel()
                    try: await generation_task
                    except asyncio.CancelledError: pass
                user_text = message.get("text")
                generation_task = asyncio.create_task(process_message(user_text))  # type: ignore[assignment]
            elif message.get("type") == "interrupt":
                if generation_task and not generation_task.done():  # type: ignore[union-attr]
                    generation_task.cancel()  # type: ignore[union-attr]
    except WebSocketDisconnect:
        if generation_task and not generation_task.done():  # type: ignore[union-attr]
            generation_task.cancel()  # type: ignore[union-attr]

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(content=b"", media_type="image/x-icon")

if __name__ == "__main__":
    import uvicorn  # type: ignore
    uvicorn.run(app, host="0.0.0.0", port=8000)
