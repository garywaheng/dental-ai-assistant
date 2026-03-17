"""
Iteration 2: Deeper conversation quality tests.
Focus on edge cases, alternative acceptance flow, and response quality.
"""
import asyncio
import json
import datetime
import sys
import re
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
        print(f"    {RED}TIMEOUT{RESET}")
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


async def test_alternative_acceptance():
    """Test: User gets unavailable slot, Sarah offers alternative, user accepts"""
    print(f"\n  {BOLD}TEST A: Alternative Acceptance Flow{RESET}")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        r = await send_and_receive(ws, "I need a dental checkup")

        # March 19 at 9 AM is booked for Dr. 1
        r = await send_and_receive(ws, "How about March 19 at 9 AM?")
        r_lower = r.lower()
        check("not available" in r_lower or "unavailable" in r_lower or "unfortunately" in r_lower,
              "Identified slot as booked", f"Didn't say unavailable: {r[:80]}")

        # Extract the suggested alternative time
        alt_match = re.search(r'(\d{1,2}:\d{2}\s*[AP]M)', r)
        if alt_match:
            alt_time = alt_match.group(1)
            print(f"    {CYAN}INFO:{RESET} Alternative offered: {alt_time}")

        # User accepts the alternative
        r = await send_and_receive(ws, "Yes, that works!")
        r_lower = r.lower()
        check(any(w in r_lower for w in ["name", "phone", "email", "book", "great", "wonderful", "available", "confirm"]),
              "Moved to patient info after accepting alternative",
              f"Didn't move to step 4 after accepting: {r[:100]}")
        check("?" in r, "Asked a question (for patient info)", f"No question mark: {r[:60]}")


async def test_alternative_rejection():
    """Test: User rejects the alternative and asks for a different time"""
    print(f"\n  {BOLD}TEST B: Alternative Rejection Flow{RESET}")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        r = await send_and_receive(ws, "I need a checkup")

        # March 19 at 9 AM is booked
        r = await send_and_receive(ws, "March 19 at 9 AM please")

        # User rejects the alternative
        r = await send_and_receive(ws, "No, that doesn't work for me. How about 2 PM instead?")
        r_lower = r.lower()
        # Should either confirm 2 PM or offer another alternative
        check(any(w in r_lower for w in ["available", "2", "name", "phone", "email", "great", "wonderful", "not available", "unfortunately"]),
              "Handled rejection and new time request",
              f"Didn't handle rejection properly: {r[:100]}")


async def test_info_provided_piecemeal():
    """Test: User provides name, phone, email one at a time"""
    print(f"\n  {BOLD}TEST C: Piecemeal Patient Info{RESET}")

    today = datetime.date.today()
    future = today + datetime.timedelta(days=15)
    while future.weekday() >= 5:
        future += datetime.timedelta(days=1)
    future_str = future.strftime("%B %d")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        await send_and_receive(ws, "I have a toothache")
        r = await send_and_receive(ws, f"I'd like to come on {future_str} at 11 AM")

        # Give only name first
        r = await send_and_receive(ws, "My name is Alice Johnson")
        r_lower = r.lower()
        check("?" in r, "Asked for remaining info", f"No question after partial info: {r[:60]}")
        check(any(w in r_lower for w in ["phone", "email", "number", "contact"]),
              "Asked for phone/email", f"Didn't ask for missing info: {r[:80]}")

        # Give phone
        r = await send_and_receive(ws, "My phone is 555-2222")
        r_lower = r.lower()
        check("?" in r or "email" in r_lower,
              "Asked for email", f"Didn't ask for email: {r[:80]}")

        # Give email
        r = await send_and_receive(ws, "My email is alice@test.com")
        r_lower = r.lower()
        check(any(w in r_lower for w in ["confirm", "appointment", "booked", "scheduled"]),
              "Confirmed booking after all info", f"No confirmation: {r[:100]}")


