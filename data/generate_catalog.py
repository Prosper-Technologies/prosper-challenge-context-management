#!/usr/bin/env python3
"""Generate a deliberately-large, deliberately-messy clinic catalog for the
context-management challenge. Deterministic (fixed seed) so the dataset is stable.

Knobs: N_LOCATIONS / N_PROVIDERS scale the catalog up toward the "hundreds of
locations, thousands of appointment types" framing without code changes.
"""

import json
import random
import sys

random.seed(7)

N_LOCATIONS = 30
N_PROVIDERS = 150

# --- capabilities a location may have (gates certain appointment types) -------
CAPABILITIES = ["imaging", "lab", "physical_therapy", "dental", "surgery"]

# --- appointment types (curated; includes deliberate near-duplicates) ---------
# (name, specialty, duration_min, requires_referral, new_patients_allowed, required_capability)
APPT_TYPES = [
    ("New Patient Consultation", "General", 40, False, True, None),
    ("New Patient Visit", "General", 30, False, True, None),            # ~ dup of above
    ("Annual Physical", "Family Medicine", 30, False, True, None),
    ("Annual Wellness Visit", "Internal Medicine", 30, False, True, None),  # ~ dup
    ("Follow-up Visit", "General", 20, False, False, None),
    ("Follow-up Consultation", "General", 20, False, False, None),     # ~ dup
    ("Telehealth Follow-up", "General", 15, False, False, None),
    ("Sick Visit", "Family Medicine", 20, False, True, None),
    ("Urgent Care Visit", "Family Medicine", 25, False, True, None),
    ("Medication Review", "General", 15, False, False, None),
    ("Vaccination / Immunization", "Family Medicine", 10, False, True, None),
    ("Flu Shot", "Family Medicine", 10, False, True, None),
    ("COVID-19 Vaccine", "Family Medicine", 10, False, True, None),
    ("Travel Vaccination Consult", "Family Medicine", 20, False, True, None),
    ("Pre-operative Evaluation", "Internal Medicine", 30, True, False, None),
    ("Well-Child Visit", "Pediatrics", 30, False, True, None),
    ("Newborn Visit", "Pediatrics", 30, False, True, None),
    ("Pediatric Sick Visit", "Pediatrics", 20, False, True, None),
    ("School Physical", "Pediatrics", 25, False, True, None),
    ("Sports Physical", "Pediatrics", 25, False, True, None),          # ~ dup of School Physical
    ("Cardiology Consultation", "Cardiology", 40, True, True, None),
    ("Echocardiogram", "Cardiology", 45, True, False, "imaging"),
    ("Stress Test", "Cardiology", 60, True, False, None),
    ("EKG / Electrocardiogram", "Cardiology", 20, False, False, None),
    ("Holter Monitor Fitting", "Cardiology", 20, True, False, None),
    ("Pacemaker Check", "Cardiology", 30, True, False, None),
    ("Dermatology Consultation", "Dermatology", 30, True, True, None),
    ("Skin Cancer Screening", "Dermatology", 30, False, True, None),
    ("Full Body Skin Exam", "Dermatology", 30, False, True, None),     # ~ dup of Skin Cancer Screening
    ("Mole Removal", "Dermatology", 30, True, False, "surgery"),
    ("Acne Follow-up", "Dermatology", 15, False, False, None),
    ("Cosmetic Consultation", "Dermatology", 30, False, True, None),
    ("Orthopedic Consultation", "Orthopedics", 40, True, True, None),
    ("Joint Injection", "Orthopedics", 30, True, False, None),
    ("Fracture Follow-up", "Orthopedics", 20, False, False, None),
    ("Pre-surgical Consultation", "Orthopedics", 40, True, False, None),
    ("Neurology Consultation", "Neurology", 45, True, True, None),
    ("EEG", "Neurology", 60, True, False, None),
    ("Headache Follow-up", "Neurology", 20, False, False, None),
    ("Memory Evaluation", "Neurology", 60, True, True, None),
    ("OB/GYN New Patient Visit", "OB/GYN", 40, False, True, None),
    ("Annual Well-Woman Exam", "OB/GYN", 30, False, True, None),
    ("Prenatal Visit", "OB/GYN", 30, False, True, None),
    ("Pap Smear", "OB/GYN", 20, False, True, None),
    ("Contraception Consultation", "OB/GYN", 20, False, True, None),
    ("Eye Exam", "Ophthalmology", 30, False, True, None),
    ("Comprehensive Eye Exam", "Ophthalmology", 45, False, True, None),  # ~ dup of Eye Exam
    ("Glaucoma Screening", "Ophthalmology", 30, True, True, None),
    ("Contact Lens Fitting", "Ophthalmology", 30, False, True, None),
    ("Cataract Evaluation", "Ophthalmology", 40, True, True, None),
    ("ENT Consultation", "ENT", 30, True, True, None),
    ("Hearing Test / Audiogram", "ENT", 30, False, True, None),
    ("Sinus Evaluation", "ENT", 30, True, True, None),
    ("GI Consultation", "Gastroenterology", 40, True, True, None),
    ("Colonoscopy Consultation", "Gastroenterology", 30, True, True, None),
    ("Endoscopy Consultation", "Gastroenterology", 30, True, True, None),  # ~ dup of Colonoscopy Consult
    ("Endocrinology Consultation", "Endocrinology", 40, True, True, None),
    ("Diabetes Management", "Endocrinology", 30, False, False, None),
    ("Thyroid Follow-up", "Endocrinology", 20, False, False, None),
    ("Psychiatric Evaluation", "Psychiatry", 60, True, True, None),
    ("Therapy Session", "Psychiatry", 50, False, False, None),
    ("Medication Management", "Psychiatry", 20, False, False, None),
    ("X-Ray", "Radiology", 20, True, False, "imaging"),
    ("MRI - Brain", "Radiology", 60, True, False, "imaging"),
    ("MRI - Spine", "Radiology", 60, True, False, "imaging"),          # ~ dup family of MRI
    ("MRI - Knee", "Radiology", 45, True, False, "imaging"),
    ("CT Scan", "Radiology", 30, True, False, "imaging"),
    ("Ultrasound", "Radiology", 30, True, False, "imaging"),
    ("Mammogram", "Radiology", 30, False, True, "imaging"),
    ("Bone Density Scan (DEXA)", "Radiology", 30, True, False, "imaging"),
    ("Physical Therapy Evaluation", "Physical Therapy", 45, True, True, "physical_therapy"),
    ("Physical Therapy Session", "Physical Therapy", 45, False, False, "physical_therapy"),
    ("Blood Draw / Lab Work", "Lab", 15, False, True, "lab"),
    ("Fasting Blood Test", "Lab", 15, False, True, "lab"),             # ~ dup of Blood Draw
    ("Dental Cleaning", "Dental", 45, False, True, "dental"),
    ("Dental Exam", "Dental", 30, False, True, "dental"),
    ("Dental New Patient Exam", "Dental", 60, False, True, "dental"),
    ("Urology Consultation", "Urology", 40, True, True, None),
    ("Pulmonology Consultation", "Pulmonology", 40, True, True, None),
    ("Spirometry / Lung Function Test", "Pulmonology", 30, True, False, None),
    ("Allergy Consultation", "Allergy/Immunology", 40, True, True, None),
    ("Allergy Skin Testing", "Allergy/Immunology", 60, True, False, None),
]

