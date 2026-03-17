"""
Comprehensive conversation tests for the Dental AI Assistant.
Tests multiple scenarios by sending messages via WebSocket and validating responses.
"""
import asyncio
import json
import datetime
import sys
import websockets

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"

total_passed = 0
total_failed = 0
issues_found = []


async def send_and_receive(ws, user_text, timeout=45):
    """Send a user message and collect the full AI response."""
    print(f"    {CYAN}USER:{RESET} {user_text}")
    await ws.send(json.dumps({"type": "user_input", "text": user_text}))

    full_response = ""
    try:
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            data = json.loads(raw)
            if data["type"] == "text_delta":
                full_response += data.get("content", "")
            elif data["type"] == "generation_done":
                break
            elif data["type"] == "audio":
                pass
    except asyncio.TimeoutError:
        print(f"    {RED}TIMEOUT{RESET} (partial: '{full_response[:60]}...')")

    response_clean = full_response.strip()
    print(f"    {YELLOW}SARAH:{RESET} {response_clean}")
    return response_clean


def check(condition, pass_msg, fail_msg):
    global total_passed, total_failed
    if condition:
        print(f"    {GREEN}PASS:{RESET} {pass_msg}")
        total_passed += 1
    else:
        print(f"    {RED}FAIL:{RESET} {fail_msg}")
        total_failed += 1
        issues_found.append(fail_msg)


def has_question_mark(r):
    return "?" in r


def no_doctor_names(r):
    r_lower = r.lower()
    return all(name not in r_lower for name in ["dr. smith", "dr. jones", "dr. davis", "smith", "jones", "davis"])


def no_asterisks(r):
    import re
    return not bool(re.search(r'\*[^*]+\*', r))


def is_concise(r, max_sentences=5):
    import re
    return len(re.findall(r'[.!?]+', r)) <= max_sentences


async def test_1_happy_path():
    """Full booking flow: greeting -> reason -> available slot -> patient info -> confirmation"""
    print(f"\n  {BOLD}TEST 1: Happy Path - Complete Booking Flow{RESET}")

    today = datetime.date.today()
    # Pick a date 7+ days out to avoid collisions
    future = today + datetime.timedelta(days=8)
    # Skip to a weekday
    while future.weekday() >= 5:  # Saturday=5, Sunday=6
        future += datetime.timedelta(days=1)
    future_str = future.strftime("%B %d")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        # Step 1: Greeting
        r = await send_and_receive(ws, "Hello, I'd like to book an appointment")
        check(has_question_mark(r), "Asked a question", f"No question mark: {r[:60]}")
        check(no_doctor_names(r), "No doctor names leaked", f"Doctor name leaked: {r[:60]}")
        check(no_asterisks(r), "No asterisks", f"Asterisks found: {r[:60]}")

        # Step 2: Give reason
        r = await send_and_receive(ws, "I have a really bad toothache")
        check(has_question_mark(r), "Asked for date/time", f"No question mark: {r[:60]}")
        r_lower = r.lower()
        check(any(w in r_lower for w in ["time", "date", "when", "prefer", "available", "schedule", "slot"]),
              "Mentioned scheduling", f"Didn't ask about scheduling: {r[:60]}")

        # Step 3: Give date and time (should be free)
        r = await send_and_receive(ws, f"How about {future_str} at 11 AM?")
        r_lower = r.lower()
        # Should either confirm availability or ask for details
        check(any(w in r_lower for w in ["available", "name", "phone", "email", "book", "great", "wonderful", "perfect", "open"]),
              "Acknowledged slot availability", f"Didn't confirm availability: {r[:80]}")
        check(no_doctor_names(r), "No doctor names leaked", f"Doctor name leaked: {r[:60]}")

        # Step 4: Provide patient info
        r = await send_and_receive(ws, "My name is Test User, phone 555-1111, email test@test.com")
        r_lower = r.lower()
        check(any(w in r_lower for w in ["confirm", "appointment", "booked", "schedule", future.strftime("%B").lower()]),
              "Confirmed booking", f"No confirmation: {r[:80]}")
        check(is_concise(r), "Response is concise", f"Too verbose: {r[:100]}")


async def test_2_booked_slot_handling():
    """Test that Sarah correctly identifies a booked slot and offers alternatives"""
    print(f"\n  {BOLD}TEST 2: Booked Slot - Unavailability Handling{RESET}")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        r = await send_and_receive(ws, "I need a dental checkup")
        check(has_question_mark(r), "Asked question after greeting", f"No question: {r[:60]}")

        # March 19 at 9 AM is booked for Dr. 1 (toothache) - a future booked slot
        r = await send_and_receive(ws, "Can I come March 19 at 9 AM?")
        r_lower = r.lower()
        check(any(w in r_lower for w in ["not available", "booked", "unavailable", "unfortunately", "taken", "alternative", "different", "another", "passed"]),
              "Correctly identified slot as BOOKED or past", f"Didn't say unavailable: {r[:100]}")
        check(has_question_mark(r), "Offered alternative with question", f"No question: {r[:60]}")
        check(no_doctor_names(r), "No doctor names", f"Doctor name leaked: {r[:60]}")


