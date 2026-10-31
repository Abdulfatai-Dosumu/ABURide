# ABURide — Hyper-Lean Transit Coordination Platform (PoC / MVP)
**Ahmadu Bello University (Samaru Campus), Zaria**

ABURide bridges the information asymmetry between stranded campus commuters and idling campus shuttle/keke drivers without GPS tracking, battery drain, or heavy native mobile applications.

---

## 1. Architectural Highlights & Non-Negotiable Constraints

1. **Zero Real-Time GPS Tracking**:
   - Geolocation is inferred strictly from discrete transactional state transitions executed while vehicles are stationary. When a driver confirms a drop-off leg at *ICS*, their discrete location transitions to *ICS*.
2. **Student Interface**:
   - Telegram Bot built on `python-telegram-bot` v20+ with inline keyboards. Zero app installation, minimal data consumption on 3G/4G networks.
3. **Driver Interface & Demand Heat Map**:
   - Mobile-first, high-contrast dashboard running in Chrome on low-end Android devices (viewport 360px–412px). Large stationary tap targets.
   - **Interactive Demand Heat Map**: Displays real-time campus hubs with red heat halos and individual red dots representing waiting students clustered at each transit hub.
   - Dual-engine rendering: Uses Leaflet.js with OpenStreetMap when online, with an automatic fallback to an inline vector SVG transit map if offline or low-bandwidth.
   - Displays the driver's stationary declared location as a prominent green marker (`📍 You are here`).
4. **Network Realism & Anti-Staleness**:
   - No optimistic queue updates. Driver dashboard uses ETag caching with 5-second polling; if connectivity drops past 15 seconds, demand is hidden with an explicit offline warning.
5. **Atomic Seat Allocations & Escrow Ledger**:
   - Seat reservation and claiming are transactional and race-condition free using SQLite `BEGIN EXCLUSIVE` transactions (the equivalent of PostgreSQL `SELECT ... FOR UPDATE` row locks).
   - Capacity invariant strictly guarantees that a driver cannot claim more seats than physical vehicle capacity (`MAX_SEATS`).
   - Mock wallet ledger automatically places fares in escrow at booking, releases funds to the driver upon verified PIN boarding, and auto-refunds on expiry.

---

## 2. Campus Stops & Seed Data

The platform comes pre-seeded with the 5 key transit hubs of Samaru campus:
- **North Gate**
- **Main Gate**
- **Complex**
- **Ribadu**
- **ICS**

Flat fare: **₦150.00** (represented internally as `15000` integer cents) across all routes.

Pre-seeded driver accounts:
- **Driver Abdullahi**: Token `driver-a-token`, Vehicle Capacity: 4 seats
- **Driver Bello**: Token `driver-b-token`, Vehicle Capacity: 6 seats

New students receive a welcome bonus of **₦500.00** (`50000` cents) upon `/start`.

---

## 3. Repository Structure

```text
ABURide(anti-g)/
├── aburide/
│   ├── __init__.py           # Package marker
│   ├── db.py                 # SQLite WAL connection manager, schema, seeds, tx context
│   ├── services.py           # Domain logic, ledger escrow, atomic claims, reaper
│   ├── main.py               # FastAPI application, REST endpoints, background reaper
│   ├── bot.py                # Telegram bot with inline keyboards & notification polling
│   └── static/
│       ├── driver.html       # High-contrast mobile web dashboard with heat map
│       └── student.html      # Interactive Telegram Student Bot simulator & TMA
├── tests/
│   ├── conftest.py           # Test fixtures & database setup
│   ├── test_aburide.py       # Core unit & concurrency tests
│   ├── test_api.py           # FastAPI routes & student simulator verification
│   ├── test_bot.py           # Telegram bot commands, refunds & lifecycle alert tests
│   └── test_e2e_simulation.py# Headless end-to-end student-to-driver lifecycle test
├── requirements.txt          # Python dependencies
└── README.md                 # Setup & presentation guide
```

---

## 4. Verification & Automated Test Suite

ABURide includes a comprehensive automated test suite verifying all required constraints and invariants (18/18 passing):

```bash
python -m pytest -v
```

### Verified Test Invariants:
1. **Concurrency Race Condition Test** (`test_two_drivers_race_for_three_students`):
   - 3 students waiting at *North Gate*, 2 drivers simultaneously claim 2 seats each across 15 randomized thread-barrier iterations.
   - Asserts: Driver A claims 2, Driver B claims 1, exactly 0 seats over-allocated, zero double claims.
