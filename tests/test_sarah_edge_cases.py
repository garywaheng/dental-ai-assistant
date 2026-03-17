"""
COMPREHENSIVE SARAH EDGE CASE TESTS
=====================================
Tests every hallucination-prone scenario with strict validation.

Covers:
- Availability DB reads (booked vs free slots)
- Response format (questions, doctor names, conciseness)
- DB writes (bookings.csv, patients.csv)
- Relative date resolution
- Working hours enforcement
- Modification blocking
- Edge case inputs (empty, gibberish, off-topic)

Run: python test_sarah_edge_cases.py
Requires: server running on ws://localhost:8000/ws
"""
import asyncio
import json
import os
import sys
import shutil
import datetime
import re
import websockets

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(PROJECT_DIR)

# ── Colors ──
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

TOTAL_PASS = 0
TOTAL_FAIL = 0
TOTAL_WARN = 0

WS_URL = "ws://localhost:8000/ws"


# ── Helpers ──

async def send(ws, text, timeout=30):
    """Send user message and collect Sarah's full response."""
    await ws.send(json.dumps({"type": "user_input", "text": text}))
    resp = ""
    try:
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            data = json.loads(raw)
            if data["type"] == "text_delta":
                resp += data.get("content", "")
            elif data["type"] == "generation_done":
                break
            elif data["type"] == "audio":
                pass
    except asyncio.TimeoutError:
        pass
    return resp.strip()


def check(label, condition, response="", warn_only=False):
    global TOTAL_PASS, TOTAL_FAIL, TOTAL_WARN
    if condition:
        print(f"    {GREEN}✓ PASS{RESET} {label}")
        TOTAL_PASS += 1
    elif warn_only:
        print(f"    {YELLOW}⚠ WARN{RESET} {label}")
        if response:
            print(f"         {DIM}→ {response[:150]}{RESET}")
        TOTAL_WARN += 1
    else:
        print(f"    {RED}✗ FAIL{RESET} {label}")
        if response:
            print(f"         {DIM}→ {response[:150]}{RESET}")
        TOTAL_FAIL += 1


def has_any(text, keywords):
    t = text.lower()
    return any(k.lower() in t for k in keywords)


def has_question(text):
    return "?" in text


def no_doctor_names(text):
    """Ensure no doctor names or IDs are leaked."""
    forbidden = ["dr. smith", "dr. jones", "dr. davis", "smith", "jones", "davis",
                  "doctor 1", "doctor 2", "doctor 3", "doctor id"]
    t = text.lower()
    return not any(f in t for f in forbidden)


def no_asterisks(text):
    return "*" not in text


def backup():
    for f in ["bookings.csv", "patients.csv"]:
        src = os.path.join(PROJECT_DIR, f)
        if os.path.exists(src):
            shutil.copy2(src, src + ".testbak")

def restore():
    for f in ["bookings.csv", "patients.csv"]:
        bak = os.path.join(PROJECT_DIR, f + ".testbak")
        if os.path.exists(bak):
            shutil.copy2(bak, os.path.join(PROJECT_DIR, f))
            os.remove(bak)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 1: BOOKED SLOT — Must say unavailable
# ═══════════════════════════════════════════════════════════════════════════════
async def test_booked_slot_11am_march12():
    """March 12 11 AM is BOOKED (Doctor 1, Patient 1). Sarah MUST say unavailable."""
    print(f"\n{BOLD}[1] BOOKED SLOT: March 12, 11 AM (General){RESET}")

    async with websockets.connect(WS_URL) as ws:
        r1 = await send(ws, "I need a checkup")
        check("Step 1: Responds and asks for details", len(r1) > 5, r1)
        check("Step 1: Has question mark", has_question(r1), r1)

        r2 = await send(ws, "March 12 at 11 AM")
        check("CRITICAL: Says 11 AM March 12 is NOT available",
              has_any(r2, ["not available", "taken", "booked", "unavailable", "unfortunately", "already", "isn't available"]),
              r2)
        check("Suggests alternative time", has_any(r2, ["suggest", "alternative", "instead", "how about", "open", "available", ":"]), r2, warn_only=True)
        check("Ends with question", has_question(r2), r2)
        check("No doctor names leaked", no_doctor_names(r2), r2)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 2: FREE SLOT — Must say available
