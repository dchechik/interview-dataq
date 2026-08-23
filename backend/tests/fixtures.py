"""Small, realistic fixtures for the two v0 target datasets."""

from __future__ import annotations

import csv
import random
from pathlib import Path

COUNTRIES = ["US", "FR", "DE", "GB", "BR", "JP", "NG", "IN"]
# Deliberately skewed so rarity/frequency aggregates have something to find.
COUNTRY_WEIGHTS = [70, 3, 8, 9, 4, 3, 1, 12]
USERS = [f"user{i:03d}@example.com" for i in range(40)]


def write_taxi_csv(path: Path, rows: int = 1200, seed: int = 7) -> Path:
    rng = random.Random(seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow([
            "trip_id", "tpep_pickup_datetime", "passenger_count", "trip_distance",
            "pickup_latitude", "pickup_longitude", "payment_type", "fare_amount",
        ])
        for i in range(rows):
            day = rng.randint(1, 28)
            hour = rng.choice([8, 9, 17, 18, 19, 20, 21, 22, 23])
            w.writerow([
                f"t{i:07d}",
                f"2024-03-{day:02d} {hour:02d}:{rng.randint(0,59):02d}:00",
                rng.randint(1, 5),
                round(rng.uniform(0.3, 22.0), 2),
                round(rng.uniform(40.60, 40.88), 6),
                round(rng.uniform(-74.02, -73.75), 6),
                rng.choice(["card", "cash", "no_charge"]),
                round(rng.uniform(3.0, 90.0), 2),
            ])
    return path


def write_auth_csv(path: Path, rows: int = 1500, seed: int = 11) -> Path:
    rng = random.Random(seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["event_id", "ts", "user_email", "src_ip", "country", "action", "success"])
        for i in range(rows):
            country = rng.choices(COUNTRIES, weights=COUNTRY_WEIGHTS)[0]
            w.writerow([
                f"e{i:07d}",
                f"2024-05-{rng.randint(1,28):02d}T{rng.randint(0,23):02d}:{rng.randint(0,59):02d}:00",
                rng.choice(USERS),
                f"{rng.randint(1,223)}.{rng.randint(0,255)}.{rng.randint(0,255)}.{rng.randint(1,254)}",
                country,
                rng.choice(["login", "logout", "mfa_challenge", "password_reset"]),
                rng.random() > 0.12,
            ])
    return path
