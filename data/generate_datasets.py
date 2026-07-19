"""
Synthetic Dataset Generator
Generates realistic datasets for testing the Autonomous Data Analyst Agent
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import random
import os

random.seed(42)
np.random.seed(42)

OUTPUT_DIR = os.path.dirname(__file__)


def generate_sales_dataset():
    """Monthly sales data across regions, products, and reps"""
    regions = ["North", "South", "East", "West"]
    products = ["Product A", "Product B", "Product C", "Product D"]
    reps = [f"Rep_{i}" for i in range(1, 21)]

    start = datetime(2023, 1, 1)
    records = []

    for month_offset in range(18):
        date = start + timedelta(days=30 * month_offset)
        for region in regions:
            for product in products:
                rep = random.choice(reps)
                base_sales = {"Product A": 50000, "Product B": 35000,
                              "Product C": 70000, "Product D": 25000}[product]
                region_factor = {"North": 1.2, "South": 0.9, "East": 1.1, "West": 1.0}[region]

                # Simulate a March 2024 sales drop
                month_factor = 1.0
                if date.month == 3 and date.year == 2024:
                    month_factor = 0.55

                sales = int(base_sales * region_factor * month_factor * np.random.uniform(0.8, 1.2))
                units = int(sales / random.randint(200, 500))
                target = int(base_sales * region_factor * 1.05)
                returns = int(units * np.random.uniform(0.01, 0.08))
                customer_satisfaction = round(np.random.uniform(3.2, 5.0), 1)

                records.append({
                    "date": date.strftime("%Y-%m-%d"),
                    "region": region,
                    "product": product,
                    "sales_rep": rep,
                    "sales_amount": sales,
                    "units_sold": units,
                    "sales_target": target,
                    "returns": returns,
                    "customer_satisfaction": customer_satisfaction,
                    "month": date.strftime("%B"),
                    "year": date.year,
                    "quarter": f"Q{(date.month - 1) // 3 + 1}"
                })

    df = pd.DataFrame(records)
    # Inject some missing values and duplicates for data quality checks
    df.loc[df.sample(frac=0.02).index, "customer_satisfaction"] = np.nan
    df.loc[df.sample(frac=0.01).index, "sales_rep"] = np.nan
    df = pd.concat([df, df.sample(5)], ignore_index=True)  # 5 duplicates

    path = os.path.join(OUTPUT_DIR, "sales_data.csv")
    df.to_csv(path, index=False)
    print(f"✅ Sales dataset: {len(df)} rows → {path}")
    return path


def generate_attendance_dataset():
    """Workforce attendance data — Canticles-flavored"""
    sites = ["Site_Alpha", "Site_Beta", "Site_Gamma", "Site_Delta", "Site_Epsilon"]
    departments = ["Security", "Housekeeping", "Maintenance", "Reception"]
    shifts = ["Morning", "Afternoon", "Night"]

    employees = []
    for i in range(1, 201):
        employees.append({
            "emp_id": f"EMP{i:04d}",
            "name": f"Employee_{i}",
            "department": random.choice(departments),
            "site": random.choice(sites),
            "shift": random.choice(shifts),
            "base_salary": random.choice([15000, 18000, 20000, 22000, 25000])
        })

    emp_df = pd.DataFrame(employees)
    records = []
    start = datetime(2024, 1, 1)

    for day_offset in range(90):
        date = start + timedelta(days=day_offset)
        if date.weekday() == 6:  # Skip Sundays
            continue
        for _, emp in emp_df.iterrows():
            status_roll = random.random()
            if status_roll < 0.85:
                status = "Present"
                punch_in = f"{random.randint(7, 9):02d}:{random.randint(0, 59):02d}"
                punch_out = f"{random.randint(17, 20):02d}:{random.randint(0, 59):02d}"
                hours = round(random.uniform(7.5, 10.5), 1)
                # Ghost attendance: present but GPS off-site
                gps_match = random.random() > 0.05
            elif status_roll < 0.92:
                status = "Late"
                punch_in = f"{random.randint(10, 12):02d}:{random.randint(0, 59):02d}"
                punch_out = f"{random.randint(17, 19):02d}:{random.randint(0, 59):02d}"
                hours = round(random.uniform(5.0, 7.5), 1)
                gps_match = True
            else:
                status = "Absent"
                punch_in = None
                punch_out = None
                hours = 0
                gps_match = None

            records.append({
                "date": date.strftime("%Y-%m-%d"),
                "emp_id": emp["emp_id"],
                "name": emp["name"],
                "department": emp["department"],
                "site": emp["site"],
                "shift": emp["shift"],
                "status": status,
                "punch_in": punch_in,
                "punch_out": punch_out,
                "hours_worked": hours,
                "gps_location_match": gps_match,
                "base_salary": emp["base_salary"]
            })

    df = pd.DataFrame(records)
    # Inject missing values
    df.loc[df.sample(frac=0.02).index, "hours_worked"] = np.nan

    path = os.path.join(OUTPUT_DIR, "attendance_data.csv")
    df.to_csv(path, index=False)
    print(f"✅ Attendance dataset: {len(df)} rows → {path}")
    return path


def generate_student_dataset():
    """Student performance dataset"""
    subjects = ["Math", "Science", "English", "History", "Computer_Science"]
    grades = ["Grade_9", "Grade_10", "Grade_11", "Grade_12"]

    records = []
    for student_id in range(1, 501):
        grade = random.choice(grades)
        study_hours = round(random.uniform(1, 8), 1)
        attendance_pct = round(random.uniform(60, 100), 1)
        base_score = 40 + (study_hours * 5) + (attendance_pct * 0.2) + random.uniform(-10, 10)

        for subject in subjects:
            subject_factor = {"Math": 0.9, "Science": 0.95, "English": 1.05,
                              "History": 1.1, "Computer_Science": 1.0}[subject]
            score = min(100, max(0, round(base_score * subject_factor + random.uniform(-8, 8), 1)))
            records.append({
                "student_id": f"STU{student_id:04d}",
                "grade": grade,
                "subject": subject,
                "score": score,
                "study_hours_per_day": study_hours,
                "attendance_percentage": attendance_pct,
                "passed": score >= 40,
                "grade_letter": "A" if score >= 80 else "B" if score >= 65 else "C" if score >= 50 else "D" if score >= 40 else "F"
            })

    df = pd.DataFrame(records)
    df.loc[df.sample(frac=0.015).index, "score"] = np.nan

    path = os.path.join(OUTPUT_DIR, "student_data.csv")
    df.to_csv(path, index=False)
    print(f"✅ Student dataset: {len(df)} rows → {path}")
    return path


def generate_timeseries_dataset():
    """E-commerce time series for forecasting"""
    start = datetime(2022, 1, 1)
    records = []

    for day_offset in range(730):  # 2 years
        date = start + timedelta(days=day_offset)
        trend = 1000 + (day_offset * 0.5)
        seasonality = 200 * np.sin(2 * np.pi * day_offset / 365)
        weekly = -150 if date.weekday() in [5, 6] else 50
        noise = np.random.normal(0, 80)
        revenue = max(0, trend + seasonality + weekly + noise)

        records.append({
            "date": date.strftime("%Y-%m-%d"),
            "revenue": round(revenue, 2),
            "orders": int(revenue / random.randint(45, 75)),
            "website_visitors": int(revenue / random.uniform(1.5, 3.0)),
            "day_of_week": date.strftime("%A"),
            "month": date.strftime("%B"),
            "is_weekend": date.weekday() >= 5
        })

    df = pd.DataFrame(records)
    path = os.path.join(OUTPUT_DIR, "ecommerce_timeseries.csv")
    df.to_csv(path, index=False)
    print(f"✅ Time series dataset: {len(df)} rows → {path}")
    return path


if __name__ == "__main__":
    print("🔄 Generating synthetic datasets...\n")
    generate_sales_dataset()
    generate_attendance_dataset()
    generate_student_dataset()
    generate_timeseries_dataset()
    print("\n✅ All datasets generated successfully!")
