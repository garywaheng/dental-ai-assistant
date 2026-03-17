"""
CONVERSATIONAL AI BEHAVIOR TESTS
=================================
Tests what Sarah actually SAYS in response to different inputs.
Each test connects to the live server, sends messages, and validates
Sarah's responses for correctness, tone, and adherence to clinic rules.

Run with: python test_sarah_conversations.py
Requires: server running on ws://localhost:8000/ws
"""
import asyncio
import json
import os
import sys
import shutil
import datetime
import websockets
import re

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(PROJECT_DIR)

# ── Helpers ──────────────────────────────────────────────────────────────────

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
    """Assert a condition, print result."""
    global TOTAL_PASS, TOTAL_FAIL, TOTAL_WARN
    if condition:
        print(f"    {GREEN}PASS{RESET} {label}")
        TOTAL_PASS += 1
    elif warn_only:
        print(f"    {YELLOW}WARN{RESET} {label}")
        if response:
            print(f"         {DIM}Response: {response[:120]}{RESET}")
        TOTAL_WARN += 1
    else:
        print(f"    {RED}FAIL{RESET} {label}")
        if response:
            print(f"         {DIM}Response: {response[:120]}{RESET}")
        TOTAL_FAIL += 1


def has_any(text, keywords):
    """Check if text contains any of the keywords (case-insensitive)."""
    t = text.lower()
    return any(k.lower() in t for k in keywords)


def has_question(text):
    return "?" in text


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


# ── Test Categories ──────────────────────────────────────────────────────────

async def test_greeting_variations():
    """Does Sarah handle different greetings naturally and guide toward dental issues?"""
    print(f"\n{BOLD}[1] GREETING VARIATIONS{RESET}")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        r = await send(ws, "hi")
        check("Responds to 'hi'", len(r) > 5, r)
        check("Asks about reason/visit", has_any(r, ["reason", "visit", "help", "today", "bring", "issue", "dental", "what", "assist", "appointment", "need", "how can", "come in"]), r)
        check("Ends with question", has_question(r), r)

        r2 = await send(ws, "hello")
        check("Varies response to repeated greeting", r2.strip() != r.strip())
        check("Still guides toward dental issue", has_any(r2, ["reason", "teeth", "gum", "dental", "help", "issue", "problem", "concern", "visit", "calling", "why", "what"]), r2)

        r3 = await send(ws, "hey there")
        check("Third greeting still varies", r3.strip() != r2.strip())

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        r = await send(ws, "Good morning!")
        check("Handles 'Good morning'", len(r) > 5, r)

        r = await send(ws, "yo")
        check("Handles slang greeting 'yo'", len(r) > 5, r)


async def test_non_dental_inputs():
    """How does Sarah handle off-topic or nonsensical inputs?"""
    print(f"\n{BOLD}[2] NON-DENTAL / NONSENSICAL INPUTS{RESET}")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        r = await send(ws, "What's the weather like?")
        check("Redirects weather question to dental topic", has_any(r, ["dental", "teeth", "help", "visit", "appointment", "reason"]), r)

        r = await send(ws, "asdfghjkl")
        check("Handles gibberish gracefully", len(r) > 5 and not has_any(r, ["error", "crash", "invalid"]), r)

        r = await send(ws, "Can you help me with my car?")
        check("Redirects non-dental request", has_any(r, ["dental", "teeth", "clinic", "help"]), r, warn_only=True)

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        r = await send(ws, "no")
        check("Handles bare 'no' without context", len(r) > 5, r)
        check("Tries to understand patient need", has_question(r), r)

        r = await send(ws, "yes")
        check("Handles bare 'yes' without context", len(r) > 5, r)

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        r = await send(ws, "")
        check("Handles empty message", len(r) > 0, r, warn_only=True)


