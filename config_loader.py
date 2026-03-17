"""
Config loader for the extensible dental AI assistant.
Loads clinic_config.json and provides typed access to all settings.
"""
import json
import os
import csv
import logging
from typing import List, Dict, Any, Optional, Tuple

logger = logging.getLogger("dental")

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "clinic_config.json")
DEFAULT_DOCTORS_CSV_PATH = os.path.join(os.path.dirname(__file__), "doctors.csv")


class PatientField:
    """Represents a configurable patient data field."""
    def __init__(self, data: dict):
        self.key: str = data["key"]
        self.label: str = data["label"]
        self.type: str = data.get("type", "text")
        self.required: bool = data.get("required", False)
        self.unique_match: bool = data.get("unique_match", False)
        self.extraction_hint: str = data.get("extraction_hint", self.label)


class BookingField:
    """Represents a configurable extra booking data field."""
    def __init__(self, data: dict):
        self.key: str = data["key"]
        self.label: str = data["label"]
        self.type: str = data.get("type", "text")
        self.required: bool = data.get("required", False)
        self.extraction_hint: str = data.get("extraction_hint", self.label)


DAYS_OF_WEEK = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


class Doctor:
    """Represents a doctor loaded from doctors.csv."""
    def __init__(self, data: dict):
        self.id: str = str(data["id"])
        self.name: str = data["name"]
        self.specialty: str = data["specialty"].lower()
        # schedule: day_name → "HH:MM AM - HH:MM PM" or absent if not working
        self.schedule: Dict[str, str] = {}
        for day in DAYS_OF_WEEK:
            hours = data.get(day, "").strip()
            if hours:
                self.schedule[day] = hours


class ServiceMapping:
    """Maps a specialty to its keywords."""
    def __init__(self, data: dict):
        self.specialty: str = data["specialty"].lower()
        self.keywords: List[str] = [k.lower() for k in data.get("keywords", [])]