GENERAL_FOR_PCP = [  # generic types a primary-care provider also offers
    "New Patient Consultation", "New Patient Visit", "Annual Physical",
    "Annual Wellness Visit", "Follow-up Visit", "Follow-up Consultation",
    "Telehealth Follow-up", "Sick Visit", "Urgent Care Visit",
    "Medication Review", "Vaccination / Immunization", "Flu Shot",
    "COVID-19 Vaccine",
]
PCP_SPECIALTIES = {"Family Medicine", "Internal Medicine", "Pediatrics"}

# --- name pools (surnames repeat on purpose to create disambiguation) ---------
FIRST = ["James", "Maria", "David", "Daniel", "Sarah", "Jennifer", "Michael",
         "Linda", "Robert", "Patricia", "John", "Elizabeth", "Wei", "Priya",
         "Carlos", "Aisha", "Kenji", "Sofia", "Omar", "Emily", "Daniel",
         "Hannah", "Lucas", "Nina", "Raj", "Grace", "Tomas", "Leila",
         "Victor", "Mei", "Andre", "Fatima", "Samuel", "Olivia", "Diego"]
LAST = ["Chen", "Garcia", "Smith", "Patel", "Nguyen", "Johnson", "Kim",
        "Lee", "Martinez", "Brown", "Wong", "Singh", "Rodriguez", "Cohen",
        "Okafor", "Ramirez", "Sato", "Hernandez", "Khan", "Williams",
        "Chen", "Garcia", "Patel", "Nguyen"]  # leading repeats raise collision odds
