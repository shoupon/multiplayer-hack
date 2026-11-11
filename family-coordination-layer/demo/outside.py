"""The outside world for the celebration demo: calendars, a restaurant booking service, and email.

All in-process mocks on one laptop. FCL never calls any of them. Agents do, and only after their
owner approved the exact action through FCL.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

TPE = ZoneInfo("Asia/Taipei")
EVENING_START, EVENING_END = time(18, 0), time(21, 30)


def at(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm), TPE)


def day_label(d: date) -> str:
    return f"{d:%a} {d.day} {d:%b}"


@dataclass(frozen=True)
class Event:
    start: datetime
    end: datetime
    title: str


@dataclass
class Calendar:
    """A personal calendar. Reading it is the owner's business; this mock only counts reads."""

    owner: str
    events: list[Event]
    reads: int = 0

    def free_evenings(self, first: date, last: date) -> list[date]:
        self.reads += 1
        free = []
        day = first
        while day <= last:
            start, end = datetime.combine(day, EVENING_START, TPE), datetime.combine(day, EVENING_END, TPE)
            if not any(e.start < end and start < e.end for e in self.events):
                free.append(day)
            day += timedelta(days=1)
        return free


@dataclass(frozen=True)
class Restaurant:
    id: str
    name: str
    cuisine: str
    address: str
    rating: float
    price_pp: int
    vegetarian_mains: bool
    kitchen_allergens: frozenset[str]
    deposit_pp: int
    seatings: dict[int, tuple[str, ...]]  # weekday (Mon=0) -> free seatings for up to 4 people


RESTAURANTS = (
    Restaurant("golden-wok", "Golden Wok", "Sichuan", "12 Lantern St, Taipei", 4.6, 850, True,
               frozenset({"peanut", "sesame"}), 200, {4: ("19:00",), 5: ("18:00", "19:30")}),
    Restaurant("green-lotus", "Green Lotus", "Modern vegetarian", "88 Lotus Ln, Taipei", 4.7, 980, True,
               frozenset({"soy"}), 300, {4: ("20:30",), 5: ("18:30", "20:30"), 6: ("18:00",)}),
    Restaurant("harbor-grill", "Harbor Grill", "Seafood grill", "3 Pier Rd, Taipei", 4.5, 1800, False,
               frozenset({"shellfish"}), 500, {5: ("19:00",)}),
    Restaurant("sprout-and-stone", "Sprout & Stone", "Vegetarian tasting", "41 Orchard Ave, Taipei", 4.8, 1100, True,
               frozenset(), 400, {4: ("19:00",)}),
    Restaurant("basil-trattoria", "Basil Trattoria", "Italian", "7 Vine St, Taipei", 4.4, 1050, True,
               frozenset({"tree_nut", "gluten"}), 300, {5: ("19:00",)}),
)


@dataclass(frozen=True)
class Card:
    token: str
    last4: str


@dataclass(frozen=True)
class Booking:
    reference: str
    receipt_id: str
    charged_minor: int
    card_last4: str


@dataclass
class Hold:
    id: str
    restaurant: Restaurant
    start: datetime
    party: int
    deposit_minor: int
    expires_at: datetime
    booking: Optional[Booking] = None
    paid_with_key: Optional[str] = None


def _luhn(number: str) -> bool:
    digits = [int(ch) for ch in number][::-1]
    total = sum(digits[0::2]) + sum(sum(divmod(2 * d, 10)) for d in digits[1::2])
    return len(digits) >= 12 and total % 10 == 0


class Outbox:
    """Where TableBook's confirmation emails land: one .eml file per recipient."""

    def __init__(self, folder: Path):
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        self.sent: list[Path] = []

    def send(self, to: tuple[str, str], subject: str, body: str, ics: str) -> Path:
        msg = EmailMessage()
        msg["From"] = "TableBook <bookings@tablebook.example>"
        msg["To"] = f"{to[0]} <{to[1]}>"
        msg["Subject"] = subject
        msg["Date"] = format_datetime(datetime.now(timezone.utc))
        msg.set_content(body)
        msg.add_attachment(ics.encode(), maintype="text", subtype="calendar", filename="dinner.ics")
        path = self.folder / f"{len(self.sent) + 1:02d}-{to[0].lower()}.eml"
        path.write_bytes(bytes(msg))
        self.sent.append(path)
        return path