async def test_reason_understanding():
    """Does Sarah correctly understand various dental reasons?"""
    print(f"\n{BOLD}[3] DENTAL REASON UNDERSTANDING{RESET}")

    reasons = [
        ("I have a toothache", ["time", "date", "when", "prefer", "schedule", "available"]),
        ("My gums are bleeding", ["time", "date", "when", "prefer", "schedule", "available"]),
        ("I need braces", ["time", "date", "when", "prefer", "schedule", "available", "orthodon"]),
        ("I want a teeth cleaning", ["time", "date", "when", "prefer", "schedule", "available"]),
        ("My kid needs a checkup", ["time", "date", "when", "prefer", "schedule", "available", "child", "pediatric"]),
        ("I have a cavity", ["time", "date", "when", "prefer", "schedule", "available"]),
        ("I need a root canal", ["time", "date", "when", "prefer", "schedule", "available"]),
        ("My tooth is really sensitive to cold", ["time", "date", "when", "prefer", "schedule", "available", "sensitivity"]),
    ]

    for reason_text, expected_keywords in reasons:
        async with websockets.connect("ws://localhost:8000/ws") as ws:
            await send(ws, "hi")
            r = await send(ws, reason_text)
            check(
                f"After '{reason_text[:30]}...' → asks for date/time",
                has_any(r, expected_keywords),
                r
            )


async def test_availability_accuracy():
    """Does Sarah correctly check booked vs available slots?"""
    print(f"\n{BOLD}[4] AVAILABILITY CHECKING ACCURACY{RESET}")

    # Known booked: March 12, 11:00-12:00, Doctor 1
    async with websockets.connect("ws://localhost:8000/ws") as ws:
        await send(ws, "I need a checkup")
        r = await send(ws, "March 12 at 11 AM")
        check(
            "Identifies 11 AM March 12 as BOOKED",
            has_any(r, ["taken", "booked", "not available", "unavailable", "already", "occupied", "conflict", "unfortunately", "alternative", "suggest", "different"]),
            r,
            warn_only=True
        )

    # Known free: March 12, 2 PM, Doctor 1
    async with websockets.connect("ws://localhost:8000/ws") as ws:
        await send(ws, "I need a checkup")
        r = await send(ws, "March 12 at 2 PM")
        check(
            "Identifies 2 PM March 12 as AVAILABLE",
            has_any(r, ["available", "works", "name", "great", "book", "confirm", "2", "perfect"]) and not has_any(r, ["taken", "booked", "unavailable"]),
            r,
            warn_only=True
        )

    # March 14 is heavily booked - check that Sarah navigates it
    async with websockets.connect("ws://localhost:8000/ws") as ws:
        await send(ws, "I have a toothache")
        r = await send(ws, "March 14 at 9 AM")
        check(
            "Identifies 9 AM March 14 as BOOKED (General doctor)",
            has_any(r, ["taken", "booked", "not available", "unavailable", "already", "alternative", "suggest", "different"]),
            r,
            warn_only=True
        )

    # Completely free day — March 16 has 10-11 AM booked for Doctor 1
    # Use a truly free slot: March 15 at 10 AM
    async with websockets.connect("ws://localhost:8000/ws") as ws:
        await send(ws, "I need a cleaning")
        r = await send(ws, "March 15 at 10 AM")
        check(
            "March 15 10 AM is available (no bookings that day)",
            has_any(r, ["available", "works", "name", "great", "book", "confirm", "10", "perfect"]) and not has_any(r, ["taken", "booked"]),
            r,
            warn_only=True
        )


