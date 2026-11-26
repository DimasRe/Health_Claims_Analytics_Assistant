"""
Generator data sintetis untuk mencoba chatbot tanpa data asli.

Seluruh nilai (fasilitas, cabang, biaya, peserta) dibuat acak dan fiktif.
Skema mengikuti config/metrics.yaml dan config/dimensions.yaml.

Pemakaian:
    python scripts/generate_sample_data.py --rows 50000 > sample_data.sql
    createdb claims_demo
    psql -d claims_demo -f sample_data.sql

Opsi:
    --rows N      jumlah baris pelayanan (default 20000)
    --seed N      seed acak agar hasil dapat diulang (default 42)
    --years A B   rentang tahun pelayanan (default 2022 2024)
"""

import argparse
import random
import sys
from datetime import date, timedelta

REGIONS = [(f"{i:02d}", f"DIVRE {i:02d}") for i in range(1, 14)]
TARIFF_REGIONS = [(str(i), f"Regional {i}") for i in range(1, 6)]

# Kode ICD-10 umum (publik). Tuple: kode, nama, kategori kronis, katastrofik.
DIAGNOSES = [
    ("I10", "Hipertensi esensial", "Hipertensi", None),
    ("E11", "Diabetes melitus tipe 2", "Diabetes Melitus", None),
    ("J18", "Pneumonia", None, None),
    ("A09", "Diare dan gastroenteritis", None, None),
    ("J44", "Penyakit paru obstruktif kronik", "PPOK", None),
    ("J45", "Asma", "Asma", None),
    ("I50", "Gagal jantung", None, "Penyakit Jantung"),
    ("I21", "Infark miokard akut", None, "Penyakit Jantung"),
    ("N18", "Penyakit ginjal kronik", None, "Gagal Ginjal"),
    ("I63", "Infark serebral", None, "Stroke"),
    ("C50", "Neoplasma ganas payudara", None, "Kanker"),
    ("O80", "Persalinan tunggal spontan", None, None),
    ("K35", "Apendisitis akut", None, None),
    ("A15", "Tuberkulosis paru", None, None),
    ("H25", "Katarak senilis", None, None),
    ("Z09", "Pemeriksaan lanjutan pascaterapi", None, None),
]

SEGMENTS = [("PBI APBN", 40), ("PBI APBD", 15), ("PPU", 25), ("PBPU", 15), ("BP", 5)]
AGE_GROUPS = [("00-04", 8), ("05-14", 10), ("15-24", 12), ("25-44", 28), ("45-64", 28), ("65+", 14)]
DISCHARGES = [
    ("1", "Atas persetujuan dokter", 85),
    ("2", "Dirujuk", 7),
    ("3", "Atas permintaan sendiri", 4),
    ("4", "Meninggal", 2),
    ("5", "Lain-lain", 2),
]
FACILITY_TYPES = ["A", "B", "C", "D"]
OWNERSHIP = ["Pemerintah", "Swasta", "TNI/Polri"]
NAME_WORDS = ["Sehat", "Sentosa", "Harapan", "Medika", "Mitra", "Husada", "Bakti", "Prima"]

DDL = """\
-- Data sintetis untuk demo. Seluruh nilai fiktif.
DROP TABLE IF EXISTS public.claims;
DROP TABLE IF EXISTS public.regions;

CREATE TABLE public.regions (
    region_code text PRIMARY KEY,
    region_name text NOT NULL
);

CREATE TABLE public.claims (
    claim_id              text PRIMARY KEY,
    member_id             text NOT NULL,
    service_date          date NOT NULL,
    service_type          text NOT NULL,      -- RJTL / RITL
    service_region_code   text NOT NULL REFERENCES public.regions(region_code),
    tariff_region_code    text,
    tariff_region_name    text,
    service_branch_code   text,
    service_branch_name   text,
    facility_code         text,
    facility_name         text,
    facility_kind         text,
    facility_type         text,
    facility_ownership    text,
    primary_dx_code       text,
    primary_dx_name       text,
    secondary_dx_codes    text,               -- dipisah ';'
    severity              text,
    sex                   text,
    age_group             text,
    member_segment        text,
    entitled_class        text,
    ward_class            text,
    catastrophic_category text,
    is_catastrophic       text,
    chronic_category      text,
    is_chronic            text,
    discharge_code        text,
    discharge_name        text,
    billed_amount         numeric(16,2),
    verified_amount       numeric(16,2),
    hospital_cost         numeric(16,2)
);
"""

COLUMNS = [
    "claim_id", "member_id", "service_date", "service_type", "service_region_code",
    "tariff_region_code", "tariff_region_name", "service_branch_code",
    "service_branch_name", "facility_code", "facility_name", "facility_kind",
    "facility_type", "facility_ownership", "primary_dx_code", "primary_dx_name",
    "secondary_dx_codes", "severity", "sex", "age_group", "member_segment",
    "entitled_class", "ward_class", "catastrophic_category", "is_catastrophic",
    "chronic_category", "is_chronic", "discharge_code", "discharge_name",
    "billed_amount", "verified_amount", "hospital_cost",
]


