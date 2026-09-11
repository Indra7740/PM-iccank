import sqlite3
import shutil
from datetime import date, timedelta
from pathlib import Path
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

# ==========================================
# 1. DATABASE SETUP
# ==========================================

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DATABASE_PATH = DATA_DIR / "project_manager.db"

# Preset hari kerja. Senin = 0 ... Minggu = 6 (mengikuti date.weekday())
DAY_NAMES = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
WORKING_DAY_PRESETS = {
    "7 Hari Kerja (Senin - Minggu)": [0, 1, 2, 3, 4, 5, 6],
    "6 Hari Kerja (Senin - Sabtu)": [0, 1, 2, 3, 4, 5],
    "5 Hari Kerja (Senin - Jumat)": [0, 1, 2, 3, 4],
}
DEFAULT_WORKING_DAYS_TEXT = "0,1,2,3,4,5,6"


def get_connection():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ensure_column(cursor, table, column, coltype_with_default):
    """Menambahkan kolom baru ke tabel lama jika belum ada (migrasi aman)."""
    cursor.execute(f"PRAGMA table_info({table})")
    existing_cols = [row[1] for row in cursor.fetchall()]
    if column not in existing_cols:
        cursor.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {coltype_with_default}"
        )


def create_tables():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            start_date TEXT NOT NULL,
            end_date TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            start_date TEXT NOT NULL,
            duration INTEGER NOT NULL,
            weight REAL NOT NULL,
            predecessor_id INTEGER,
            actual_progress REAL DEFAULT 0,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY (predecessor_id) REFERENCES tasks(id) ON DELETE SET NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            actual REAL DEFAULT 0,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
            UNIQUE(task_id, date)
        )
    """)

    # --- Migrasi: tambah kolom hari kerja untuk proyek & pekerjaan ---
    _ensure_column(
        cursor,
        "projects",
        "default_working_days",
        f"TEXT DEFAULT '{DEFAULT_WORKING_DAYS_TEXT}'",
    )
    _ensure_column(cursor, "tasks", "working_days", "TEXT")

    conn.commit()

    # --- Migrasi: pastikan setiap proyek lama punya nilai default_working_days ---
    cursor.execute(
        "UPDATE projects SET default_working_days = ? WHERE default_working_days IS NULL",
        (DEFAULT_WORKING_DAYS_TEXT,),
    )
    conn.commit()

    # --- Migrasi data progress lama: jika sebuah pekerjaan sudah punya
    # actual_progress > 0 tapi belum pernah tercatat di tabel progress
    # (kasus lama sebelum perbaikan), buat satu baris riwayat baseline
    # per hari ini supaya datanya tidak hilang dan Kurva S tetap konsisten.
    cursor.execute(
        """
        SELECT t.id, t.actual_progress FROM tasks t
        WHERE t.actual_progress > 0
        AND NOT EXISTS (SELECT 1 FROM progress p WHERE p.task_id = t.id)
        """
    )
    orphan_tasks = cursor.fetchall()
    for row in orphan_tasks:
        cursor.execute(
            """
            INSERT OR IGNORE INTO progress (task_id, date, actual)
            VALUES (?, ?, ?)
            """,
            (row["id"], date.today().isoformat(), row["actual_progress"]),
        )
    conn.commit()
    conn.close()


# Inisialisasi tabel saat aplikasi dijalankan
create_tables()

# ==========================================
# 2. UTIL HARI KERJA
# ==========================================


def parse_working_days(text):
    if not text:
        return list(range(7))
    return sorted(int(x) for x in text.split(",") if x != "")


def working_days_to_text(days):
    return ",".join(str(d) for d in sorted(set(days)))


def preset_name_for_days(days):
    days_sorted = sorted(days)
    for preset_name, preset_days in WORKING_DAY_PRESETS.items():
        if sorted(preset_days) == days_sorted:
            return preset_name
    return "Kustom"


def working_days_picker(label, default_days, key_prefix):
    """Widget untuk memilih hari kerja: preset atau kustom (checkbox per hari)."""
    options = list(WORKING_DAY_PRESETS.keys()) + ["Kustom"]
    default_preset = preset_name_for_days(default_days)
    index = options.index(default_preset) if default_preset in options else 0

    choice = st.radio(
        label, options, index=index, key=f"{key_prefix}_preset", horizontal=True
    )

    if choice == "Kustom":
        default_labels = [DAY_NAMES[d] for d in default_days]
        selected_labels = st.multiselect(
            "Pilih hari kerja",
            DAY_NAMES,
            default=default_labels,
            key=f"{key_prefix}_custom",
        )
        days = sorted(DAY_NAMES.index(lbl) for lbl in selected_labels)
        if not days:
            st.warning("Minimal pilih satu hari kerja. Sementara memakai Senin-Sabtu.")
            days = WORKING_DAY_PRESETS["6 Hari Kerja (Senin - Sabtu)"]
    else:
        days = WORKING_DAY_PRESETS[choice]

    return days


def is_working_day(d, working_days):
    return d.weekday() in working_days


def next_working_day(d, working_days):
    while not is_working_day(d, working_days):
        d += timedelta(days=1)
    return d


def calculate_finish_date(start_date, duration, working_days):
    """start_date HARUS sudah berupa hari kerja. Menghitung tanggal selesai
    setelah `duration` hari kerja (bukan hari kalender) terlampaui."""
    current = start_date
    finish = start_date
    count = 0
    while count < duration:
        if is_working_day(current, working_days):
            count += 1
            finish = current
        if count < duration:
            current += timedelta(days=1)
    return finish


def count_working_days(start, end, working_days):
    if start > end:
        return 0
    count = 0
    current = start
    while current <= end:
        if is_working_day(current, working_days):
            count += 1
        current += timedelta(days=1)
    return count


# ==========================================
# 3. QUERY & CRUD FUNCTIONS
# ==========================================


def get_projects():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM projects ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_project(project_id):
    conn = get_connection()
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def create_project(name, description, start_date, end_date, default_working_days):
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO projects (name, description, start_date, end_date, default_working_days)
        VALUES (?, ?, ?, ?, ?)
    """,
        (
            name,
            description,
            start_date.isoformat(),
            end_date.isoformat(),
            working_days_to_text(default_working_days),
        ),
    )
    conn.commit()
    conn.close()