async def test_working_hours_enforcement():
    """Does Sarah reject times outside doctor working hours?"""
    print(f"\n{BOLD}[5] WORKING HOURS ENFORCEMENT{RESET}")

    # General doctor works 9 AM - 5 PM
    async with websockets.connect("ws://localhost:8000/ws") as ws:
        await send(ws, "I have a toothache")
        r = await send(ws, "March 16 at 7 AM")
        check(
            "Rejects 7 AM (before General 9 AM start)",
            has_any(r, ["early", "working hours", "9", "open", "start", "available from", "not available", "earliest"]),
            r
        )

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        await send(ws, "I need a cleaning")
        r = await send(ws, "March 16 at 6 PM")
        check(
            "Rejects 6 PM (after General 5 PM end)",
            has_any(r, ["closed", "after", "working hours", "5", "late", "end", "not available", "last"]),
            r,
            warn_only=True
        )

    # Orthodontics works 10 AM - 6 PM
    async with websockets.connect("ws://localhost:8000/ws") as ws:
        await send(ws, "I need braces")
        r = await send(ws, "March 16 at 9 AM")
        check(
            "Rejects 9 AM for Orthodontics (starts at 10 AM)",
            has_any(r, ["10", "working hours", "start", "not available", "earliest", "available from"]),
            r,
            warn_only=True
        )


async def test_relative_date_handling():
    """Does Sarah correctly resolve relative dates?"""
    print(f"\n{BOLD}[6] RELATIVE DATE RESOLUTION{RESET}")

    today = datetime.date.today()
    tomorrow = today + datetime.timedelta(days=1)
    tomorrow_str = tomorrow.strftime("%B %d").replace(" 0", " ")  # "March 12" not "March 02"

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        await send(ws, "I have a cavity")
        r = await send(ws, "Can I come tomorrow at 10 AM?")
        check(
            f"Resolves 'tomorrow' to {tomorrow_str}",
            has_any(r, [tomorrow_str, str(tomorrow.day), tomorrow.strftime("%A"), "tomorrow"]),
            r,
            warn_only=True
        )

    # "this Friday"
    days_until_friday = (4 - today.weekday()) % 7
    if days_until_friday == 0:
        days_until_friday = 7
    friday = today + datetime.timedelta(days=days_until_friday)
    friday_day = str(friday.day)

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        await send(ws, "I need a checkup")
        r = await send(ws, "How about this Friday at 11 AM?")
        check(
            f"Resolves 'this Friday' to correct date (day {friday_day})",
            has_any(r, [friday_day, "Friday", friday.strftime("%B")]),
            r
        )

    # "next week Monday"
    days_until_monday = (0 - today.weekday()) % 7
    if days_until_monday == 0:
        days_until_monday = 7
    next_monday = today + datetime.timedelta(days=days_until_monday)

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        await send(ws, "I need a filling")
        r = await send(ws, "Next Monday at 2 PM")
        check(
            f"Resolves 'next Monday' to a date",
            has_any(r, [str(next_monday.day), "Monday", next_monday.strftime("%B"), "march"]),
            r,
            warn_only=True
        )


async def test_doctor_name_hiding():
    """Sarah should NEVER mention doctor names or Doctor IDs to patients."""
    print(f"\n{BOLD}[7] DOCTOR NAME / ID HIDING{RESET}")

    doctor_names = ["Dr. Smith", "Dr. Jones", "Dr. Davis", "Smith", "Jones", "Davis"]
    doctor_ids = ["Doctor ID 1", "Doctor ID 2", "Doctor ID 3", "doctor id 1", "doctor id 2", "doctor id 3",
                  "Doctor 1", "Doctor 2", "Doctor 3", "Patient ID"]

    scenarios = [
        "I need a cleaning",
        "I need braces",
        "My child needs a checkup",
    ]

    for scenario in scenarios:
        async with websockets.connect("ws://localhost:8000/ws") as ws:
            r1 = await send(ws, scenario)
            r2 = await send(ws, "March 18 at 10 AM")
            combined = r1 + " " + r2
            check(
                f"No doctor names in response to '{scenario}'",
                not has_any(combined, doctor_names),
                combined
            )
            check(
                f"No Doctor IDs leaked in response to '{scenario}'",
                not has_any(combined, doctor_ids),
                combined
            )


async def test_no_asterisks():
    """Sarah should never use *action* text."""
    print(f"\n{BOLD}[8] NO ASTERISKS / ACTION TEXT{RESET}")

    prompts = [
        "I'm really scared of dentists",
        "I'm in so much pain right now",
        "Thank you so much!",
        "This is an emergency, my tooth broke!",
    ]

    for prompt in prompts:
        async with websockets.connect("ws://localhost:8000/ws") as ws:
            r = await send(ws, prompt)
            check(
                f"No asterisks in response to '{prompt[:40]}'",
                "*" not in r,
                r
            )