# ═══════════════════════════════════════════════════════════════════════════════
async def test_free_slot_2pm_march12():
    """March 12 2 PM is FREE for Doctor 1. Sarah MUST say available."""
    print(f"\n{BOLD}[2] FREE SLOT: March 12, 2 PM (General){RESET}")

    async with websockets.connect(WS_URL) as ws:
        await send(ws, "I need a checkup")
        r = await send(ws, "March 12 at 2 PM")
        check("CRITICAL: Says 2 PM March 12 IS available",
              has_any(r, ["available", "works", "great", "perfect", "name", "phone", "email", "book", "confirm"]),
              r)
        check("Does NOT say booked/taken",
              not has_any(r, ["taken", "booked", "unavailable", "not available"]),
              r)
        check("Asks for patient info", has_any(r, ["name", "phone", "email", "information", "details"]), r, warn_only=True)
        check("Ends with question", has_question(r), r)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 3: COMPLETE BOOKING FLOW — Full happy path
# ═══════════════════════════════════════════════════════════════════════════════
async def test_full_booking_flow():
    """Complete booking: reason → date/time → patient info → confirmation + DB write."""
    print(f"\n{BOLD}[3] FULL BOOKING FLOW (happy path){RESET}")

    today = datetime.date.today()
    future = today + datetime.timedelta(days=7)
    future_str = future.strftime("%B %d")
    future_iso = future.strftime("%Y-%m-%d")

    async with websockets.connect(WS_URL) as ws:
        # Step 1: Reason
        r1 = await send(ws, "I have a toothache")
        check("Step 1→2: Asks for date/time after reason",
              has_any(r1, ["date", "time", "when", "prefer", "schedule", "available"]), r1)
        check("Has question", has_question(r1), r1)

        # Step 2: Date + time (free slot)
        r2 = await send(ws, f"{future_str} at 10 AM")
        check("Step 2→4: Confirms availability or asks for info",
              has_any(r2, ["available", "works", "name", "phone", "email", "great", "perfect", "book"]), r2)

        # Step 4: Patient info
        r3 = await send(ws, "John TestFlow, 555-0000, testflow@test.com")

        # Allow one more exchange if Sarah asked again
        if "?" in r3 and not has_any(r3, ["confirmed", "booked", "scheduled"]):
            r3 = await send(ws, "Yes, please confirm")

        check("Step 5: Confirms booking",
              has_any(r3, ["confirmed", "booked", "scheduled", "thank"]), r3)
        check("Mentions Bright Smiles", has_any(r3, ["bright smiles"]), r3, warn_only=True)
        check("No asterisks", no_asterisks(r3), r3)
        check("No doctor names", no_doctor_names(r3), r3)

    # Check DB writes
    await asyncio.sleep(2)
    bookings = open(os.path.join(PROJECT_DIR, "bookings.csv")).read()
    patients = open(os.path.join(PROJECT_DIR, "patients.csv")).read()

    check("DB: Booking date in bookings.csv", future_iso in bookings, bookings[-200:])
    check("DB: Patient name in patients.csv",
          "testflow" in patients.lower() or "john" in patients.lower(),
          patients[-200:], warn_only=True)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 4: WORKING HOURS — Before opening (7 AM for General 9 AM-5 PM)