def delete_project(project_id):
    """Menghapus proyek beserta seluruh pekerjaan dan riwayat progress di
    dalamnya. Aman karena tabel tasks & progress dibuat dengan
    ON DELETE CASCADE dan PRAGMA foreign_keys = ON diaktifkan di setiap koneksi."""
    conn = get_connection()
    conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    conn.commit()
    conn.close()


def get_tasks(project_id):
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT * FROM tasks WHERE project_id = ? ORDER BY id
    """,
        (project_id,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def create_task(
    project_id, name, start_date, duration, weight, predecessor_id, working_days=None
):
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO tasks (project_id, name, start_date, duration, weight, predecessor_id, working_days)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """,
        (
            project_id,
            name,
            start_date.isoformat(),
            duration,
            weight,
            predecessor_id,
            working_days_to_text(working_days) if working_days else None,
        ),
    )
    conn.commit()
    conn.close()


def update_task(
    task_id, name, start_date, duration, weight, predecessor_id, working_days=None
):
    """Catatan: fungsi ini SENGAJA tidak lagi menerima/mengubah actual_progress.
    Progress aktual sekarang HANYA diubah lewat save_progress(), supaya tidak
    ada dua sumber data yang saling tidak sinkron (ini yang jadi penyebab
    bug 'progress tidak terinput / proyek dianggap terlambat')."""
    conn = get_connection()
    conn.execute(
        """
        UPDATE tasks
        SET name = ?, start_date = ?, duration = ?, weight = ?, predecessor_id = ?, working_days = ?
        WHERE id = ?
    """,
        (
            name,
            start_date.isoformat(),
            duration,
            weight,
            predecessor_id,
            working_days_to_text(working_days) if working_days else None,
            task_id,
        ),
    )
    conn.commit()
    conn.close()


def delete_task(task_id):
    conn = get_connection()
    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()


def sync_task_actual_progress(task_id):
    """Menyamakan tasks.actual_progress dengan entri progress bertanggal
    PALING BARU (bukan asal entri terakhir diinput), supaya kolom ini selalu
    konsisten dengan riwayat progress walau input dilakukan mundur (backdate)."""
    conn = get_connection()
    row = conn.execute(
        "SELECT actual FROM progress WHERE task_id = ? ORDER BY date DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE tasks SET actual_progress = ? WHERE id = ?",
            (row["actual"], task_id),
        )
        conn.commit()
    conn.close()