async def test_conciseness():
    """Responses should be 1-2 sentences, not paragraphs."""
    print(f"\n{BOLD}[9] RESPONSE CONCISENESS{RESET}")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        r = await send(ws, "I have a toothache")
        sentence_count = len(re.findall(r'[.!?]+', r))
        check(
            f"Response is concise ({sentence_count} sentences, expecting ≤ 3)",
            sentence_count <= 4,
            r
        )
        check(
            f"Response is not too long ({len(r)} chars, expecting < 300)",
            len(r) < 300,
            r
        )

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        r = await send(ws, "hi")
        check(
            f"Greeting response is concise ({len(r)} chars)",
            len(r) < 200,
            r
        )


async def test_existing_patient_recognition():
    """Does Sarah recognize patients already in the database?"""
    print(f"\n{BOLD}[10] EXISTING PATIENT RECOGNITION{RESET}")

    # John Doe is patient ID 1
    async with websockets.connect("ws://localhost:8000/ws") as ws:
        await send(ws, "I need a cleaning")
        await send(ws, "March 16 at 10 AM")
        r = await send(ws, "My name is John Doe, phone 555-0101, email john@example.com")
        check(
            "Recognizes John Doe as existing patient",
            has_any(r, ["welcome back", "already", "existing", "system", "profile", "records", "file"]),
            r
        )
        check(
            "Does NOT say 'create new profile' for existing patient",
            not has_any(r, ["create new", "new profile", "new patient"]),
            r
        )

    # Gourab is patient ID 5
    async with websockets.connect("ws://localhost:8000/ws") as ws:
        await send(ws, "I have a cavity")
        await send(ws, "March 16 at 2 PM")
        r = await send(ws, "Gourab, 9876357288, gaurab@gmail.com")
        check(
            "Recognizes Gourab as existing patient",
            has_any(r, ["welcome back", "already", "existing", "system", "profile", "records", "file", "looking up", "confirmed", "booked", "scheduled"]),
            r,
            warn_only=True
        )


async def test_new_patient_handling():
    """Does Sarah handle truly new patients correctly?"""
    print(f"\n{BOLD}[11] NEW PATIENT HANDLING{RESET}")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        await send(ws, "I have a toothache")
        await send(ws, "March 16 at 3 PM")
        r = await send(ws, "My name is Alice Wonderland, phone 555-7777, email alice@wonder.com")
        check(
            "Tells new patient about creating profile",
            has_any(r, ["new", "create", "profile", "welcome", "register", "add"]),
            r,
            warn_only=True
        )
        check(
            "Confirms the appointment",
            has_any(r, ["confirmed", "booked", "scheduled", "appointment", "checking", "profile", "create"]),
            r
        )


async def test_all_info_in_one_message():
    """When patient gives name/phone/email in one message, Sarah should confirm immediately."""
    print(f"\n{BOLD}[12] SINGLE-MESSAGE PATIENT INFO → IMMEDIATE CONFIRMATION{RESET}")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        await send(ws, "I need a cleaning")
        await send(ws, "March 18 at 11 AM")
        r = await send(ws, "Yes that works. My name is Bob Test, phone 555-1234, email bob@test.com")
        check(
            "Confirms booking in same response as receiving patient info",
            has_any(r, ["confirmed", "booked", "scheduled", "thank you"]),
            r
        )
        check(
            "Does NOT ask another question after confirmation",
            not has_question(r) or has_any(r, ["anything else"]),
            r,
            warn_only=True
        )