# ═══════════════════════════════════════════════════════════════════════════════
async def test_before_working_hours():
    """7 AM is before General doctor's 9 AM start. Must reject or suggest 9 AM."""
    print(f"\n{BOLD}[4] WORKING HOURS: 7 AM (before 9 AM start){RESET}")

    async with websockets.connect(WS_URL) as ws:
        await send(ws, "I have a toothache")
        r = await send(ws, "March 16 at 7 AM")
        check("CRITICAL: Rejects or redirects 7 AM",
              has_any(r, ["not available", "9", "working hours", "start", "earliest", "open", "unfortunately", "isn't available"]),
              r)
        check("Does NOT confirm 7 AM as available",
              not has_any(r, ["7 am is available", "7:00 am works"]),
              r)
        check("Ends with question", has_question(r), r)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 5: WORKING HOURS — After closing (6 PM for General 9 AM-5 PM)
# ═══════════════════════════════════════════════════════════════════════════════
async def test_after_working_hours():
    """6 PM is after General doctor's 5 PM end. Must reject."""
    print(f"\n{BOLD}[5] WORKING HOURS: 6 PM (after 5 PM close){RESET}")

    async with websockets.connect(WS_URL) as ws:
        await send(ws, "I need a cleaning")
        r = await send(ws, "March 16 at 6 PM")
        check("CRITICAL: Rejects 6 PM",
              has_any(r, ["not available", "5", "close", "end", "working hours", "unfortunately", "isn't available", "last"]),
              r)
        check("Ends with question", has_question(r), r)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 6: ORTHODONTICS ROUTING — Braces at 9 AM (Ortho starts 10 AM)
# ═══════════════════════════════════════════════════════════════════════════════
async def test_ortho_before_hours():
    """Braces → Orthodontics (Doctor 2, 10 AM-6 PM). 9 AM must be rejected."""
    print(f"\n{BOLD}[6] SPECIALTY ROUTING: Braces at 9 AM (Ortho starts 10 AM){RESET}")

    async with websockets.connect(WS_URL) as ws:
        await send(ws, "I need braces")
        r = await send(ws, "March 16 at 9 AM")
        check("CRITICAL: Rejects 9 AM for orthodontics",
              has_any(r, ["not available", "10", "working hours", "start", "earliest", "unfortunately", "isn't available"]),
              r)
        check("Ends with question", has_question(r), r)
        check("No doctor names", no_doctor_names(r), r)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 7: QUESTION MARK ENFORCEMENT (Steps 1-4 must end with ?)
# ═══════════════════════════════════════════════════════════════════════════════
async def test_question_mark_enforcement():
    """Every response in steps 1-4 must end with a question mark."""
    print(f"\n{BOLD}[7] QUESTION MARK ENFORCEMENT (Steps 1-4){RESET}")

    async with websockets.connect(WS_URL) as ws:
        r1 = await send(ws, "hi")
        check("Step 1 greeting: Has '?'", has_question(r1), r1)

        r2 = await send(ws, "I have a cavity")
        check("Step 2 after reason: Has '?'", has_question(r2), r2)

        r3 = await send(ws, "March 15 at 10 AM")
        check("Step 3/4 after date: Has '?'", has_question(r3), r3)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 8: DOCTOR NAME HIDING (across all scenarios)
# ═══════════════════════════════════════════════════════════════════════════════
async def test_doctor_name_hiding():
    """Sarah must NEVER mention Dr. Smith/Jones/Davis or doctor IDs."""
    print(f"\n{BOLD}[8] DOCTOR NAME / ID HIDING{RESET}")

    scenarios = [
        ("I need a cleaning", "March 18 at 10 AM"),
        ("I need braces", "March 18 at 11 AM"),
        ("My child needs a checkup", "March 18 at 9 AM"),
    ]
    for reason, dt in scenarios:
        async with websockets.connect(WS_URL) as ws:
            r1 = await send(ws, reason)
            r2 = await send(ws, dt)
            combined = r1 + " " + r2
            check(f"No doctor names in '{reason}' flow", no_doctor_names(combined), combined)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 9: NO ASTERISKS (across emotional inputs)