class ClinicConfig:
    """Central configuration object loaded from clinic_config.json."""

    def __init__(self, config_path: str = DEFAULT_CONFIG_PATH,
                 doctors_path: str = DEFAULT_DOCTORS_CSV_PATH):
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Config file not found: {config_path}")

        with open(config_path, "r") as f:
            raw = json.load(f)

        # ── Clinic identity ──
        clinic = raw.get("clinic", {})
        self.clinic_name: str = clinic.get("name", "Dental Clinic")
        self.clinic_phone: str = clinic.get("phone", "")
        self.clinic_address: str = clinic.get("address", "")
        self.timezone: str = clinic.get("timezone", "UTC")
        self.specialty_label: str = clinic.get("specialty_label", "dental")
        self.assistant_name: str = clinic.get("assistant_name", "Sarah")
        self.assistant_voice: str = clinic.get("assistant_voice", "en-US-JennyNeural")
        self.assistant_voice_rate: str = clinic.get("assistant_voice_rate", "+5%")
        self.parking: bool = clinic.get("parking", False)
        self.insurance: List[str] = clinic.get("insurance", [])

        # ── Clinic working hours (per day, from config) ──
        self.working_hours: Dict[str, str] = {}
        for day, hours in clinic.get("working_hours", {}).items():
            if hours:
                self.working_hours[day.lower()] = hours

        # ── Doctors (from doctors.csv) ──
        if not os.path.exists(doctors_path):
            raise FileNotFoundError(f"Doctors CSV file not found: {doctors_path}")

        self.doctors: List[Doctor] = []
        with open(doctors_path, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if not row.get("id", "").strip():
                    continue
                doc_data: Dict[str, Any] = {
                    "id": row["id"].strip(),
                    "name": row["name"].strip(),
                    "specialty": row["specialty"].strip(),
                }
                for day in DAYS_OF_WEEK:
                    doc_data[day] = row.get(day, "").strip()
                self.doctors.append(Doctor(doc_data))

        # ── Services (from clinic_config.json) ──
        self.services: List[ServiceMapping] = [ServiceMapping(s) for s in raw.get("services", [])]

        # ── Booking settings ──
        booking = raw.get("booking", {})
        self.slot_duration_minutes: int = booking.get("slot_duration_minutes", 60)

        # ── Booking extra fields ──
        self.booking_fields: List[BookingField] = [
            BookingField(f) for f in booking.get("booking_fields", [])
        ]

        # ── Patient fields ──
        self.patient_fields: List[PatientField] = [
            PatientField(f) for f in booking.get("patient_fields", [
                {"key": "name", "label": "Full Name", "type": "text", "required": True},
                {"key": "phone", "label": "Phone Number", "type": "phone", "required": True},
                {"key": "email", "label": "Email Address", "type": "email", "required": True},
            ])
        ]

        # ── LLM settings ──
        llm = raw.get("llm", {})
        self.llm_model: str = llm.get("model", "meta/llama-3.1-8b-instruct")
        self.llm_base_url: str = llm.get("base_url", "https://integrate.api.nvidia.com/v1")
        self.llm_max_tokens_extract: int = llm.get("max_tokens_extract", 500)
        self.llm_max_tokens_respond: int = llm.get("max_tokens_respond", 300)
        self.llm_temperature_extract: float = llm.get("temperature_extract", 0.1)
        self.llm_temperature_respond: float = llm.get("temperature_respond", 0.6)

        # Build lookup maps
        self._specialty_keyword_map: Dict[str, List[str]] = {}
        for svc in self.services:
            self._specialty_keyword_map[svc.specialty] = svc.keywords

        self._doctor_map: Dict[str, Doctor] = {d.id: d for d in self.doctors}

        logger.info(f"[CONFIG] Loaded: clinic='{self.clinic_name}', "
                     f"doctors={len(self.doctors)}, "
                     f"patient_fields={[f.key for f in self.patient_fields]}, "
                     f"required_fields={[f.key for f in self.required_patient_fields]}")

    # ── Accessors ──

    @property
    def required_patient_fields(self) -> List[PatientField]:
        return [f for f in self.patient_fields if f.required]

    @property
    def all_patient_keys(self) -> List[str]:
        return [f.key for f in self.patient_fields]

    @property
    def required_booking_fields(self) -> List[BookingField]:
        return [f for f in self.booking_fields if f.required]

    @property
    def all_booking_keys(self) -> List[str]:
        return [f.key for f in self.booking_fields]

    @property
    def default_doctor_ids(self) -> List[str]:
        """Return the first doctor's ID as fallback, replacing hardcoded ['1']."""
        if self.doctors:
            return [self.doctors[0].id]
        return ["1"]

    @property
    def service_examples(self) -> str:
        """Generate example keywords from configured services for prompts."""
        examples: List[str] = []
        for svc in self.services:
            examples.extend(svc.keywords[:3])
        return ", ".join(examples[:6])

    @property
    def patient_csv_header(self) -> str:
        """Generate dynamic CSV header for patients file."""
        return "patient_id," + ",".join(self.all_patient_keys)

    @property
    def booking_csv_header(self) -> str:
        """CSV header for bookings file."""
        base = "date,start_time,end_time,doctor_id,patient_id,reason"
        if self.booking_fields:
            extra = ",".join(f.key for f in self.booking_fields)
            return f"{base},{extra}"
        return base

    @property
    def doctor_names(self) -> List[str]:
        """List of all doctor names for scrubbing from responses."""
        names = []
        for d in self.doctors:
            names.append(d.name.lower())
            # Also add just the last name
            parts = d.name.lower().replace("dr.", "").replace("dr", "").strip().split()
            names.extend(parts)
        return names

    def get_doctor(self, doctor_id: str) -> Optional[Doctor]:
        return self._doctor_map.get(str(doctor_id))

    def get_specialty_keywords(self, specialty: str) -> List[str]:
        return self._specialty_keyword_map.get(specialty.lower(), [])

    def find_matching_doctors(self, reason: str) -> List[str]:
        """Find doctor IDs whose specialty matches the reason."""
        reason_lower = reason.lower() if reason else ""
        matched_ids: List[str] = []

        for doc in self.doctors:
            keywords = self.get_specialty_keywords(doc.specialty)
            if any(kw in reason_lower for kw in keywords) or doc.specialty in reason_lower:
                matched_ids.append(doc.id)

        if not matched_ids:
            for doc in self.doctors:
                if doc.specialty == "general":
                    matched_ids.append(doc.id)

        return matched_ids

    def is_clinic_open(self, day_name: str) -> bool:
        """Check if the clinic is open on the given day."""
        return day_name.lower() in self.working_hours

    def get_clinic_hours(self, day_name: str) -> Optional[Tuple[str, str]]:
        """Return (start_str, end_str) for the clinic on a given day, or None."""
        hours = self.working_hours.get(day_name.lower())
        if not hours:
            return None
        parts = hours.split(" - ")
        if len(parts) == 2:
            return (parts[0].strip(), parts[1].strip())
        return None

    def get_doctor_working_hours(self, doctor_id: str, day_name: str) -> Optional[Tuple[str, str]]:
        """Return (start_str, end_str) for a doctor on a given day, or None."""
        doc = self.get_doctor(doctor_id)
        if not doc:
            return None
        hours = doc.schedule.get(day_name.lower())
        if not hours:
            return None
        parts = hours.split(" - ")
        if len(parts) == 2:
            return (parts[0].strip(), parts[1].strip())
        return None

    def is_doctor_working_day(self, doctor_id: str, day_name: str) -> bool:
        """Check if a doctor works on the given day."""
        doc = self.get_doctor(doctor_id)
        if not doc:
            return False
        return day_name.lower() in doc.schedule

    def get_patient_field_labels(self, required_only: bool = False) -> str:
        """Get a human-readable list of patient field labels for prompts."""
        fields = self.required_patient_fields if required_only else self.patient_fields
        labels = [f.label for f in fields]
        if len(labels) <= 2:
            return " and ".join(labels)
        return ", ".join(labels[:-1]) + ", and " + labels[-1]

    @staticmethod
    def _format_field_schema(f) -> str:
        type_hints = {"phone": "string (digits, dashes, parentheses)",
                      "date": "string (YYYY-MM-DD)", "email": "string (email format)"}
        hint = type_hints.get(f.type, "string")
        req = " (REQUIRED)" if f.required else " (optional)"
        return f"- '{f.key}': {hint}{req} — {f.extraction_hint}. NULL if not given."

    def get_extraction_fields_json_schema(self) -> str:
        """Build JSON schema description for LLM extraction prompt."""
        all_fields = list(self.patient_fields) + list(self.booking_fields)
        return "\n".join(self._format_field_schema(f) for f in all_fields)

    def get_extraction_json_template(self) -> str:
        """Build the JSON template for LLM extraction output."""
        fields = []
        fields.append('"reason": string|null')
        fields.append('"date": "YYYY-MM-DD"|null')
        fields.append('"time": "HH:MM AM/PM"|null')
        for f in self.patient_fields:
            fields.append(f'"{f.key}": string|null')
        for f in self.booking_fields:
            fields.append(f'"{f.key}": string|null')
        fields.append('"is_modification": false')
        return "{" + ", ".join(fields) + "}"

    def get_booking_field_labels(self, required_only: bool = False) -> str:
        """Get a human-readable list of booking field labels for prompts."""
        fields = self.required_booking_fields if required_only else self.booking_fields
        labels = [f.label for f in fields]
        if not labels:
            return ""
        if len(labels) <= 2:
            return " and ".join(labels)
        return ", ".join(labels[:-1]) + ", and " + labels[-1]

    def ensure_csv_files(self, patients_path: str = "patients.csv",
                         bookings_path: str = "bookings.csv"):
        """Ensure CSV files exist with proper headers per config."""
        if not os.path.exists(patients_path):
            with open(patients_path, "w") as f:
                f.write(self.patient_csv_header + "\n")
            logger.info(f"[CONFIG] Created {patients_path} with header: {self.patient_csv_header}")

        if not os.path.exists(bookings_path):
            with open(bookings_path, "w") as f:
                f.write(self.booking_csv_header + "\n")
            logger.info(f"[CONFIG] Created {bookings_path} with header: {self.booking_csv_header}")

    def validate(self) -> List[str]:
        """Validate the config and return a list of issues."""
        issues = []
        if not self.clinic_name:
            issues.append("clinic.name is required")
        if not self.doctors:
            issues.append("At least one doctor is required")
        if not self.patient_fields:
            issues.append("At least one patient_field is required")
        if not any(f.key == "name" for f in self.patient_fields):
            issues.append("A 'name' patient field is strongly recommended")

        # Check doctors have valid specialties that map to services
        specialties_with_keywords = {s.specialty for s in self.services}
        for doc in self.doctors:
            if doc.specialty not in specialties_with_keywords:
                issues.append(f"Doctor {doc.id} ({doc.name}) has specialty '{doc.specialty}' "
                             f"which has no service keyword mapping")

        # Check booking field key collisions with patient fields and reserved keys
        reserved_keys = {"reason", "date", "time", "is_modification"}
        patient_keys = set(self.all_patient_keys)
        for bf in self.booking_fields:
            if bf.key in reserved_keys:
                issues.append(f"Booking field '{bf.key}' collides with reserved key")
            if bf.key in patient_keys:
                issues.append(f"Booking field '{bf.key}' collides with patient field key")

        return issues