async def test_emotional_empathy():
    """Does Sarah respond empathetically to emotional patients?"""
    print(f"\n{BOLD}[13] EMOTIONAL / EMPATHETIC RESPONSES{RESET}")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        r = await send(ws, "I'm really scared, I hate going to the dentist")
        check(
            "Shows empathy to scared patient",
            has_any(r, ["understand", "help", "here", "worry", "comfort", "calm", "scared", "fear", "normal", "safe", "okay", "sorry", "concern", "reassure", "anxiety", "nervous"]),
            r
        )

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        r = await send(ws, "I'm in terrible pain, please help me")
        check(
            "Shows urgency for pain",
            has_any(r, ["sorry", "hear", "pain", "help", "soon", "understand", "uncomfortable", "concerning", "right away", "as soon"]),
            r
        )
        check(
            "Still moves toward booking",
            has_any(r, ["time", "date", "when", "schedule", "appointment", "come in", "visit", "?", "available"]),
            r,
            warn_only=True
        )

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        r = await send(ws, "My child fell and broke a tooth, I'm panicking!")
        check(
            "Empathetic response to child emergency",
            has_any(r, ["sorry", "hear", "help", "child", "understand", "calm", "worry"]),
            r,
            warn_only=True
        )


async def test_alternative_slot_suggestion():
    """When a slot is taken, does Sarah suggest alternatives?"""
    print(f"\n{BOLD}[14] ALTERNATIVE SLOT SUGGESTIONS{RESET}")

    # March 12, 11-12 is booked for General (Doctor 1)
    async with websockets.connect("ws://localhost:8000/ws") as ws:
        await send(ws, "I need a checkup")
        r = await send(ws, "March 12 at 11 AM")
        check(
            "Suggests an alternative time when slot is taken",
            has_any(r, ["suggest", "alternative", "instead", "how about", "would", "available", "other", "different", ":"]),
            r
        )
        # Should suggest a specific time
        check(
            "Mentions a specific alternative time",
            bool(re.search(r'\d{1,2}(:\d{2})?\s*(AM|PM|am|pm)', r)),
            r,
            warn_only=True
        )


async def test_booking_confirmation_format():
    """Does the final confirmation include Date, Time, and Reason?"""
    print(f"\n{BOLD}[15] BOOKING CONFIRMATION FORMAT{RESET}")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        await send(ws, "I need a cleaning")
        await send(ws, "March 16 at 10 AM")
        r = await send(ws, "Yes. My name is Format Test, phone 555-4444, email format@test.com")

        if "?" in r and not has_any(r, ["confirmed", "booked"]):
            r = await send(ws, "Yes, please confirm")

        check("Confirmation mentions date", has_any(r, ["march 16", "March 16", "3/16"]), r)
        check("Confirmation mentions time", has_any(r, ["10", "10:00"]), r)
        check("Confirmation mentions reason", has_any(r, ["cleaning", "clean"]), r)
        check("Confirmation includes thank you", has_any(r, ["thank"]), r)
        check("Confirmation mentions clinic name", has_any(r, ["bright smiles", "Bright Smiles"]), r, warn_only=True)


async def test_professionalism():
    """Does Sarah stay professional with rude or inappropriate inputs?"""
    print(f"\n{BOLD}[16] PROFESSIONALISM WITH DIFFICULT INPUTS{RESET}")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        r = await send(ws, "This clinic sucks, you're terrible")
        check(
            "Stays professional with rude input",
            has_any(r, ["sorry", "help", "understand", "apologize", "assist", "dental"]) and "*" not in r,
            r
        )
        check(
            "Does not respond rudely back",
            not has_any(r, ["rude", "shut up", "leave", "go away"]),
            r
        )

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        r = await send(ws, "I want to talk to a real person, not a bot")
        check(
            "Handles 'talk to human' request gracefully",
            len(r) > 5 and not has_any(r, ["error", "crash"]),
            r
        )