# ═══════════════════════════════════════════════════════════════════════════════
async def test_no_asterisks_emotional():
    """Sarah must never use *action* text even with emotional inputs."""
    print(f"\n{BOLD}[9] NO ASTERISKS / ACTION TEXT{RESET}")

    prompts = [
        "I'm really scared of dentists",
        "I'm in so much pain right now",
        "Thank you so much!",
        "This is an emergency, my tooth broke!",
    ]
    for prompt in prompts:
        async with websockets.connect(WS_URL) as ws:
            r = await send(ws, prompt)
            check(f"No asterisks: '{prompt[:40]}'", no_asterisks(r), r)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 10: CONCISENESS (max 3 sentences)
# ═══════════════════════════════════════════════════════════════════════════════
async def test_conciseness():
    """Responses must be 1-2 sentences, not paragraphs."""
    print(f"\n{BOLD}[10] RESPONSE CONCISENESS{RESET}")

    async with websockets.connect(WS_URL) as ws:
        r = await send(ws, "I have a toothache")
        sentence_count = len(re.findall(r'[.!?]+', r))
        check(f"Concise ({sentence_count} sentences, ≤ 4)", sentence_count <= 4, r)
        check(f"Not too long ({len(r)} chars, < 300)", len(r) < 300, r)

    async with websockets.connect(WS_URL) as ws:
        r = await send(ws, "hi")
        check(f"Greeting concise ({len(r)} chars, < 200)", len(r) < 200, r)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 11: MODIFICATION BLOCKING — Cancel/reschedule must be declined
# ═══════════════════════════════════════════════════════════════════════════════
async def test_modification_blocking():
    """Requests to change/cancel/reschedule should be politely declined."""
    print(f"\n{BOLD}[11] MODIFICATION BLOCKING{RESET}")

    modification_inputs = [
        "I want to cancel my appointment",
        "Can I reschedule my booking?",
        "I need to change my appointment time",
        "Delete my appointment please",
    ]
    for inp in modification_inputs:
        async with websockets.connect(WS_URL) as ws:
            r = await send(ws, inp)
            check(f"Declines modification: '{inp[:40]}'",
                  has_any(r, ["can't", "cannot", "only book", "new", "front desk", "call",
                              "unable", "not able", "don't handle", "booking new"]),
                  r)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 12: PAST DATE — Must reject
# ═══════════════════════════════════════════════════════════════════════════════
async def test_past_date():
    """March 1 2026 is in the past (today is March 11). Must reject."""
    print(f"\n{BOLD}[12] PAST DATE: March 1{RESET}")

    async with websockets.connect(WS_URL) as ws:
        await send(ws, "I need a cleaning")
        r = await send(ws, "March 1 at 10 AM")
        check("CRITICAL: Addresses past date",
              has_any(r, ["past", "already", "passed", "future", "another", "different", "earlier"]),
              r)
        check("Ends with question", has_question(r), r)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 13: RELATIVE DATE — "tomorrow"
# ═══════════════════════════════════════════════════════════════════════════════
async def test_relative_date_tomorrow():
    """'Tomorrow at 10 AM' should resolve to the correct date."""
    print(f"\n{BOLD}[13] RELATIVE DATE: 'tomorrow'{RESET}")

    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    tomorrow_str = tomorrow.strftime("%B %d").replace(" 0", " ")

    async with websockets.connect(WS_URL) as ws:
        await send(ws, "I have a cavity")
        r = await send(ws, "Can I come tomorrow at 10 AM?")
        check(f"Resolves 'tomorrow' ({tomorrow_str})",
              has_any(r, [tomorrow_str, str(tomorrow.day), tomorrow.strftime("%A"), "tomorrow"]),
              r, warn_only=True)
        # Most importantly, responds sensibly
        check("Responds meaningfully", len(r) > 10, r)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 14: RELATIVE DATE — "this Friday"