def weighted(rng, items):
    values = [item[:-1] if len(item) > 2 else item[0] for item in items]
    weights = [item[-1] for item in items]
    return rng.choices(values, weights=weights, k=1)[0]


def build_facilities(rng):
    branches, facilities = [], []
    for region_code, _ in REGIONS:
        tariff = rng.choice(TARIFF_REGIONS)
        for b in range(1, 4):
            branch_code = f"{region_code}{b:02d}"
            branch = (branch_code, f"Cabang Fiktif {branch_code}", region_code, tariff)
            branches.append(branch)
            for f in range(1, 4):
                code = f"{branch_code}R{f:03d}"
                kind = "Rumah Sakit" if f < 3 else "Klinik Utama"
                name = f"{'RS' if kind == 'Rumah Sakit' else 'Klinik'} " \
                       f"{rng.choice(NAME_WORDS)} {rng.choice(NAME_WORDS)} {code[-3:]}"
                facilities.append({
                    "code": code, "name": name, "kind": kind,
                    "type": rng.choice(FACILITY_TYPES), "ownership": rng.choice(OWNERSHIP),
                    "branch": branch,
                })
    return facilities


def build_members(rng, count):
    members = []
    for i in range(count):
        segment = weighted(rng, SEGMENTS)
        klass = "Kelas 3" if segment.startswith("PBI") else rng.choice(["Kelas 1", "Kelas 2", "Kelas 3"])
        members.append({
            "id": f"M{i + 1:08d}",
            "sex": rng.choice(["L", "P"]),
            "age": weighted(rng, AGE_GROUPS),
            "segment": segment,
            "class": klass,
        })
    return members


def copy_value(value):
    if value is None:
        return r"\N"
    return str(value).replace("\\", "\\\\").replace("\t", " ").replace("\n", " ")


def generate(rows, seed, first_year, last_year, out):
    rng = random.Random(seed)
    facilities = build_facilities(rng)
    members = build_members(rng, max(1, rows // 3))
    start = date(first_year, 1, 1)
    span = (date(last_year, 12, 31) - start).days

    out.write(DDL)
    out.write("\nCOPY public.regions (region_code, region_name) FROM stdin;\n")
    for code, name in REGIONS:
        out.write(f"{code}\t{name}\n")
    out.write("\\.\n")

    out.write(f"\nCOPY public.claims ({', '.join(COLUMNS)}) FROM stdin;\n")
    for i in range(rows):
        member = rng.choice(members)
        fac = rng.choice(facilities)
        branch_code, branch_name, region_code, tariff = fac["branch"]
        inpatient = rng.random() < 0.15
        dx_code, dx_name, chronic, catastrophic = rng.choice(DIAGNOSES)
        secondary = ";".join(rng.sample([d[0] for d in DIAGNOSES if d[0] != dx_code], rng.randint(0, 3)))

        if inpatient:
            billed = rng.uniform(2_500_000, 30_000_000) * (1.8 if catastrophic else 1.0)
            severity = rng.choices(["I", "II", "III"], weights=[60, 30, 10])[0]
            ward = rng.choice([member["class"], "Kelas 3"])
        else:
            billed = rng.uniform(150_000, 1_200_000) * (1.5 if catastrophic else 1.0)
            severity, ward = "I", None
        verified = billed * rng.uniform(0.82, 1.0)
        hospital = billed * rng.uniform(0.85, 1.25)
        discharge = weighted(rng, DISCHARGES) if inpatient else ("1", "Atas persetujuan dokter")

        row = [
            f"C{i + 1:010d}", member["id"], start + timedelta(days=rng.randint(0, span)),
            "RITL" if inpatient else "RJTL", region_code, tariff[0], tariff[1],
            branch_code, branch_name, fac["code"], fac["name"], fac["kind"], fac["type"],
            fac["ownership"], dx_code, dx_name, secondary or None, severity, member["sex"],
            member["age"], member["segment"], member["class"], ward,
            catastrophic or "Non Katastrofik", "Y" if catastrophic else "N",
            chronic or "Non Kronis", "Y" if chronic else "N",
            discharge[0], discharge[1],
            f"{billed:.2f}", f"{verified:.2f}", f"{hospital:.2f}",
        ]
        out.write("\t".join(copy_value(v) for v in row) + "\n")
    out.write("\\.\n")

    out.write("""
CREATE INDEX ON public.claims (service_date);
CREATE INDEX ON public.claims (service_region_code);
CREATE INDEX ON public.claims (service_type);
ANALYZE public.claims;
ANALYZE public.regions;

-- Opsional: user khusus baca (sesuaikan password):
-- CREATE ROLE analytics_readonly LOGIN PASSWORD 'ganti_password';
-- GRANT USAGE ON SCHEMA public TO analytics_readonly;
-- GRANT SELECT ON public.claims, public.regions TO analytics_readonly;
""")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rows", type=int, default=20000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--years", type=int, nargs=2, default=[2022, 2024], metavar=("AWAL", "AKHIR"))
    args = parser.parse_args()
    generate(args.rows, args.seed, args.years[0], args.years[1], sys.stdout)


if __name__ == "__main__":
    main()