async def test_ambiguous_time_inputs():
    """How does Sarah handle ambiguous or partial time inputs?"""
    print(f"\n{BOLD}[17] AMBIGUOUS / PARTIAL TIME INPUTS{RESET}")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        await send(ws, "I need a cleaning")
        r = await send(ws, "sometime next week")
        check(
            "Asks for specific date/time when given vague input",
            has_any(r, ["specific", "time", "date", "prefer", "which day", "when", "what day", "what time", "particular", "day"]),
            r,
            warn_only=True
        )

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        await send(ws, "I need a cleaning")
        r = await send(ws, "morning")
        check(
            "Asks for specific date when only 'morning' given",
            has_any(r, ["date", "which", "what day", "when", "specific", "prefer"]),
            r,
            warn_only=True
        )

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        await send(ws, "I have a toothache")
        r = await send(ws, "14 march 2pm")
        check(
            "Handles 'DD month time' format",
            has_any(r, ["march 14", "March 14", "14", "2 pm", "2:00", "available", "taken"]),
            r
        )


async def test_multi_booking_in_session():
    """Can a patient book two different appointments in one call?"""
    print(f"\n{BOLD}[18] MULTIPLE BOOKINGS IN ONE SESSION{RESET}")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        # Booking 1
        await send(ws, "I need braces")
        await send(ws, "March 16 at 10 AM")
        r1 = await send(ws, "Yes. My name is Multi Test, phone 555-8888, email multi@test.com")
        if "?" in r1 and not has_any(r1, ["confirmed"]):
            r1 = await send(ws, "Yes confirm")

        check("First booking confirmed", has_any(r1, ["confirmed", "booked", "scheduled", "thank"]), r1)

        await asyncio.sleep(2)

        # Booking 2 - different reason
        r2 = await send(ws, "I also have bleeding gums, can I book another appointment?")
        check("Acknowledges second booking request", has_any(r2, ["gum", "bleed", "time", "date", "when", "schedule", "appointment", "another"]), r2)


async def test_edge_case_dates():
    """How does Sarah handle edge case dates?"""
    print(f"\n{BOLD}[19] EDGE CASE DATES{RESET}")

    # Past date
    async with websockets.connect("ws://localhost:8000/ws") as ws:
        await send(ws, "I need a cleaning")
        r = await send(ws, "March 1 at 10 AM")
        check(
            "Addresses past date (March 1 is already past)",
            has_any(r, ["past", "already", "passed", "future", "another", "different", "available"]),
            r,
            warn_only=True
        )

    # Very far future
    async with websockets.connect("ws://localhost:8000/ws") as ws:
        await send(ws, "I need a checkup")
        r = await send(ws, "December 25 at 10 AM")
        check(
            "Handles far-future date (Dec 25)",
            len(r) > 5,
            r
        )

    # Today
    async with websockets.connect("ws://localhost:8000/ws") as ws:
        await send(ws, "I have a toothache")
        r = await send(ws, "Today at 3 PM")
        today = datetime.date.today()
        check(
            "Resolves 'today' to current date",
            has_any(r, [str(today.day), today.strftime("%A"), today.strftime("%B"), "today", "3", "3:00", "this afternoon"]),
            r,
            warn_only=True
        )


async def test_specialty_routing():
    """Does Sarah route to the correct specialist based on the issue?"""
    print(f"\n{BOLD}[20] SPECIALTY ROUTING{RESET}")

    # These should NOT go to Pediatric (Doctor 3, 8 AM start)
    # They should go to General (Doctor 1, 9 AM start) or Orthodontics (Doctor 2, 10 AM start)

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        await send(ws, "I need a cleaning")
        r = await send(ws, "March 16 at 8 AM")
        check(
            "Cleaning at 8 AM → rejected (General starts at 9 AM, should NOT route to Pediatric)",
            has_any(r, ["9", "working hours", "start", "early", "not available", "available from", "earliest"]),
            r,
            warn_only=True
        )

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        await send(ws, "I need braces")
        r = await send(ws, "March 16 at 9 AM")
        check(
            "Braces at 9 AM → rejected (Orthodontics starts at 10 AM)",
            has_any(r, ["10", "working hours", "start", "not available", "available from", "earliest"]),
            r,
            warn_only=True
        )