# ═══════════════════════════════════════════════════════════════════════════════
async def test_relative_date_friday():
    """'This Friday at 11 AM' should resolve correctly."""
    print(f"\n{BOLD}[14] RELATIVE DATE: 'this Friday'{RESET}")

    today = datetime.date.today()
    days_until_friday = (4 - today.weekday()) % 7
    if days_until_friday == 0:
        days_until_friday = 7
    friday = today + datetime.timedelta(days=days_until_friday)

    async with websockets.connect(WS_URL) as ws:
        await send(ws, "I need a checkup")
        r = await send(ws, "How about this Friday at 11 AM?")
        check(f"Resolves 'this Friday' (day {friday.day})",
              has_any(r, [str(friday.day), "Friday", friday.strftime("%B")]),
              r, warn_only=True)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 15: GIBBERISH / EMPTY INPUT
# ═══════════════════════════════════════════════════════════════════════════════
async def test_gibberish_and_empty():
    """Gibberish and empty inputs should be handled gracefully."""
    print(f"\n{BOLD}[15] GIBBERISH & EMPTY INPUT{RESET}")

    async with websockets.connect(WS_URL) as ws:
        r = await send(ws, "asdfghjkl")
        check("Handles gibberish", len(r) > 5 and not has_any(r, ["error", "crash"]), r)
        check("Ends with question (still step 1)", has_question(r), r)

    async with websockets.connect(WS_URL) as ws:
        r = await send(ws, "")
        check("Handles empty message", len(r) > 0, r, warn_only=True)

    async with websockets.connect(WS_URL) as ws:
        r = await send(ws, "!@#$%^&()")
        check("Handles special characters", len(r) > 5, r)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 16: NON-DENTAL INPUT — weather, car, etc.
# ═══════════════════════════════════════════════════════════════════════════════
async def test_non_dental_input():
    """Off-topic inputs should be redirected to dental topics."""
    print(f"\n{BOLD}[16] NON-DENTAL INPUT{RESET}")

    async with websockets.connect(WS_URL) as ws:
        r = await send(ws, "What's the weather like?")
        check("Redirects weather to dental", has_any(r, ["dental", "teeth", "help", "appointment", "visit", "clinic", "bright smiles", "brings"]), r)

    async with websockets.connect(WS_URL) as ws:
        r = await send(ws, "Can you help me with my car?")
        check("Redirects car question", has_any(r, ["dental", "teeth", "clinic", "help"]), r, warn_only=True)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 17: EXISTING PATIENT — Should not create new profile
# ═══════════════════════════════════════════════════════════════════════════════
async def test_existing_patient():
    """John Doe (patient 1) already exists. Should use existing record."""
    print(f"\n{BOLD}[17] EXISTING PATIENT: John Doe{RESET}")

    async with websockets.connect(WS_URL) as ws:
        await send(ws, "I need a cleaning")
        await send(ws, "March 16 at 10 AM")
        r = await send(ws, "My name is John Doe, phone 555-0101, email john@example.com")

        # Allow follow-up
        if "?" in r and not has_any(r, ["confirmed", "booked"]):
            r = await send(ws, "Yes, please confirm")

        check("Does NOT create new profile",
              not has_any(r, ["create new", "new profile", "new patient"]), r)

    await asyncio.sleep(2)
    # Verify no duplicate John Doe created
    patients = open(os.path.join(PROJECT_DIR, "patients.csv")).read()
    john_count = patients.lower().count("john doe")
    check(f"Only 1 'John Doe' in patients.csv (found {john_count})", john_count == 1, patients[-200:])


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 18: BOOKED SLOT March 14 9 AM (Pediatric Doctor 3)
# ═══════════════════════════════════════════════════════════════════════════════
async def test_booked_slot_march14_9am():
    """March 14 9 AM is booked for Doctor 3. General toothache → Doctor 1 (he's free at 9 AM March 14).
    BUT March 14 10 AM is booked for Doctor 1. So March 14 at 10 AM for general should be unavailable."""
    print(f"\n{BOLD}[18] BOOKED SLOT: March 14, 10 AM (General Doctor 1){RESET}")

    async with websockets.connect(WS_URL) as ws:
        await send(ws, "I have a toothache")
        r = await send(ws, "March 14 at 10 AM")
        check("CRITICAL: March 14 10 AM is NOT available for General (Doctor 1 is booked 10-11)",
              has_any(r, ["not available", "taken", "booked", "unavailable", "unfortunately", "already", "isn't available", "alternative"]),
              r)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 19: ALL INFO IN ONE MESSAGE