def save_progress(task_id, progress_date, actual):
    """Satu-satunya jalur resmi untuk mengisi progress aktual. Menulis ke
    tabel riwayat (progress) sekaligus menyinkronkan tasks.actual_progress."""
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO progress (task_id, date, actual)
        VALUES (?, ?, ?)
        ON CONFLICT(task_id, date) DO UPDATE SET actual = excluded.actual
    """,
        (task_id, progress_date.isoformat(), actual),
    )
    conn.commit()
    conn.close()
    sync_task_actual_progress(task_id)


def get_latest_progress(task_id, period_date):
    """Mengambil progress aktual pekerjaan pada tanggal <= period_date.
    Jika belum pernah ada input progress sama sekali sebelum tanggal itu,
    dianggap 0 (belum dilaporkan) -- bukan mengambil nilai 'saat ini' dari
    tabel tasks, karena itulah yang dulu menyebabkan Kurva S memakai data
    yang sudah usang / tidak konsisten dengan riwayat."""
    conn = get_connection()
    row = conn.execute(
        """
        SELECT actual FROM progress
        WHERE task_id = ? AND date <= ?
        ORDER BY date DESC LIMIT 1
    """,
        (task_id, period_date),
    ).fetchone()
    conn.close()
    return row["actual"] if row else 0


def get_progress_history(task_id):
    conn = get_connection()
    rows = conn.execute(
        "SELECT date, actual FROM progress WHERE task_id = ? ORDER BY date",
        (task_id,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


# ==========================================
# 4. PERHITUNGAN KURVA S & JADWAL
# ==========================================


def generate_schedule(project_id):
    tasks = get_tasks(project_id)
    if not tasks:
        return []

    project = get_project(project_id)
    default_days = parse_working_days(project["default_working_days"]) if project else list(range(7))

    task_dict = {task["id"]: task for task in tasks}
    schedule = {}
    remaining = set(task_dict.keys())

    while remaining:
        progress = False
        for task_id in list(remaining):
            task = task_dict[task_id]
            predecessor_id = task["predecessor_id"]
            working_days = (
                parse_working_days(task["working_days"])
                if task.get("working_days")
                else default_days
            )

            if predecessor_id is None:
                own_start = date.fromisoformat(task["start_date"])
                start = next_working_day(own_start, working_days)
            elif predecessor_id in schedule:
                predecessor_finish = schedule[predecessor_id]["finish_date"]
                own_start = date.fromisoformat(task["start_date"])
                candidate_start = max(own_start, predecessor_finish + timedelta(days=1))
                start = next_working_day(candidate_start, working_days)
            else:
                continue

            finish = calculate_finish_date(start, task["duration"], working_days)
            schedule[task_id] = {
                "id": task["id"],
                "name": task["name"],
                "start_date": start,
                "finish_date": finish,
                "duration": task["duration"],
                "weight": task["weight"],
                "actual_progress": task["actual_progress"],
                "predecessor_id": predecessor_id,
                "working_days": working_days,
            }
            remaining.remove(task_id)
            progress = True

        if not progress:
            raise ValueError(
                "Terjadi masalah pada predecessor. Periksa dependency melingkar."
            )

    return list(schedule.values())


def generate_periods(schedule):
    if not schedule:
        return []

    project_start = min(task["start_date"] for task in schedule)
    project_finish = max(task["finish_date"] for task in schedule)
    periods = []
    current = project_start

    while current <= project_finish:
        period_end = min(current + timedelta(days=6), project_finish)
        periods.append({"start": current, "end": period_end})
        current = period_end + timedelta(days=1)

    return periods


def calculate_planned_s_curve(project_id):
    schedule = generate_schedule(project_id)
    if not schedule:
        return []

    periods = generate_periods(schedule)
    result = []
    cumulative = 0

    for period in periods:
        weekly_weight = 0
        for task in schedule:
            overlap_start = max(task["start_date"], period["start"])
            overlap_end = min(task["finish_date"], period["end"])

            if overlap_start <= overlap_end:
                # Hanya hitung hari KERJA dalam rentang overlap, bukan hari kalender,
                # supaya sebaran bobot mingguan konsisten dengan hari kerja pekerjaan ini.
                overlap_working_days = count_working_days(
                    overlap_start, overlap_end, task["working_days"]
                )
                contribution = (
                    task["weight"] * overlap_working_days / task["duration"]
                )
                weekly_weight += contribution

        cumulative += weekly_weight
        result.append(
            {
                "period": period["end"].isoformat(),
                "planned_weekly": round(weekly_weight, 2),
                "planned_cumulative": round(min(cumulative, 100), 2),
            }
        )
    return result


def calculate_actual_cumulative(project_id, period_date):
    schedule = generate_schedule(project_id)
    total = sum(
        (task["weight"] * get_latest_progress(task["id"], period_date) / 100)
        for task in schedule
    )
    return min(total, 100)


def generate_s_curve_data(project_id):
    schedule = generate_schedule(project_id)
    if not schedule:
        return []

    periods = generate_periods(schedule)
    result = []
    previous_actual = 0
    planned_data = calculate_planned_s_curve(project_id)

    for index, period in enumerate(periods):
        period_date = period["end"].isoformat()
        actual_cumulative = calculate_actual_cumulative(
            project_id, period_date
        )

        if index < len(planned_data):
            planned_cumulative = planned_data[index]["planned_cumulative"]
            planned_weekly = planned_data[index]["planned_weekly"]
        else:
            planned_cumulative = 0
            planned_weekly = 0

        actual_weekly = actual_cumulative - previous_actual
        deviation = actual_cumulative - planned_cumulative

        result.append(
            {
                "period": period_date,
                "planned_weekly": round(planned_weekly, 2),
                "planned_cumulative": round(planned_cumulative, 2),
                "actual_weekly": round(max(actual_weekly, 0), 2),
                "actual_cumulative": round(actual_cumulative, 2),
                "deviation": round(deviation, 2),
            }
        )
        previous_actual = actual_cumulative

    return result


def get_project_status(project_id):
    data = generate_s_curve_data(project_id)
    if not data:
        return "BELUM ADA DATA"

    latest = data[-1]
    deviation = latest["deviation"]
    if deviation > 1:
        return "LEBIH CEPAT"
    elif deviation < -1:
        return "TERLAMBAT"
    return "SESUAI RENCANA"


# ==========================================
# 5. STYLING & KOMPONEN VISUAL
# ==========================================

STATUS_STYLES = {
    "TERLAMBAT": {"color": "#DC2626", "bg": "#FEE2E2", "icon": "⚠️"},
    "LEBIH CEPAT": {"color": "#0369A1", "bg": "#DBEAFE", "icon": "🚀"},
    "SESUAI RENCANA": {"color": "#15803D", "bg": "#DCFCE7", "icon": "✅"},
    "BELUM ADA DATA": {"color": "#6B7280", "bg": "#F3F4F6", "icon": "❔"},
}


def inject_custom_css():
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Poppins:wght@500;600;700&family=Inter:wght@400;500;600&display=swap');

        html, body, [class*="css"]  {
            font-family: 'Inter', sans-serif;
        }

        /* Latar utama gelap */
        .stApp {
            background-color: #0B1220;
        }
        .main .block-container {
            padding-top: 1.5rem;
            max-width: 1200px;
        }
        .stApp, .stApp p, .stApp span, .stApp label, .stApp h1,
        .stApp h2, .stApp h3, .stApp h4, .stApp li {
            color: #E2E8F0;
        }

        /* Sidebar */
        [data-testid="stSidebar"] {
            background: linear-gradient(180deg, #1E293B 0%, #0B1220 100%);
        }
        [data-testid="stSidebar"] * {
            color: #E2E8F0 !important;
        }
        [data-testid="stSidebar"] .stRadio div[role="radiogroup"] label {
            background: rgba(255,255,255,0.05);
            border-radius: 10px;
            padding: 8px 12px;
            margin-bottom: 6px;
            transition: background 0.15s ease;
        }
        [data-testid="stSidebar"] .stRadio div[role="radiogroup"] label:hover {
            background: rgba(255,255,255,0.14);
        }

        /* Header banner */
        .app-banner {
            background: linear-gradient(120deg, #4338CA 0%, #2563EB 60%, #0891B2 100%);
            border-radius: 18px;
            padding: 28px 32px;
            margin-bottom: 24px;
            box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
        }
        .app-banner h1 {
            font-family: 'Poppins', sans-serif;
            color: white;
            font-size: 1.7rem;
            margin: 0 0 4px 0;
        }
        .app-banner p {
            color: rgba(255,255,255,0.85);
            margin: 0;
            font-size: 0.95rem;
        }

        /* Metric cards */
        .metric-card {
            background: #16213A;
            border-radius: 16px;
            padding: 18px 20px;
            box-shadow: 0 2px 10px rgba(0, 0, 0, 0.35);
            border: 1px solid #2A3A55;
            height: 100%;
        }
        .metric-card .metric-icon {
            font-size: 1.4rem;
        }
        .metric-card .metric-label {
            color: #94A3B8;
            font-size: 0.82rem;
            font-weight: 500;
            margin-top: 4px;
        }
        .metric-card .metric-value {
            font-family: 'Poppins', sans-serif;
            color: #F1F5F9;
            font-size: 1.6rem;
            font-weight: 700;
            margin-top: 2px;
        }

        /* Status badge (warna dibuat tetap terang supaya kontras di background gelap) */
        .status-badge {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 8px 16px;
            border-radius: 999px;
            font-weight: 700;
            font-size: 0.95rem;
        }

        /* Section title */
        .section-title {
            font-family: 'Poppins', sans-serif;
            font-weight: 600;
            color: #F1F5F9;
            border-left: 5px solid #3B82F6;
            padding-left: 10px;
            margin: 6px 0 14px 0;
        }

        /* Tombol - warna dibuat eksplisit supaya SELALU terlihat, tidak
           bergantung tema bawaan browser/OS */
        .stButton > button, .stFormSubmitButton > button {
            background-color: #2563EB !important;
            color: #FFFFFF !important;
            border-radius: 10px !important;
            font-weight: 600 !important;
            border: 1px solid #3B82F6 !important;
            transition: transform 0.08s ease, box-shadow 0.08s ease, background-color 0.15s ease;
        }
        .stButton > button:hover, .stFormSubmitButton > button:hover {
            background-color: #3B82F6 !important;
            transform: translateY(-1px);
            box-shadow: 0 4px 14px rgba(59, 130, 246, 0.45);
        }
        .stButton > button:disabled {
            background-color: #334155 !important;
            color: #94A3B8 !important;
            border: 1px solid #334155 !important;
        }
        /* Tombol hapus (kind=primary dipakai khusus di tombol hapus proyek) */
        button[kind="primary"] {
            background-color: #DC2626 !important;
            border: 1px solid #F87171 !important;
        }
        button[kind="primary"]:hover {
            background-color: #EF4444 !important;
        }

        /* Input & select fields */
        .stTextInput input, .stNumberInput input, .stDateInput input,
        .stTextArea textarea {
            background-color: #16213A !important;
            color: #F1F5F9 !important;
            border: 1px solid #2A3A55 !important;
        }
        [data-baseweb="select"] > div {
            background-color: #16213A !important;
            border-color: #2A3A55 !important;
            color: #F1F5F9 !important;
        }

        /* Zona berbahaya (hapus proyek) */
        .danger-zone {
            border: 1px solid #7F1D1D;
            background: #2A0E0E;
            border-radius: 14px;
            padding: 16px 20px;
            margin-top: 8px;
        }
        .danger-zone-title {
            color: #FCA5A5;
            font-weight: 700;
            font-family: 'Poppins', sans-serif;
            margin-bottom: 4px;
        }

        /* Expander (daftar pekerjaan) */
        [data-testid="stExpander"] {
            border-radius: 14px !important;
            border: 1px solid #2A3A55 !important;
            overflow: hidden;
            margin-bottom: 10px;
            background: #101A2E;
        }
        [data-testid="stExpander"] summary {
            font-weight: 600 !important;
            background: #16213A;
            color: #F1F5F9 !important;
        }

        /* Tabel data */
        [data-testid="stDataFrame"] {
            background-color: #16213A;
            border-radius: 12px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_banner(title, subtitle):
    st.markdown(
        f"""
        <div class="app-banner">
            <h1>{title}</h1>
            <p>{subtitle}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_section_title(text):
    st.markdown(f'<div class="section-title">{text}</div>', unsafe_allow_html=True)