class TableBook:
    """A restaurant booking service with deposits. Charges a card at most once per idempotency key."""

    def __init__(self, outbox: Outbox):
        self.outbox = outbox
        self.holds: dict[str, Hold] = {}
        self.cards: dict[str, Card] = {}
        self.charges: list[tuple[str, int]] = []
        self.taken: set[tuple[str, datetime]] = set()

    def restaurant(self, restaurant_id: str) -> Restaurant:
        return next(r for r in RESTAURANTS if r.id == restaurant_id)

    def search(self, day: date) -> list[tuple[Restaurant, list[datetime]]]:
        return [(r, [s for s in (at(day, hhmm) for hhmm in r.seatings.get(day.weekday(), ())) if (r.id, s) not in self.taken])
                for r in RESTAURANTS]

    def hold(self, restaurant_id: str, start: datetime, party: int) -> Hold:
        r = self.restaurant(restaurant_id)
        assert (r.id, start) not in self.taken, "seating already taken"
        self.taken.add((r.id, start))
        h = Hold(f"hold_{secrets.token_hex(4)}", r, start, party, r.deposit_pp * party * 100,
                 datetime.now(TPE) + timedelta(minutes=30))
        self.holds[h.id] = h
        return h

    def tokenize_card(self, number: str) -> Card:
        """TableBook's hosted card field. The card number never leaves TableBook; callers get a token."""
        digits = number.replace(" ", "")
        if not digits.isdigit() or not _luhn(digits):
            raise ValueError("card number is not valid")
        card = Card(f"tok_{secrets.token_hex(6)}", digits[-4:])
        self.cards[card.token] = card
        return card

    def confirm(self, hold_id: str, card_token: str, amount_minor: int, idempotency_key: str,
                guests: list[tuple[str, str]]) -> Booking:
        h = self.holds[hold_id]
        if h.booking is not None:
            if h.paid_with_key != idempotency_key:
                raise ValueError("hold already paid under a different idempotency key")
            return h.booking
        if amount_minor != h.deposit_minor:
            raise ValueError(f"deposit is {h.deposit_minor}, not {amount_minor}")
        card = self.cards[card_token]
        self.charges.append((card_token, amount_minor))
        h.booking = Booking(f"TB-{secrets.token_hex(3).upper()}", f"rcpt_{secrets.token_hex(4)}", amount_minor, card.last4)
        h.paid_with_key = idempotency_key
        self._email(h, guests)
        return h.booking

    def _email(self, h: Hold, guests: list[tuple[str, str]]) -> None:
        r, b = h.restaurant, h.booking
        assert b is not None
        when = f"{day_label(h.start.date())}, {h.start:%H:%M}"
        stamp = lambda d: d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")  # noqa: E731
        ics = "\r\n".join([
            "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//TableBook mock//EN", "BEGIN:VEVENT",
            f"UID:{b.reference}@tablebook.example", f"DTSTAMP:{stamp(datetime.now(TPE))}",
            f"DTSTART:{stamp(h.start)}", f"DTEND:{stamp(h.start + timedelta(hours=2))}",
            f"SUMMARY:Celebration dinner at {r.name}", f"LOCATION:{r.name}\\, {r.address}",
            "END:VEVENT", "END:VCALENDAR", ""])
        body = (f"Your table is booked.\n\n"
                f"  {r.name} ({r.cuisine})\n  {r.address}\n  {when}, party of {h.party}\n\n"
                f"Booking reference: {b.reference}\n"
                f"Deposit paid: NT${b.charged_minor // 100:,} (card ending {b.card_last4}), receipt {b.receipt_id}\n"
                f"Kitchen allergens: {', '.join(sorted(r.kitchen_allergens)) or 'none'}. Vegetarian mains available.\n\n"
                f"A calendar invite is attached.\n")
        for guest in guests:
            self.outbox.send(guest, f"Confirmed: {r.name}, {when}, party of {h.party}", body, ics)