# ═══════════════════════════════════════════════════════════════════════════════
async def test_all_info_one_message():
    """When patient gives name/phone/email in one message, confirm immediately."""
    print(f"\n{BOLD}[19] SINGLE MESSAGE → IMMEDIATE CONFIRMATION{RESET}")

    async with websockets.connect(WS_URL) as ws:
        await send(ws, "I need a cleaning")
        await send(ws, "March 18 at 11 AM")
        r = await send(ws, "My name is Bob Test, phone 555-1234, email bob@test.com")

        if "?" in r and not has_any(r, ["confirmed", "booked"]):
            r = await send(ws, "Yes confirm")

        check("Confirmed in response", has_any(r, ["confirmed", "booked", "scheduled", "thank"]), r)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 20: EMPATHETIC RESPONSES
# ═══════════════════════════════════════════════════════════════════════════════
async def test_empathy():
    """Sarah should show empathy for scared/in-pain patients."""
    print(f"\n{BOLD}[20] EMPATHETIC RESPONSES{RESET}")

    async with websockets.connect(WS_URL) as ws:
        r = await send(ws, "I'm really scared, I hate going to the dentist")
        check("Shows empathy to scared patient",
              has_any(r, ["understand", "help", "worry", "comfort", "safe", "okay", "sorry", "concern", "anxiety", "nervous", "here for"]),
              r)

    async with websockets.connect(WS_URL) as ws:
        r = await send(ws, "I'm in terrible pain, please help me")
        check("Shows urgency for pain",
              has_any(r, ["sorry", "hear", "pain", "help", "soon", "understand"]),
              r)
        check("Still moves toward booking", has_question(r), r, warn_only=True)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 21: PROFESSIONALISM WITH RUDE INPUT
# ═══════════════════════════════════════════════════════════════════════════════
async def test_professionalism():
    """Sarah stays professional with rude/hostile input."""
    print(f"\n{BOLD}[21] PROFESSIONALISM{RESET}")

    async with websockets.connect(WS_URL) as ws:
        r = await send(ws, "This clinic sucks, you're terrible")
        check("Stays professional", has_any(r, ["sorry", "help", "understand", "apologize", "assist"]) and no_asterisks(r), r)
        check("Does not respond rudely", not has_any(r, ["rude", "shut up", "leave", "go away"]), r)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 22: AMBIGUOUS TIME — "sometime next week" / "morning"
# ═══════════════════════════════════════════════════════════════════════════════
async def test_ambiguous_time():
    """Vague time inputs should prompt for clarification."""
    print(f"\n{BOLD}[22] AMBIGUOUS TIME INPUT{RESET}")

    async with websockets.connect(WS_URL) as ws:
        await send(ws, "I need a cleaning")
        r = await send(ws, "sometime next week")
        check("Asks for specific date/time",
              has_any(r, ["specific", "time", "date", "prefer", "which", "when", "what", "particular"]),
              r, warn_only=True)

    async with websockets.connect(WS_URL) as ws:
        await send(ws, "I need a cleaning")
        r = await send(ws, "morning")
        check("Asks for specific date when only 'morning' given",
              has_any(r, ["date", "which", "what day", "when", "specific"]),
              r, warn_only=True)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 23: GREETING VARIATIONS