TITLES = ["MD", "MD", "MD", "DO", "NP", "PA"]
LANGUAGES = [["English"], ["English", "Spanish"], ["English", "Mandarin"],
             ["English", "Hindi"], ["English", "Vietnamese"],
             ["English", "Arabic"], ["English", "Spanish", "Portuguese"]]
SPECIALTIES = sorted({a[1] for a in APPT_TYPES} - {"General"})

# neighborhoods with intentional near-duplicates
NEIGHBORHOODS = [
    "Mission Bay", "Mission District", "North Beach", "North Gate", "Downtown",
    "Midtown", "Sunset", "Richmond", "SOMA", "Marina", "Nob Hill", "Bayview",
    "Hayes Valley", "Pacific Heights", "Castro", "Noe Valley", "Glen Park",
    "Excelsior", "Potrero Hill", "Russian Hill", "Tenderloin", "Chinatown",
    "Inner Sunset", "Outer Sunset", "Dogpatch", "Cole Valley", "Presidio",
    "Japantown", "Western Addition", "Twin Peaks",
]
LOC_KINDS = ["Medical Center", "Family Clinic", "Health Center", "Medical Group",
             "Community Clinic", "Specialty Center", "Care Center"]
STREETS = ["Main St", "Market St", "Oak Ave", "Pine St", "Cedar Blvd",
           "Valencia St", "Geary Blvd", "Folsom St", "Mission St", "Bryant St"]


def lid(i): return f"loc_{i:03d}"
def pid(i): return f"prov_{i:03d}"
def aid(i): return f"appt_{i:03d}"


# ---- appointment types -------------------------------------------------------
appointment_types = []
for i, (name, spec, dur, ref, newp, cap) in enumerate(APPT_TYPES):
    at = {
        "id": aid(i),
        "name": name,
        "specialty": spec,
        "duration_min": dur,
        "requires_referral": ref,
        "new_patients_allowed": newp,
    }
    if cap:
        at["required_capability"] = cap
    appointment_types.append(at)

appt_by_specialty = {}
appt_by_name = {}
for at in appointment_types:
    appt_by_specialty.setdefault(at["specialty"], []).append(at["id"])
    appt_by_name[at["name"]] = at["id"]
GENERAL_IDS = [appt_by_name[n] for n in GENERAL_FOR_PCP]

# ---- locations ---------------------------------------------------------------
locations = []
hood_choices = NEIGHBORHOODS[:]
random.shuffle(hood_choices)
for i in range(N_LOCATIONS):
    hood = hood_choices[i % len(hood_choices)]
    kind = random.choice(LOC_KINDS)
    caps = []
    # ~1/5 have imaging, ~1/2 lab, plus scattered specialty capabilities
    if random.random() < 0.20:
        caps.append("imaging")
    if random.random() < 0.50:
        caps.append("lab")
    if random.random() < 0.25:
        caps.append("physical_therapy")
    if random.random() < 0.15:
        caps.append("dental")
    if random.random() < 0.12:
        caps.append("surgery")
    locations.append({
        "id": lid(i),
        "name": f"{hood} {kind}",
        "address": f"{random.randint(100, 4999)} {random.choice(STREETS)}",
        "city": "San Francisco",
        "phone": f"(415) 555-{random.randint(1000, 9999)}",
        "hours": "Mon-Fri 8:00-17:00",
        "capabilities": caps,
    })

imaging_locs = [l["id"] for l in locations if "imaging" in l["capabilities"]]
# guarantee at least a couple of imaging locations exist
while len(imaging_locs) < 3:
    l = random.choice(locations)
    if "imaging" not in l["capabilities"]:
        l["capabilities"].append("imaging")
        imaging_locs.append(l["id"])

# ---- providers ---------------------------------------------------------------
# Force specific collision cases first (documented ambiguities).
forced = [
    ("David", "Chen", "MD", "Cardiology"),
    ("Daniel", "Chen", "MD", "Dermatology"),     # same surname, different first + specialty
    ("Maria", "Garcia", "MD", "Pediatrics"),
    ("Maria", "Garcia", "NP", "Family Medicine"),  # same full name, different role/specialty
    ("Wei", "Chen", "DO", "Internal Medicine"),   # third "Chen"
]
providers = []
all_loc_ids = [l["id"] for l in locations]