2. **Capacity Race Invariant** (`test_capacity_race_never_overbooks`):
   - Two concurrent threads attempt to overbook a 4-seater vehicle.
   - Asserts: Vehicle never exceeds 4 seats; excess requests raise `CapacityExceeded`.
3. **Auto-Expiry & Escrow Refund** (`test_expiry_refunds_exact_fare`):
   - Creates a hold with 3-second TTL.
   - Reaper triggers at `t0 + 3`: status transitions to `EXPIRED`, student ledger is refunded the exact fare amount, driver demand drops to 0, second reaper run is idempotent.
4. **Capacity HTTP 400 Invariant** (`test_capacity_invariant_http_400`):
   - Driver requests more seats than capacity via HTTP endpoint; receives HTTP 400 with `{"type": "CapacityExceeded"}`.
5. **Balance & Single-Hold Invariant** (`test_insufficient_funds_and_single_open_hold`):
   - Prevents duplicate active holds per student and guards against insufficient balance.
6. **Telegram Bot Flow & Lifecycle Notifications** (`test_bot_start_command`, `test_bot_ride_full_booking_flow`, `test_bot_notification_dispatcher`, `test_bot_cancel_command`, `test_notifications_on_expiry_boarding_and_arrival`):
   - Tests registration, `/ride` origin/destination inline keyboards, confirmation card, PIN generation, and live push notification delivery.
   - **3-Minute Auto-Refund**: Verifies that when no driver claims within 3 minutes, the hold expires, the fare is refunded to the student's ledger, and a refund alert is sent to the student with their updated balance.
   - **Boarding Confirmation**: Verifies that student receives an instant message upon driver PIN verification ("Welcome aboard! Driver ... has verified your PIN. En route to ...").
   - **Arrival & Thank You**: Verifies message delivered to student when vehicle completes the trip at their destination ("Arrived at ... Thank you for riding with ABURide! Have a wonderful day on campus!").
   - **Student Cancellation**: Verifies `/cancel` command cancels an active hold and issues an immediate refund.
7. **End-to-End Headless Simulation** (`test_complete_student_to_driver_e2e_lifecycle`):
   - Simulates the full chain: Student registers via Telegram -> Books North Gate to ICS -> Escrow holds ₦150 -> Driver sees live demand -> Driver claims seat -> Student receives instant PIN notification -> Driver boards student with valid PIN -> Driver completes trip -> Discrete location updates to ICS -> Vehicle seat freed -> Reaper guarantees completed trip is never refunded.

---

## 5. Quick Start & MVP Presentation Guide

### Prerequisites
- Python 3.10+ installed
- Dependencies installed:
  ```bash
  pip install -r requirements.txt
  ```

### Step 1: Start the Driver Backend & Web Dashboard
In a terminal, navigate to `ABURide(anti-g)` and run:
```bash
python -m uvicorn aburide.main:app --host 127.0.0.1 --port 8000 --reload
```
Open your mobile browser or Chrome DevTools (Device Mode: Pixel 7 or Galaxy S20, 360–412px width):
- URL: `http://127.0.0.1:8000/`
- Enter driver token: `driver-a-token` (or `driver-b-token`)
- Click **Sign in**
- Select where you are parked (e.g., *Main Gate* or *North Gate*)

### Step 2: Start the Telegram Bot (Student Interface)
To run the Telegram bot with a live bot token from [@BotFather](https://t.me/BotFather):
```bash
python -m aburide.bot <YOUR_TELEGRAM_BOT_TOKEN>
```
*Note: In headless test mode or during development, the complete student flow can also be exercised via the test suite or direct API calls.*

### Step 3: Presenting the MVP Flow
1. **Student Books Ride**:
   - In Telegram, send `/start` to register and receive ₦500 welcome credit.
   - Send `/ride`.
   - Tap **North Gate** for pickup, then tap **ICS** for destination.
   - View fare card (₦150.00) and tap **✅ Confirm**.
   - Student receives their 3-digit PIN (e.g. `482`).
2. **Driver Observes Live Demand**:
   - Driver Dashboard updates within 5s showing 1 passenger waiting at North Gate.
   - Driver taps **Claim 1**.
3. **Student Alert**:
   - Telegram bot instantly alerts the student: *"🚐 Driver claimed your hold! Vehicle arriving at North Gate. Present PIN: 4 8 2"*.
4. **Stationary Boarding**:
   - Driver enters PIN `482` and taps **Confirm boarding**.
   - Escrow funds (₦150) transfer to driver earnings.
5. **Trip Completion**:
   - Driver arrives at ICS and taps **Confirm drop-off**.
   - Seat is freed on the dashboard and driver location automatically updates to ICS.