# ═══════════════════════════════════════════════════════════════════════════════
async def test_greeting_variations():
    """Different greetings should all guide toward dental issue."""
    print(f"\n{BOLD}[23] GREETING VARIATIONS{RESET}")

    greetings = ["hi", "Good morning!", "yo", "hey there", "Hello, how are you?"]
    for g in greetings:
        async with websockets.connect(WS_URL) as ws:
            r = await send(ws, g)
            check(f"Responds to '{g}'", len(r) > 5, r)
            check(f"Asks about dental reason after '{g}'",
                  has_any(r, ["dental", "help", "reason", "visit", "today", "assist", "appointment", "what", "how can"]),
                  r)
            check(f"Ends with question after '{g}'", has_question(r), r)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 24: REASON UNDERSTANDING (various dental issues)
# ═══════════════════════════════════════════════════════════════════════════════
async def test_reason_understanding():
    """Sarah should understand various dental reasons and advance to step 2."""
    print(f"\n{BOLD}[24] REASON UNDERSTANDING{RESET}")

    reasons = [
        "I have a toothache",
        "My gums are bleeding",
        "I need braces",
        "I want a teeth cleaning",
        "My kid needs a checkup",
        "I have a cavity",
        "My tooth is really sensitive to cold",
    ]
    for reason in reasons:
        async with websockets.connect(WS_URL) as ws:
            await send(ws, "hi")
            r = await send(ws, reason)
            check(f"After '{reason[:30]}…' → asks for date/time",
                  has_any(r, ["time", "date", "when", "prefer", "schedule", "available", "age", "concern"]),
                  r)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 25: BOOKING CONFIRMATION FORMAT
# ═══════════════════════════════════════════════════════════════════════════════
async def test_confirmation_format():
    """Final confirmation should include date, time, reason, and thank you."""
    print(f"\n{BOLD}[25] CONFIRMATION FORMAT{RESET}")

    async with websockets.connect(WS_URL) as ws:
        await send(ws, "I need a cleaning")
        await send(ws, "March 16 at 10 AM")
        r = await send(ws, "Yes. My name is Format Test, phone 555-4444, email format@test.com")

        if "?" in r and not has_any(r, ["confirmed", "booked"]):
            r = await send(ws, "Yes, please confirm")

        check("Mentions date", has_any(r, ["march 16", "March 16"]), r)
        check("Mentions time", has_any(r, ["10", "10:00"]), r)
        check("Mentions reason", has_any(r, ["cleaning", "clean"]), r)
        check("Includes thank you", has_any(r, ["thank"]), r)
        check("Mentions Bright Smiles", has_any(r, ["bright smiles"]), r, warn_only=True)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 26: NO "HOLD ON" / "LET ME CHECK" (Forbidden phrases)
# ═══════════════════════════════════════════════════════════════════════════════
async def test_no_hold_phrases():
    """Sarah should never say 'hold on', 'let me check', etc."""
    print(f"\n{BOLD}[26] NO 'HOLD ON' / 'LET ME CHECK' PHRASES{RESET}")

    async with websockets.connect(WS_URL) as ws:
        await send(ws, "I have a toothache")
        r = await send(ws, "March 14 at 11 AM")
        check("No 'hold on' / 'let me check' in availability response",
              not has_any(r, ["hold on", "let me check", "wait while", "one moment while", "checking"]),
              r)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 27: ALTERNATIVE SLOT SUGGESTION
# ═══════════════════════════════════════════════════════════════════════════════
async def test_alternative_slot():
    """When a slot is taken, Sarah must suggest a specific alternative time."""
    print(f"\n{BOLD}[27] ALTERNATIVE SLOT SUGGESTION{RESET}")

    # March 12, 11-12 is booked for Doctor 1
    async with websockets.connect(WS_URL) as ws:
        await send(ws, "I need a checkup")
        r = await send(ws, "March 12 at 11 AM")
        check("Suggests alternative time",
              has_any(r, ["suggest", "alternative", "instead", "how about", "open", "available", ":"]),
              r)
        check("Mentions a specific time", bool(re.search(r'\d{1,2}(:\d{2})?\s*(AM|PM|am|pm)', r)), r, warn_only=True)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 28: CONVERSATION FLOW ORDER