def render_metric_card(icon, label, value, column):
    with column:
        st.markdown(
            f"""
            <div class="metric-card">
                <div class="metric-icon">{icon}</div>
                <div class="metric-label">{label}</div>
                <div class="metric-value">{value}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )


def render_status_badge(status):
    style = STATUS_STYLES.get(status, STATUS_STYLES["BELUM ADA DATA"])
    st.markdown(
        f"""
        <span class="status-badge" style="background:{style['bg']}; color:{style['color']};">
            {style['icon']} {status}
        </span>
        """,
        unsafe_allow_html=True,
    )


# ==========================================
# 6. TAMPILAN FORM (UI)
# ==========================================


def project_form():
    st.header("📁 Data Proyek")

    st.subheader("➕ Buat Proyek Baru")
    with st.form("project_form"):
        name = st.text_input("Nama proyek")
        description = st.text_area("Deskripsi proyek")
        start_date = st.date_input("Tanggal mulai", value=date.today())
        end_date = st.date_input(
            "Tanggal selesai", value=date.today() + timedelta(days=30)
        )
        st.markdown("**Hari kerja default proyek** (bisa di-override per pekerjaan)")
        default_days = working_days_picker(
            "Pola hari kerja", WORKING_DAY_PRESETS["6 Hari Kerja (Senin - Sabtu)"], "new_project_wd"
        )
        submitted = st.form_submit_button("💾 Simpan Proyek")

        if submitted:
            if not name.strip():
                st.error("Nama proyek wajib diisi.")
            elif end_date < start_date:
                st.error(
                    "Tanggal selesai tidak boleh lebih awal dari tanggal mulai."
                )
            else:
                create_project(name, description, start_date, end_date, default_days)
                st.success("Proyek berhasil dibuat!")
                st.rerun()

    projects = get_projects()
    if not projects:
        st.info("Belum ada proyek. Silakan buat proyek terlebih dahulu.")
        return

    st.divider()
    project_names = {p["id"]: p["name"] for p in projects}
    selected_project = st.selectbox(
        "Pilih proyek",
        options=list(project_names.keys()),
        format_func=lambda x: project_names[x],
    )
    current_project = get_project(selected_project)
    project_default_days = parse_working_days(current_project["default_working_days"])

    st.caption(
        f"Hari kerja default proyek ini: "
        f"{', '.join(DAY_NAMES[d] for d in project_default_days)}"
    )

    with st.expander("⚠️ Zona Berbahaya: Hapus Proyek Ini"):
        st.markdown(
            f"""
            <div class="danger-zone">
                <div class="danger-zone-title">⚠️ Hapus Proyek Permanen</div>
                <div>Menghapus proyek <b>{current_project['name']}</b> akan ikut menghapus
                <b>semua pekerjaan</b> dan <b>seluruh riwayat progress</b> di dalamnya.
                Tindakan ini <b>tidak bisa dibatalkan</b>.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.write("")
        confirm_text = st.text_input(
            f"Ketik nama proyek ini persis untuk konfirmasi: **{current_project['name']}**",
            key=f"confirm_delete_{selected_project}",
        )
        if st.button(
            "🗑️ Hapus Proyek Ini Secara Permanen",
            key=f"delete_project_{selected_project}",
            type="primary",
        ):
            if confirm_text.strip() == current_project["name"]:
                delete_project(selected_project)
                st.success("Proyek berhasil dihapus.")
                st.rerun()
            else:
                st.error("Nama proyek yang diketik tidak cocok. Proyek TIDAK dihapus.")

    st.subheader("➕ Tambah Pekerjaan")
    tasks = get_tasks(selected_project)
    predecessor_options = {None: "Tidak ada predecessor"}
    for task in tasks:
        predecessor_options[task["id"]] = task["name"]

    with st.form("task_form"):
        task_name = st.text_input("Nama pekerjaan")
        task_start = st.date_input(
            "Tanggal mulai pekerjaan", value=date.today()
        )
        duration = st.number_input("Durasi (hari KERJA)", min_value=1, value=7)
        weight = st.number_input(
            "Bobot (%)",
            min_value=0.0,
            max_value=100.0,
            value=10.0,
            step=0.5,
        )
        predecessor = st.selectbox(
            "Predecessor",
            options=list(predecessor_options.keys()),
            format_func=lambda x: predecessor_options[x],
        )
        st.markdown("**Hari kerja pekerjaan ini**")
        task_working_days = working_days_picker(
            "Pola hari kerja pekerjaan", project_default_days, "new_task_wd"
        )
        submitted = st.form_submit_button("➕ Tambahkan Pekerjaan")

        if submitted:
            create_task(
                selected_project,
                task_name,
                task_start,
                duration,
                weight,
                predecessor,
                task_working_days,
            )
            st.success("Pekerjaan berhasil ditambahkan.")
            st.rerun()

    st.divider()
    st.subheader("📋 Daftar Pekerjaan")
    tasks = get_tasks(selected_project)
    if not tasks:
        st.info("Belum ada pekerjaan.")
        return

    total_weight = sum(task["weight"] for task in tasks)
    st.metric("Total Bobot", f"{total_weight:.2f}%")
    if abs(total_weight - 100) > 0.01:
        st.warning(
            f"Total bobot harus 100%. Saat ini tercatat {total_weight:.2f}%."
        )

    for task in tasks:
        task_days = (
            parse_working_days(task["working_days"])
            if task.get("working_days")
            else project_default_days
        )
        with st.expander(f"🔹 {task['name']} ({task['weight']:.2f}%)"):
            col1, col2 = st.columns(2)
            with col1:
                new_name = st.text_input(
                    "Nama pekerjaan",
                    value=task["name"],
                    key=f"name_{task['id']}",
                )
                new_start = st.date_input(
                    "Tanggal mulai",
                    value=date.fromisoformat(task["start_date"]),
                    key=f"start_{task['id']}",
                )
                new_duration = st.number_input(
                    "Durasi (hari kerja)",
                    min_value=1,
                    value=int(task["duration"]),
                    key=f"duration_{task['id']}",
                )

            with col2:
                pred_keys = list(predecessor_options.keys())
                pred_idx = (
                    pred_keys.index(task["predecessor_id"])
                    if task["predecessor_id"] in pred_keys
                    else 0
                )
                predecessor = st.selectbox(
                    "Predecessor",
                    options=pred_keys,
                    index=pred_idx,
                    format_func=lambda x: predecessor_options[x],
                    key=f"pred_{task['id']}",
                )
                new_weight = st.number_input(
                    "Bobot (%)",
                    min_value=0.0,
                    max_value=100.0,
                    value=float(task["weight"]),
                    step=0.5,
                    key=f"weight_{task['id']}",
                )
                st.metric(
                    "Progress aktual saat ini",
                    f"{task['actual_progress'] or 0:.1f}%",
                    help="Diambil dari riwayat progress terbaru. Ubah lewat bagian 'Update Progress Aktual' di bawah.",
                )

            st.markdown("**Hari kerja pekerjaan ini**")
            new_working_days = working_days_picker(
                "Pola hari kerja", task_days, f"task_wd_{task['id']}"
            )

            col_save, col_delete = st.columns(2)
            with col_save:
                if st.button("💾 Simpan Perubahan Jadwal", key=f"save_{task['id']}"):
                    update_task(
                        task["id"],
                        new_name,
                        new_start,
                        new_duration,
                        new_weight,
                        predecessor,
                        new_working_days,
                    )
                    st.success("Data jadwal berhasil diperbarui.")
                    st.rerun()

            with col_delete:
                if st.button("🗑️ Hapus Pekerjaan", key=f"delete_{task['id']}"):
                    delete_task(task["id"])
                    st.success("Pekerjaan dihapus.")
                    st.rerun()

            st.divider()
            st.markdown("**📌 Update Progress Aktual**")
            st.caption(
                "Ini satu-satunya cara resmi mengisi progress. Pilih tanggal pelaporan "
                "(boleh mundur untuk mengisi progress minggu lalu yang terlewat), lalu simpan."
            )
            prog_col1, prog_col2 = st.columns(2)
            with prog_col1:
                progress_date = st.date_input(
                    "Tanggal progress",
                    value=date.today(),
                    max_value=date.today(),
                    key=f"progdate_{task['id']}",
                )
            with prog_col2:
                progress_value = st.number_input(
                    "Progress aktual (%) pada tanggal tsb",
                    min_value=0.0,
                    max_value=100.0,
                    value=float(task["actual_progress"] or 0),
                    step=1.0,
                    key=f"progval_{task['id']}",
                )
            if st.button("📌 Simpan Progress", key=f"progress_{task['id']}"):
                save_progress(task["id"], progress_date, progress_value)
                st.success("Progress berhasil disimpan dan disinkronkan ke Kurva S.")
                st.rerun()

            history = get_progress_history(task["id"])
            if history:
                with st.expander("Riwayat progress pekerjaan ini"):
                    st.dataframe(pd.DataFrame(history), use_container_width=True)


# ==========================================
# 7. DASHBOARD & APLIKASI UTAMA (STREAMLIT)
# ==========================================

st.set_page_config(
    page_title="Project Manager", page_icon="🏗️", layout="wide"
)

inject_custom_css()

st.sidebar.markdown(
    """
    <div style="padding: 10px 4px 20px 4px;">
        <div style="font-family:'Poppins',sans-serif; font-size:1.25rem; font-weight:700; color:white;">
            🏗️ PROJECT MANAGER
        </div>
        <div style="font-size:0.8rem; color:#94A3B8; margin-top:2px;">
            Project Scheduling & S-Curve
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

menu = st.sidebar.radio(
    "Menu",
    [
        "📁 Data Proyek",
        "📊 Jadwal Proyek",
        "📈 Kurva S",
        "📋 Dashboard",
        "👷 Arsip Tenaga Kerja",
        "💾 Backup & Pindah Data",
    ],
    label_visibility="collapsed",
)

if menu == "📁 Data Proyek":
    render_banner("📁 Data Proyek", "Kelola proyek, pekerjaan, jadwal, dan hari kerja.")
    project_form()

elif menu == "📊 Jadwal Proyek":
    render_banner("📊 Jadwal Proyek", "Lihat urutan pekerjaan dan Gantt chart proyek.")
    projects = get_projects()

    if not projects:
        st.info("Belum ada proyek.")
    else:
        project_dict = {p["id"]: p["name"] for p in projects}
        project_id = st.selectbox(
            "Pilih proyek",
            list(project_dict.keys()),
            format_func=lambda x: project_dict[x],
        )

        try:
            schedule = generate_schedule(project_id)
            if not schedule:
                st.info("Belum ada pekerjaan.")
            else:
                df = pd.DataFrame(schedule)
                df["start_date"] = pd.to_datetime(df["start_date"])
                df["finish_date"] = pd.to_datetime(df["finish_date"])
                df["actual_progress"] = df["actual_progress"].fillna(0)

                render_section_title("📋 Tabel Jadwal")
                st.dataframe(
                    df[
                        [
                            "name",
                            "start_date",
                            "finish_date",
                            "duration",
                            "weight",
                            "actual_progress",
                        ]
                    ],
                    use_container_width=True,
                    hide_index=True,
                )

                render_section_title("📅 Gantt Chart")
                fig = px.timeline(
                    df,
                    x_start="start_date",
                    x_end="finish_date",
                    y="name",
                    color="actual_progress",
                    color_continuous_scale=[
                        (0.0, "#F87171"),
                        (0.5, "#FBBF24"),
                        (1.0, "#22C55E"),
                    ],
                    range_color=[0, 100],
                    hover_data=["duration", "weight", "actual_progress"],
                )
                fig.update_yaxes(autorange="reversed", title=None, gridcolor="#26344F")
                fig.update_xaxes(title=None, gridcolor="#26344F")
                fig.update_layout(
                    plot_bgcolor="#101A2E",
                    paper_bgcolor="#101A2E",
                    font=dict(family="Inter, sans-serif", color="#E2E8F0"),
                    coloraxis_colorbar=dict(title="Progress (%)"),
                    margin=dict(l=10, r=10, t=20, b=10),
                )
                fig.update_traces(marker_line_color="rgba(255,255,255,0.2)", marker_line_width=1)
                st.plotly_chart(fig, use_container_width=True)
        except Exception as e:
            st.error(f"Gagal membuat jadwal: {e}")

elif menu == "📈 Kurva S":
    render_banner("📈 Kurva S", "Bandingkan progress rencana vs aktual dari waktu ke waktu.")
    projects = get_projects()

    if not projects:
        st.info("Belum ada proyek.")
    else:
        project_dict = {p["id"]: p["name"] for p in projects}
        project_id = st.selectbox(
            "Pilih proyek",
            list(project_dict.keys()),
            format_func=lambda x: project_dict[x],
            key="scurve_project",
        )

        try:
            data = generate_s_curve_data(project_id)
            if not data:
                st.info("Belum ada data jadwal.")
            else:
                df = pd.DataFrame(data)

                status = get_project_status(project_id)
                latest_deviation = df.iloc[-1]["deviation"]
                col_status, col_dev = st.columns([2, 1])
                with col_status:
                    render_section_title("Status Proyek")
                    render_status_badge(status)
                with col_dev:
                    deviasi_label = "Deviasi terakhir"
                    deviasi_val = f"{latest_deviation:+.2f}%"
                    render_metric_card("📐", deviasi_label, deviasi_val, st.container())

                st.write("")
                render_section_title("📈 Grafik Kurva S")

                fig = go.Figure()
                fig.add_trace(
                    go.Scatter(
                        x=df["period"],
                        y=df["planned_cumulative"],
                        mode="lines+markers",
                        name="Rencana",
                        line=dict(color="#94A3B8", width=3, dash="dash"),
                        marker=dict(size=6, color="#CBD5E1"),
                    )
                )
                fig.add_trace(
                    go.Scatter(
                        x=df["period"],
                        y=df["actual_cumulative"],
                        mode="lines+markers",
                        name="Aktual",
                        line=dict(color="#38BDF8", width=3),
                        marker=dict(size=7, color="#38BDF8"),
                        fill="tozeroy",
                        fillcolor="rgba(56, 189, 248, 0.15)",
                    )
                )
                fig.update_layout(
                    plot_bgcolor="#101A2E",
                    paper_bgcolor="#101A2E",
                    font=dict(family="Inter, sans-serif", color="#E2E8F0"),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
                    margin=dict(l=10, r=10, t=20, b=10),
                    hovermode="x unified",
                )
                fig.update_yaxes(
                    range=[0, 100], title="Progress (%)", gridcolor="#26344F"
                )
                fig.update_xaxes(title="Periode", gridcolor="#26344F")
                st.plotly_chart(fig, use_container_width=True)

                render_section_title("📋 Data Kurva S")
                st.dataframe(df, use_container_width=True, hide_index=True)
        except Exception as e:
            st.error(f"Gagal membuat Kurva S: {e}")

elif menu == "📋 Dashboard":
    render_banner("📋 Dashboard", "Ringkasan cepat kondisi proyek secara keseluruhan.")
    projects = get_projects()

    if not projects:
        st.info("Belum ada proyek.")
    else:
        project_dict = {p["id"]: p["name"] for p in projects}
        project_id = st.selectbox(
            "Pilih proyek",
            list(project_dict.keys()),
            format_func=lambda x: project_dict[x],
            key="dashboard_project",
        )

        tasks = get_tasks(project_id)
        if tasks:
            total_weight = sum(task["weight"] for task in tasks)
            total_actual = sum(
                task["weight"] * (task["actual_progress"] or 0) / 100
                for task in tasks
            )
            status = get_project_status(project_id)

            col1, col2, col3, col4 = st.columns(4)
            render_metric_card("🧱", "Jumlah Pekerjaan", len(tasks), col1)
            render_metric_card("⚖️", "Total Bobot", f"{total_weight:.2f}%", col2)
            render_metric_card("📈", "Progress Proyek", f"{total_actual:.2f}%", col3)
            with col4:
                st.markdown(
                    """
                    <div class="metric-card">
                        <div class="metric-icon">🏁</div>
                        <div class="metric-label">Status</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
                render_status_badge(status)

            st.write("")
            render_section_title("Progress Keseluruhan")
            st.progress(min(int(total_actual), 100))

            st.write("")
            if abs(total_weight - 100) > 0.01:
                st.warning(
                    f"Perhatian: total bobot pekerjaan = {total_weight:.2f}%. Idealnya 100%."
                )
        else:
            st.info("Belum ada pekerjaan.")

elif menu == "💾 Backup & Pindah Data":
    render_banner(
        "💾 Backup & Pindah Data",
        "Pindahkan seluruh data (proyek, pekerjaan, riwayat progress) ke perangkat lain.",
    )

    render_section_title("⬇️ Langkah 1: Unduh Backup di Perangkat Ini")
    st.write(
        "File ini berisi SEMUA data aplikasi: semua proyek, pekerjaan, pengaturan "
        "hari kerja, dan riwayat progress. Simpan file ini lalu pindahkan ke "
        "perangkat baru (lewat email, Google Drive, WhatsApp ke diri sendiri, dsb)."
    )

    if DATABASE_PATH.exists():
        with open(DATABASE_PATH, "rb") as f:
            backup_bytes = f.read()
        st.download_button(
            "⬇️ Unduh Backup Data (.db)",
            data=backup_bytes,
            file_name=f"backup_project_manager_{date.today().isoformat()}.db",
            mime="application/octet-stream",
        )
    else:
        st.info("Belum ada data untuk di-backup.")

    st.write("")
    render_section_title("⬆️ Langkah 2: Import di Perangkat Baru")
    st.markdown(
        """
        <div class="danger-zone">
            <div class="danger-zone-title">⚠️ Perhatian</div>
            <div>Meng-import file backup akan <b>MENGGANTIKAN seluruh data</b> yang
            sedang ada di perangkat ini saat ini. Jika perangkat ini juga sudah
            punya data penting, unduh dulu backup-nya (Langkah 1) sebelum import.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.write("")

    uploaded_file = st.file_uploader(
        "Pilih file backup (.db) dari perangkat lama", type=["db"]
    )
    confirm_import = st.checkbox(
        "Saya paham proses ini akan mengganti seluruh data yang ada di perangkat ini sekarang."
    )

    if uploaded_file is not None and confirm_import:
        if st.button("📥 Import & Ganti Data Sekarang", type="primary"):
            temp_path = DATA_DIR / "_incoming_import.db"
            try:
                with open(temp_path, "wb") as f:
                    f.write(uploaded_file.getbuffer())

                # Validasi: pastikan file yang diupload benar file database aplikasi ini
                check_conn = sqlite3.connect(temp_path)
                table_names = {
                    row[0]
                    for row in check_conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                check_conn.close()

                required_tables = {"projects", "tasks", "progress"}
                if not required_tables.issubset(table_names):
                    st.error(
                        "File tidak valid. Ini bukan file backup dari aplikasi ini."
                    )
                    temp_path.unlink(missing_ok=True)
                else:
                    shutil.copyfile(temp_path, DATABASE_PATH)
                    temp_path.unlink(missing_ok=True)
                    # Jalankan migrasi ulang, jaga-jaga jika backup berasal dari
                    # versi aplikasi yang lebih lama (kolom baru otomatis ditambahkan)
                    create_tables()
                    st.success(
                        "Data berhasil di-import! Semua proyek dari perangkat lama sekarang ada di sini."
                    )
                    st.rerun()
            except Exception as e:
                st.error(f"Gagal mengimpor file: {e}")
                temp_path.unlink(missing_ok=True)
    elif uploaded_file is not None and not confirm_import:
        st.warning("Centang kotak konfirmasi di atas dulu sebelum bisa import.")
        
elif menu == "👷 Arsip Tenaga Kerja":
    st.header("👷 Arsip & Identitas Tenaga Kerja")
    st.caption("Sistem terintegrasi pengelolaan data personil proyek (Supabase Cloud Database).")
    
    # Masukkan link publik website Untitled-8.html yang sudah di-hosting
    hosted_url = "https://superlative-torrone-7c55b0.netlify.app/"
    
    # Render menggunakan iframe dengan tinggi yang disesuaikan
    components.iframe(hosted_url, height=850, scrolling=True)