async def test_3_working_hours_enforcement():
    """Test that times outside working hours are rejected"""
    print(f"\n  {BOLD}TEST 3: Working Hours Enforcement{RESET}")

    today = datetime.date.today()
    future = today + datetime.timedelta(days=5)
    while future.weekday() >= 5:
        future += datetime.timedelta(days=1)
    future_str = future.strftime("%B %d")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        r = await send_and_receive(ws, "I need a tooth cleaning")

        # 7 AM is before any doctor starts (earliest is 8 AM pediatric)
        r = await send_and_receive(ws, f"Can I come at 7 AM on {future_str}?")
        r_lower = r.lower()
        check(any(w in r_lower for w in ["not available", "unavailable", "unfortunately", "different", "alternative", "another", "open", "9", "8"]),
              "Rejected 7 AM or offered alternative", f"Accepted 7 AM: {r[:100]}")

        # 6 PM is after general doctor ends (5 PM)
        r = await send_and_receive(ws, f"How about 6 PM on {future_str}?")
        r_lower = r.lower()
        check(any(w in r_lower for w in ["not available", "unavailable", "unfortunately", "different", "alternative", "another", "booked"]),
              "Rejected 6 PM or offered alternative", f"Accepted 6 PM: {r[:100]}")


async def test_4_relative_dates():
    """Test 'tomorrow', 'today', day-of-week handling"""
    print(f"\n  {BOLD}TEST 4: Relative Date Resolution{RESET}")

    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    tomorrow_str = tomorrow.strftime("%B %d")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        r = await send_and_receive(ws, "I have a cavity that needs to be filled")
        check(has_question_mark(r), "Asked scheduling question", f"No question: {r[:60]}")

        r = await send_and_receive(ws, "Can I come tomorrow at 10 AM?")
        r_lower = r.lower()
        # Should reference tomorrow's actual date or say "tomorrow"
        tomorrow_day = str(tomorrow.day)
        tomorrow_month = tomorrow.strftime("%B").lower()
        check(any(w in r_lower for w in [tomorrow_day, tomorrow_month, "tomorrow"]),
              f"Resolved 'tomorrow' correctly", f"Didn't reference tomorrow ({tomorrow_str}): {r[:100]}")


async def test_5_no_asterisks_emotional_input():
    """Test that emotional inputs don't trigger asterisk action text"""
    print(f"\n  {BOLD}TEST 5: No Asterisks on Emotional Input{RESET}")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        responses = []
        responses.append(await send_and_receive(ws, "I'm really scared about going to the dentist"))
        responses.append(await send_and_receive(ws, "My tooth is killing me, I'm in so much pain!"))
        responses.append(await send_and_receive(ws, "I'm crying because it hurts so bad"))

        for i, r in enumerate(responses):
            check(no_asterisks(r), f"Response {i+1}: No asterisks", f"Response {i+1} has asterisks: {r[:60]}")
            check(no_doctor_names(r), f"Response {i+1}: No doctor names", f"Response {i+1} leaks names: {r[:60]}")


async def test_6_conciseness():
    """Test that all responses are concise (max ~3-5 sentences)"""
    print(f"\n  {BOLD}TEST 6: Response Conciseness{RESET}")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        responses = []
        responses.append(await send_and_receive(ws, "Hi there!"))
        responses.append(await send_and_receive(ws, "I have a toothache that's been going on for 3 days"))

        for i, r in enumerate(responses):
            check(is_concise(r), f"Response {i+1}: Concise", f"Response {i+1} too verbose: {r[:100]}")
            check(len(r) < 400, f"Response {i+1}: Under 400 chars ({len(r)})", f"Response {i+1} too long: {len(r)} chars")


async def test_7_modification_blocking():
    """Test that cancel/reschedule requests are blocked"""
    print(f"\n  {BOLD}TEST 7: Modification Blocking{RESET}")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        r = await send_and_receive(ws, "I want to cancel my appointment")
        r_lower = r.lower()
        check(any(w in r_lower for w in ["cancel", "change", "reschedule", "front desk", "call", "555", "new appointment", "book"]),
              "Directed to front desk or offered new booking", f"Didn't handle cancellation: {r[:100]}")
        check(has_question_mark(r), "Offered alternative with question", f"No question: {r[:60]}")


async def test_8_non_dental_input():
    """Test that non-dental questions are redirected"""
    print(f"\n  {BOLD}TEST 8: Non-Dental Input Redirect{RESET}")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        r = await send_and_receive(ws, "What's the weather like today?")
        r_lower = r.lower()
        check(any(w in r_lower for w in ["dental", "tooth", "teeth", "appointment", "help", "smile", "clinic", "book", "schedule", "service"]),
              "Redirected to dental topics", f"Didn't redirect: {r[:100]}")
        check(has_question_mark(r), "Asked a dental question", f"No question: {r[:60]}")