def make_provider(idx, first, last, title, spec):
    # provider practices at 1-3 locations
    locs = random.sample(all_loc_ids, k=random.choice([1, 1, 2, 2, 3]))
    accepting = random.random() < 0.75  # ~1/4 not accepting new patients
    # appointment types this provider can do
    ids = list(appt_by_specialty.get(spec, []))
    if spec in PCP_SPECIALTIES:
        ids = sorted(set(ids) | set(GENERAL_IDS))
    return {
        "id": pid(idx),
        "name": f"Dr. {first} {last}",
        "title": title,
        "specialty": spec,
        "location_ids": locs,
        "accepting_new_patients": accepting,
        "languages": random.choice(LANGUAGES),
        "appointment_type_ids": ids,
    }


idx = 0
for (f, l, t, s) in forced:
    providers.append(make_provider(idx, f, l, t, s))
    idx += 1
# weight specialties toward primary care
weighted_specs = (["Family Medicine"] * 5 + ["Internal Medicine"] * 4 +
                  ["Pediatrics"] * 3 + SPECIALTIES)
while idx < N_PROVIDERS:
    f = random.choice(FIRST)
    l = random.choice(LAST)
    t = random.choice(TITLES)
    s = random.choice(weighted_specs)
    providers.append(make_provider(idx, f, l, t, s))
    idx += 1

# Make at least one provider explicitly practice at BOTH "Mission" locations,
# so "book with that provider" still needs a location to disambiguate.
mission_locs = [l["id"] for l in locations if l["name"].startswith("Mission")]
if len(mission_locs) >= 2:
    providers[0]["location_ids"] = sorted(set(providers[0]["location_ids"]) | set(mission_locs[:2]))

# ---- weekly availability (recurring; no stale absolute dates) ----------------
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
BLOCKS = [("09:00", "12:00"), ("13:00", "17:00"), ("08:00", "11:00"), ("14:00", "18:00")]
schedules = []
sidx = 0
for p in providers:
    n_blocks = random.choice([1, 2, 2, 3])
    for _ in range(n_blocks):
        loc = random.choice(p["location_ids"])
        day = random.choice(WEEKDAYS)
        start, end = random.choice(BLOCKS)
        schedules.append({
            "id": f"sched_{sidx:04d}",
            "provider_id": p["id"],
            "location_id": loc,
            "weekday": day,
            "start": start,
            "end": end,
        })
        sidx += 1

# ---- policies (the restrictions a correct agent must honor) ------------------
policies = [
    "A provider can only be booked at a location listed in their location_ids.",
    "A provider can only be booked for an appointment type listed in their appointment_type_ids.",
    "An appointment type with a required_capability can only be booked at a location whose capabilities include that capability (e.g. MRI/X-Ray/CT need 'imaging').",
    "Appointment types with requires_referral = true need a referral on file before booking.",
    "A new patient may only book appointment types where new_patients_allowed = true, and only with providers where accepting_new_patients = true.",
    "When the caller names a provider who practices at multiple locations, the location must be disambiguated before booking.",
]

catalog = {
    "metadata": {
        "description": "Synthetic clinic catalog for the context-management challenge. Deterministic (seed=7). Intentionally large and messy.",
        "counts": {
            "locations": len(locations),
            "providers": len(providers),
            "appointment_types": len(appointment_types),
            "schedules": len(schedules),
        },
    },
    "policies": policies,
    "locations": locations,
    "providers": providers,
    "appointment_types": appointment_types,
    "schedules": schedules,
}

out = sys.argv[1] if len(sys.argv) > 1 else "catalog.json"
with open(out, "w") as fh:
    json.dump(catalog, fh, indent=2)

# quick stats to stderr
print("counts:", catalog["metadata"]["counts"], file=sys.stderr)
names = {}
for p in providers:
    names[p["name"]] = names.get(p["name"], 0) + 1
dups = {n: c for n, c in names.items() if c > 1}
print("duplicate provider names:", dups, file=sys.stderr)
print("imaging locations:", len(imaging_locs), file=sys.stderr)