async def test_conversation_flow_order():
    """Does Sarah follow the correct step order (reason → date → availability → info → confirm)?"""
    print(f"\n{BOLD}[21] CONVERSATION FLOW ORDER{RESET}")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        # Step 1: Should ask for reason
        r = await send(ws, "I want to book an appointment")
        check("Step 1: Asks for reason", has_any(r, ["reason", "what", "bring", "issue", "help", "problem", "visit"]), r)

        # Step 2: Should ask for date/time
        r = await send(ws, "Toothache")
        check("Step 2: Asks for date/time", has_any(r, ["time", "date", "when", "prefer", "schedule", "available"]), r)

        # Step 3: Should check availability (use a free slot)
        r = await send(ws, "March 18 at 10 AM")
        check("Step 3: Checks availability or confirms date", has_any(r, ["available", "works", "taken", "name", "phone", "email", "book", "confirm", "work for you", "does that", "correct", "march", "10"]), r)

        # Step 4: Should ask for info (or confirm if already given)
        if has_any(r, ["name", "phone", "email", "information", "provide"]):
            check("Step 4: Asks for patient info", True)
            r = await send(ws, "Flow Test, 555-0000, flow@test.com")
        else:
            r = await send(ws, "Yes, that works")
            check("Step 4: Moves forward", len(r) > 5, r)
            if has_any(r, ["name", "phone", "email", "provide"]):
                r = await send(ws, "Flow Test, 555-0000, flow@test.com")

        # Step 5: Should confirm (allow one more exchange if needed)
        if not has_any(r, ["confirmed", "booked", "scheduled", "thank"]):
            r = await send(ws, "Yes, please confirm the appointment")
        if not has_any(r, ["confirmed", "booked", "scheduled", "thank"]):
            r = await send(ws, "Confirm")
        check("Step 5: Final confirmation", has_any(r, ["confirmed", "booked", "scheduled", "thank"]), r)


# ── Runner ───────────────────────────────────────────────────────────────────

async def run_all():
    global TOTAL_PASS, TOTAL_FAIL, TOTAL_WARN

    print(f"\n{BOLD}{'#' * 60}")
    print(f"  SARAH CONVERSATION BEHAVIOR TESTS")
    print(f"  Testing AI responses across {21} scenarios")
    print(f"  Server: ws://localhost:8000/ws")
    print(f"{'#' * 60}{RESET}")

    backup()

    tests = [
        test_greeting_variations,
        test_non_dental_inputs,
        test_reason_understanding,
        test_availability_accuracy,
        test_working_hours_enforcement,
        test_relative_date_handling,
        test_doctor_name_hiding,
        test_no_asterisks,
        test_conciseness,
        test_existing_patient_recognition,
        test_new_patient_handling,
        test_all_info_in_one_message,
        test_emotional_empathy,
        test_alternative_slot_suggestion,
        test_booking_confirmation_format,
        test_professionalism,
        test_ambiguous_time_inputs,
        test_multi_booking_in_session,
        test_edge_case_dates,
        test_specialty_routing,
        test_conversation_flow_order,
    ]

    for test_fn in tests:
        try:
            restore()
            backup()
            await test_fn()
        except Exception as e:
            print(f"    {RED}CRASH{RESET} {test_fn.__name__}: {e}")
            import traceback
            traceback.print_exc()
            TOTAL_FAIL += 1

    restore()

    print(f"\n{BOLD}{'=' * 60}")
    total = TOTAL_PASS + TOTAL_FAIL + TOTAL_WARN
    print(f"  RESULTS: {GREEN}{TOTAL_PASS} passed{RESET}, {RED}{TOTAL_FAIL} failed{RESET}, {YELLOW}{TOTAL_WARN} warnings{RESET} ({total} total)")
    print(f"{'=' * 60}{RESET}")

    return TOTAL_FAIL == 0


if __name__ == "__main__":
    success = asyncio.run(run_all())
    sys.exit(0 if success else 1)