async def test_9_specialty_routing():
    """Test that specialty-specific issues route to correct doctors"""
    print(f"\n  {BOLD}TEST 9: Specialty Routing (Braces -> Orthodontics){RESET}")

    today = datetime.date.today()
    future = today + datetime.timedelta(days=10)
    while future.weekday() >= 5:
        future += datetime.timedelta(days=1)
    future_str = future.strftime("%B %d")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        r = await send_and_receive(ws, "I need braces for my teeth")
        check(has_question_mark(r), "Asked scheduling question", f"No question: {r[:60]}")

        # Orthodontics doctor works 10 AM - 6 PM, so 9 AM should be unavailable
        r = await send_and_receive(ws, f"Can I come at 9 AM on {future_str}?")
        r_lower = r.lower()
        check(any(w in r_lower for w in ["not available", "unavailable", "unfortunately", "10", "alternative", "different"]),
              "9 AM rejected (ortho starts at 10 AM) or offered 10 AM", f"Accepted 9 AM for ortho: {r[:100]}")


async def test_10_past_date_rejection():
    """Test that past dates are rejected"""
    print(f"\n  {BOLD}TEST 10: Past Date Rejection{RESET}")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        r = await send_and_receive(ws, "I need a dental cleaning")

        r = await send_and_receive(ws, "Can I come on January 5?")
        r_lower = r.lower()
        check(any(w in r_lower for w in ["past", "passed", "future", "another", "different", "available"]),
              "Rejected past date", f"Accepted past date: {r[:100]}")


async def test_11_gibberish_handling():
    """Test that gibberish/empty inputs are handled gracefully"""
    print(f"\n  {BOLD}TEST 11: Gibberish/Empty Input Handling{RESET}")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        r = await send_and_receive(ws, "asdfghjkl qwerty zxcvbn")
        check(has_question_mark(r), "Still asked a question", f"No question after gibberish: {r[:60]}")
        check(len(r) > 10, "Gave a meaningful response", f"Too short: {r}")

        r = await send_and_receive(ws, "")
        # Empty might not trigger, that's ok


async def test_12_multi_turn_consistency():
    """Test that conversation state is maintained across multiple turns"""
    print(f"\n  {BOLD}TEST 12: Multi-Turn State Consistency{RESET}")

    today = datetime.date.today()
    future = today + datetime.timedelta(days=12)
    while future.weekday() >= 5:
        future += datetime.timedelta(days=1)
    future_str = future.strftime("%B %d")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        await send_and_receive(ws, "Hi")
        r = await send_and_receive(ws, "I have bleeding gums")
        r_lower = r.lower()
        check(any(w in r_lower for w in ["time", "date", "when", "schedule", "available", "slot", "prefer"]),
              "Moved to scheduling after reason given", f"Didn't move to step 2: {r[:80]}")

        r = await send_and_receive(ws, f"How about {future_str} at 2 PM?")
        r_lower = r.lower()
        # Should be at step 4 (asking for info) if available
        check(any(w in r_lower for w in ["name", "phone", "email", "available", "book", "great", "open"]),
              "Asked for patient info or confirmed slot", f"Unexpected: {r[:80]}")


async def run_all():
    global total_passed, total_failed, issues_found

    print(f"\n{BOLD}{'#'*70}")
    print(f"  DENTAL AI ASSISTANT - COMPREHENSIVE TEST SUITE")
    print(f"  Date: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'#'*70}{RESET}")

    tests = [
        test_1_happy_path,
        test_2_booked_slot_handling,
        test_3_working_hours_enforcement,
        test_4_relative_dates,
        test_5_no_asterisks_emotional_input,
        test_6_conciseness,
        test_7_modification_blocking,
        test_8_non_dental_input,
        test_9_specialty_routing,
        test_10_past_date_rejection,
        test_11_gibberish_handling,
        test_12_multi_turn_consistency,
    ]

    for test_fn in tests:
        try:
            await test_fn()
        except Exception as e:
            print(f"    {RED}CRASHED: {e}{RESET}")
            import traceback
            traceback.print_exc()
            total_failed += 1
            issues_found.append(f"Test crashed: {e}")

    print(f"\n{BOLD}{'='*70}")
    print(f"  RESULTS: {GREEN}{total_passed} passed{RESET}, {RED}{total_failed} failed{RESET}")
    if issues_found:
        print(f"\n  {RED}ISSUES FOUND:{RESET}")
        for issue in issues_found:
            print(f"    - {issue}")
    print(f"{'='*70}{RESET}")

    return total_failed == 0


if __name__ == "__main__":
    success = asyncio.run(run_all())
    sys.exit(0 if success else 1)