async def test_forbidden_phrases():
    """Test that forbidden phrases don't appear in any responses"""
    print(f"\n  {BOLD}TEST D: No Forbidden Phrases{RESET}")

    forbidden = ["hold on", "let me check", "wait while", "one moment while i check",
                 "checking our schedule", "let me look", "pulling up", "i told you",
                 "as i said", "listen to me", "previously mentioned", "like i said"]

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        responses = []
        responses.append(await send_and_receive(ws, "Hi, I need an appointment"))
        responses.append(await send_and_receive(ws, "I have a cavity"))
        responses.append(await send_and_receive(ws, "Do you have anything available?"))
        responses.append(await send_and_receive(ws, "What about tomorrow?"))

        all_clean = True
        for i, r in enumerate(responses):
            r_lower = r.lower()
            for phrase in forbidden:
                if phrase in r_lower:
                    check(False, "", f"Response {i+1} contains forbidden phrase '{phrase}': {r[:80]}")
                    all_clean = False
        if all_clean:
            check(True, "No forbidden phrases in any response", "")


async def test_same_day_booking():
    """Test booking for today when slots are available"""
    print(f"\n  {BOLD}TEST E: Same-Day Booking{RESET}")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        r = await send_and_receive(ws, "I need an emergency dental visit")
        r_lower = r.lower()
        check("?" in r, "Asked about dental issue", f"No question: {r[:60]}")

        r = await send_and_receive(ws, "My tooth is broken, can I come in today at 4 PM?")
        r_lower = r.lower()
        # 4 PM today should be available for general (Dr. 1 works 9-5, 4 PM is within hours)
        # Unless it's already past 4 PM or booked
        check(any(w in r_lower for w in ["available", "name", "phone", "email", "great", "not available", "unfortunately", "4"]),
              "Handled same-day request", f"Didn't handle same-day: {r[:100]}")


async def test_user_changes_mind():
    """Test: User gets to step 4, then wants a different time"""
    print(f"\n  {BOLD}TEST F: User Changes Mind at Step 4{RESET}")

    today = datetime.date.today()
    future = today + datetime.timedelta(days=20)
    while future.weekday() >= 5:
        future += datetime.timedelta(days=1)
    future_str = future.strftime("%B %d")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        await send_and_receive(ws, "I need a dental cleaning")
        r = await send_and_receive(ws, f"How about {future_str} at 10 AM?")
        r_lower = r.lower()

        # Should be at step 4 (available)
        if "name" in r_lower or "available" in r_lower:
            # User changes their mind
            r = await send_and_receive(ws, f"Actually, can I do 2 PM on {future_str} instead?")
            r_lower = r.lower()
            check(any(w in r_lower for w in ["2", "available", "name", "phone", "email", "great", "not available"]),
                  "Handled time change gracefully", f"Didn't handle time change: {r[:100]}")
        else:
            check(True, "Skipped (slot was unavailable)", "")


async def test_response_never_empty():
    """Test that responses are never empty or extremely short"""
    print(f"\n  {BOLD}TEST G: Non-Empty Responses{RESET}")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        inputs = ["hi", ".", "123", "what?", "I don't know", "help"]
        for text in inputs:
            r = await send_and_receive(ws, text)
            check(len(r) >= 10, f"Response to '{text}' is non-empty ({len(r)} chars)",
                  f"Response to '{text}' is too short: '{r}'")


async def run_all():
    global total_passed, total_failed, issues_found

    print(f"\n{BOLD}{'#'*70}")
    print(f"  DENTAL AI ASSISTANT - ITERATION 2 TESTS")
    print(f"  Date: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'#'*70}{RESET}")

    tests = [
        test_alternative_acceptance,
        test_alternative_rejection,
        test_info_provided_piecemeal,
        test_forbidden_phrases,
        test_same_day_booking,
        test_user_changes_mind,
        test_response_never_empty,
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
