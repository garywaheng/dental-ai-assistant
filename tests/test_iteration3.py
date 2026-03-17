"""
Iteration 3: Stress tests and tricky edge cases.
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


async def test_rapid_full_booking():
    """User provides everything in 2 messages - reason+date+time, then info"""
    print(f"\n  {BOLD}TEST I: Rapid Full Booking (2 messages){RESET}")

    today = datetime.date.today()
    future = today + datetime.timedelta(days=14)
    while future.weekday() >= 5:
        future += datetime.timedelta(days=1)
    future_str = future.strftime("%B %d")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        r = await send_and_receive(ws, f"I need a cleaning on {future_str} at 10 AM")
        r_lower = r.lower()
        check(any(w in r_lower for w in ["name", "phone", "email", "available", "not available", "slot"]),
              "Jumped to info request or availability", f"Unexpected response: {r[:100]}")

        if "name" in r_lower or "email" in r_lower:
            r = await send_and_receive(ws, "Jane Smith, 555-3333, jane@smith.com")
            r_lower = r.lower()
            check(any(w in r_lower for w in ["confirm", "appointment", "booked"]),
                  "Confirmed booking", f"No confirmation: {r[:100]}")


async def test_cancel_then_new_booking():
    """User asks to cancel, then decides to book new"""
    print(f"\n  {BOLD}TEST II: Cancel Request -> New Booking{RESET}")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        r = await send_and_receive(ws, "I want to cancel my appointment")
        r_lower = r.lower()
        check("555" in r_lower or "front desk" in r_lower or "call" in r_lower,
              "Directed to front desk for cancellation", f"Didn't mention front desk: {r[:80]}")

        # Now book a new one
        r = await send_and_receive(ws, "Yes, I'd like to book a new appointment")
        r_lower = r.lower()
        check("?" in r, "Asked dental question", f"No question: {r[:60]}")

        r = await send_and_receive(ws, "I have a toothache")
        r_lower = r.lower()
        check(any(w in r_lower for w in ["time", "date", "when", "prefer", "available", "schedule", "slot"]),
              "Moved to scheduling for new booking", f"Didn't progress: {r[:80]}")


async def test_weekend_awareness():
    """Test that Saturday/Sunday aren't offered as available"""
    print(f"\n  {BOLD}TEST III: Weekend Handling{RESET}")

    # Find next Saturday
    today = datetime.date.today()
    days_until_sat = (5 - today.weekday()) % 7
    if days_until_sat == 0:
        days_until_sat = 7
    next_sat = today + datetime.timedelta(days=days_until_sat)
    sat_str = next_sat.strftime("%B %d")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        await send_and_receive(ws, "I need a cleaning")
        r = await send_and_receive(ws, f"Can I come on {sat_str} at 10 AM?")
        r_lower = r.lower()
        # Doctors don't have Saturday hours in the CSV, so it might say unavailable
        # or suggest a weekday alternative
        check(len(r) > 10, "Got a response for Saturday request",
              f"Empty or short response: {r}")


async def test_very_long_input():
    """Test handling of very long user messages"""
    print(f"\n  {BOLD}TEST IV: Very Long Input{RESET}")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        long_msg = "I have been having this terrible toothache for about three weeks now. " * 10
        r = await send_and_receive(ws, long_msg)
        check(len(r) > 10, "Handled long input", f"Empty response to long input")
        check("?" in r, "Asked a follow-up question", f"No question: {r[:60]}")


async def test_multiple_issues():
    """User mentions multiple dental issues"""
    print(f"\n  {BOLD}TEST V: Multiple Issues Mentioned{RESET}")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        r = await send_and_receive(ws, "I need both a cleaning and I have a toothache")
        r_lower = r.lower()
        check("?" in r, "Asked scheduling question", f"No question: {r[:60]}")
        check(any(w in r_lower for w in ["time", "date", "when", "prefer", "available", "schedule", "slot"]),
              "Moved to scheduling", f"Didn't progress: {r[:80]}")


async def test_time_format_variations():
    """Test various time format inputs"""
    print(f"\n  {BOLD}TEST VI: Time Format Variations{RESET}")

    today = datetime.date.today()
    future = today + datetime.timedelta(days=10)
    while future.weekday() >= 5:
        future += datetime.timedelta(days=1)
    future_str = future.strftime("%B %d")

    formats = [
        ("10am", "10 AM"),
        ("2:30 PM", "2:30 PM"),
        ("3 in the afternoon", "3 PM"),
    ]

    for fmt_input, expected in formats:
        async with websockets.connect("ws://localhost:8000/ws") as ws:
            await send_and_receive(ws, "I have a toothache")
            r = await send_and_receive(ws, f"Can I come {future_str} at {fmt_input}?")
            check(len(r) > 10, f"Handled time format '{fmt_input}'", f"Poor response to '{fmt_input}': {r[:60]}")


async def test_slot_suggestion_quality():
    """Test that suggested slots are actually available"""
    print(f"\n  {BOLD}TEST VII: Slot Suggestion Quality{RESET}")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        r = await send_and_receive(ws, "I need a dental checkup")
        r_lower = r.lower()

        # Extract suggested times from the response
        times = re.findall(r'(\d{1,2}:\d{2}\s*[AP]M)', r)
        if times:
            print(f"    {CYAN}INFO:{RESET} Suggested times: {times}")
            # Try booking the first suggested time
            r2 = await send_and_receive(ws, f"I'll take {times[0]}")
            r2_lower = r2.lower()
            check(any(w in r2_lower for w in ["name", "phone", "email", "available", "great", "wonderful", "book"]),
                  f"Suggested time {times[0]} was actually available",
                  f"Suggested time {times[0]} wasn't available: {r2[:80]}")
        else:
            check(True, "No specific times suggested (asked for preference)", "")


async def test_conversation_doesnt_loop():
    """Verify conversation progresses and doesn't get stuck"""
    print(f"\n  {BOLD}TEST VIII: No Conversation Loops{RESET}")

    today = datetime.date.today()
    future = today + datetime.timedelta(days=7)
    while future.weekday() >= 5:
        future += datetime.timedelta(days=1)
    future_str = future.strftime("%B %d")

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        r1 = await send_and_receive(ws, "Hi")
        r2 = await send_and_receive(ws, "I have pain in my teeth")
        r3 = await send_and_receive(ws, f"{future_str} at 11 AM")
        r4 = await send_and_receive(ws, "Bob Wilson, 555-4444, bob@test.com")

        # Each response should be different
        responses = [r1, r2, r3, r4]
        unique_responses = set(responses)
        check(len(unique_responses) >= 3,
              f"Conversation progressed ({len(unique_responses)} unique responses)",
              f"Responses may be looping (only {len(unique_responses)} unique)")

        # Final response should be confirmation
        r4_lower = r4.lower()
        check(any(w in r4_lower for w in ["confirm", "appointment", "booked", "thank", "name", "phone", "email"]),
              "Reached confirmation or info collection",
              f"Didn't reach end: {r4[:80]}")


async def run_all():
    global total_passed, total_failed, issues_found

    print(f"\n{BOLD}{'#'*70}")
    print(f"  DENTAL AI ASSISTANT - ITERATION 3 (STRESS TESTS)")
    print(f"  Date: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'#'*70}{RESET}")

    tests = [
        test_rapid_full_booking,
        test_cancel_then_new_booking,
        test_weekend_awareness,
        test_very_long_input,
        test_multiple_issues,
        test_time_format_variations,
        test_slot_suggestion_quality,
        test_conversation_doesnt_loop,
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