# ═══════════════════════════════════════════════════════════════════════════════
async def test_flow_order():
    """Sarah must follow: reason → date/time → availability → info → confirm."""
    print(f"\n{BOLD}[28] CONVERSATION FLOW ORDER{RESET}")

    async with websockets.connect(WS_URL) as ws:
        # Step 1:
        r = await send(ws, "I want to book an appointment")
        check("Step 1: Asks for reason", has_any(r, ["reason", "what", "bring", "issue", "help", "problem", "visit"]), r)

        # Step 2:
        r = await send(ws, "Toothache")
        check("Step 2: Asks for date/time", has_any(r, ["time", "date", "when", "prefer"]), r)

        # Step 3/4:
        r = await send(ws, "March 18 at 10 AM")
        check("Step 3/4: Responds about availability",
              has_any(r, ["available", "works", "taken", "name", "phone", "email", "march"]), r)

        # Step 4:
        if has_any(r, ["name", "phone", "email", "information"]):
            r = await send(ws, "Flow Test, 555-0000, flow@test.com")
        else:
            r = await send(ws, "Yes, that works. Flow Test, 555-0000, flow@test.com")

        if not has_any(r, ["confirmed", "booked", "scheduled", "thank"]):
            r = await send(ws, "Yes, please confirm")

        check("Step 5: Final confirmation", has_any(r, ["confirmed", "booked", "scheduled", "thank"]), r)


# ═══════════════════════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════════════════════

async def run_all():
    global TOTAL_PASS, TOTAL_FAIL, TOTAL_WARN

    print(f"\n{BOLD}{'#' * 65}")
    print(f"  SARAH DENTAL AI — COMPREHENSIVE EDGE CASE TESTS")
    print(f"  Server: {WS_URL}")
    print(f"  Date: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Tests: 28 scenarios")
    print(f"{'#' * 65}{RESET}")

    backup()

    tests = [
        test_booked_slot_11am_march12,        # 1
        test_free_slot_2pm_march12,            # 2
        test_full_booking_flow,                # 3
        test_before_working_hours,             # 4
        test_after_working_hours,              # 5
        test_ortho_before_hours,               # 6
        test_question_mark_enforcement,        # 7
        test_doctor_name_hiding,               # 8
        test_no_asterisks_emotional,           # 9
        test_conciseness,                      # 10
        test_modification_blocking,            # 11
        test_past_date,                        # 12
        test_relative_date_tomorrow,           # 13
        test_relative_date_friday,             # 14
        test_gibberish_and_empty,              # 15
        test_non_dental_input,                 # 16
        test_existing_patient,                 # 17
        test_booked_slot_march14_9am,          # 18
        test_all_info_one_message,             # 19
        test_empathy,                          # 20
        test_professionalism,                  # 21
        test_ambiguous_time,                   # 22
        test_greeting_variations,              # 23
        test_reason_understanding,             # 24
        test_confirmation_format,              # 25
        test_no_hold_phrases,                  # 26
        test_alternative_slot,                 # 27
        test_flow_order,                       # 28
    ]

    for test_fn in tests:
        try:
            restore()
            backup()
            await test_fn()
        except Exception as e:
            print(f"    {RED}✗ CRASH{RESET} {test_fn.__name__}: {e}")
            import traceback
            traceback.print_exc()
            TOTAL_FAIL += 1

    restore()

    print(f"\n{BOLD}{'═' * 65}")
    total = TOTAL_PASS + TOTAL_FAIL + TOTAL_WARN
    pct = (TOTAL_PASS / total * 100) if total > 0 else 0
    print(f"  RESULTS: {GREEN}{TOTAL_PASS} passed{RESET} ({pct:.0f}%), {RED}{TOTAL_FAIL} failed{RESET}, {YELLOW}{TOTAL_WARN} warnings{RESET} ({total} total)")
    print(f"{'═' * 65}{RESET}")

    return TOTAL_FAIL == 0


if __name__ == "__main__":
    success = asyncio.run(run_all())
    sys.exit(0 if success else 1)
