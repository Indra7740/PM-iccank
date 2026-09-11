import sqlite3
from datetime import date, timedelta
from pathlib import Path
import pandas as pd
import plotly.express as px
import streamlit as st
import streamlit.components.v1 as components

# ==========================================
# 1. DATABASE SETUP
# ==========================================

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DATABASE_PATH = DATA_DIR / "project_manager.db"


def get_connection():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


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

    conn.commit()
    conn.close()


# Inisialisasi tabel saat aplikasi dijalankan
create_tables()

# ==========================================
# 2. QUERY & CRUD FUNCTIONS
# ==========================================


def get_projects():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM projects ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def create_project(name, description, start_date, end_date):
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO projects (name, description, start_date, end_date)
        VALUES (?, ?, ?, ?)
    """,
        (name, description, start_date.isoformat(), end_date.isoformat()),
    )
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
    project_id, name, start_date, duration, weight, predecessor_id
):
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO tasks (project_id, name, start_date, duration, weight, predecessor_id)
        VALUES (?, ?, ?, ?, ?, ?)
    """,
        (
            project_id,
            name,
            start_date.isoformat(),
            duration,
            weight,
            predecessor_id,
        ),
    )
    conn.commit()
    conn.close()


def update_task(
    task_id, name, start_date, duration, weight, actual_progress, predecessor_id
):
    conn = get_connection()
    conn.execute(
        """
        UPDATE tasks
        SET name = ?, start_date = ?, duration = ?, weight = ?, actual_progress = ?, predecessor_id = ?
        WHERE id = ?
    """,
        (
            name,
            start_date.isoformat(),
            duration,
            weight,
            actual_progress,
            predecessor_id,
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


def save_progress(task_id, progress_date, actual):
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO progress (task_id, date, actual)
        VALUES (?, ?, ?)
        ON CONFLICT(task_id, date) DO UPDATE SET actual = excluded.actual
    """,
        (task_id, progress_date.isoformat(), actual),
    )

    conn.execute(
        """
        UPDATE tasks SET actual_progress = ? WHERE id = ?
    """,
        (actual, task_id),
    )
    conn.commit()
    conn.close()


def get_latest_progress(task_id, period_date):
    conn = get_connection()
    row = conn.execute(
        """
        SELECT actual FROM progress
        WHERE task_id = ? AND date <= ?
        ORDER BY date DESC LIMIT 1
    """,
        (task_id, period_date),
    ).fetchone()

    if row:
        conn.close()
        return row["actual"]

    row = conn.execute(
        "SELECT actual_progress FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    conn.close()
    return (row["actual_progress"] or 0) if row else 0


# ==========================================
# 3. PERHITUNGAN KURVA S & JADWAL
# ==========================================


def calculate_finish_date(start_date, duration):
    return start_date + timedelta(days=duration - 1)


def generate_schedule(project_id):
    tasks = get_tasks(project_id)
    if not tasks:
        return []

    task_dict = {task["id"]: task for task in tasks}
    schedule = {}
    remaining = set(task_dict.keys())

    while remaining:
        progress = False
        for task_id in list(remaining):
            task = task_dict[task_id]
            predecessor_id = task["predecessor_id"]

            if predecessor_id is None:
                start = date.fromisoformat(task["start_date"])
            elif predecessor_id in schedule:
                predecessor_finish = schedule[predecessor_id]["finish_date"]
                own_start = date.fromisoformat(task["start_date"])
                start = max(own_start, predecessor_finish + timedelta(days=1))
            else:
                continue

            finish = calculate_finish_date(start, task["duration"])
            schedule[task_id] = {
                "id": task["id"],
                "name": task["name"],
                "start_date": start,
                "finish_date": finish,
                "duration": task["duration"],
                "weight": task["weight"],
                "actual_progress": task["actual_progress"],
                "predecessor_id": predecessor_id,
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
                overlap_days = (overlap_end - overlap_start).days + 1
                contribution = (
                    task["weight"] * overlap_days / task["duration"]
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
# 4. TAMPILAN FORM (UI)
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
        submitted = st.form_submit_button("💾 Simpan Proyek")

        if submitted:
            if not name.strip():
                st.error("Nama proyek wajib diisi.")
            elif end_date < start_date:
                st.error(
                    "Tanggal selesai tidak boleh lebih awal dari tanggal mulai."
                )
            else:
                create_project(name, description, start_date, end_date)
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
        duration = st.number_input("Durasi (hari)", min_value=1, value=7)
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
        submitted = st.form_submit_button("➕ Tambahkan Pekerjaan")

        if submitted:
            create_task(
                selected_project,
                task_name,
                task_start,
                duration,
                weight,
                predecessor,
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
                    "Durasi",
                    min_value=1,
                    value=int(task["duration"]),
                    key=f"duration_{task['id']}",
                )

            with col2:
                new_weight = st.number_input(
                    "Bobot (%)",
                    min_value=0.0,
                    max_value=100.0,
                    value=float(task["weight"]),
                    step=0.5,
                    key=f"weight_{task['id']}",
                )
                new_actual = st.number_input(
                    "Progress aktual (%)",
                    min_value=0.0,
                    max_value=100.0,
                    value=float(task["actual_progress"] or 0),
                    step=1.0,
                    key=f"actual_{task['id']}",
                )
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

            col_save, col_progress, col_delete = st.columns(3)
            with col_save:
                if st.button("💾 Simpan", key=f"save_{task['id']}"):
                    update_task(
                        task["id"],
                        new_name,
                        new_start,
                        new_duration,
                        new_weight,
                        new_actual,
                        predecessor,
                    )
                    st.success("Data berhasil diperbarui.")
                    st.rerun()

            with col_progress:
                if st.button(
                    "📌 Simpan Progress Hari Ini", key=f"progress_{task['id']}"
                ):
                    save_progress(task["id"], date.today(), new_actual)
                    st.success("Progress tersimpan.")
                    st.rerun()

            with col_delete:
                if st.button("🗑️ Hapus", key=f"delete_{task['id']}"):
                    delete_task(task["id"])
                    st.success("Pekerjaan dihapus.")
                    st.rerun()


# ==========================================
# 5. DASHBOARD & APLIKASI UTAMA (STREAMLIT)
# ==========================================

st.set_page_config(
    page_title="Project Manager", page_icon="🏗️", layout="wide"
)

st.sidebar.title("🏗️ PROJECT MANAGER")
st.sidebar.caption("Project Scheduling & S-Curve")

menu = st.sidebar.radio(
    "Menu", 
    ["📁 Data Proyek", "📊 Jadwal Proyek", "📈 Kurva S", "📋 Dashboard", "👷 Arsip Tenaga Kerja"]
)

if menu == "📁 Data Proyek":
    project_form()

elif menu == "📊 Jadwal Proyek":
    st.header("📊 Jadwal Proyek")
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

                st.subheader("📋 Tabel Jadwal")
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
                )

                st.subheader("📅 Gantt Chart")
                fig = px.timeline(
                    df,
                    x_start="start_date",
                    x_end="finish_date",
                    y="name",
                    hover_data=["duration", "weight", "actual_progress"],
                    title="Jadwal Proyek",
                )
                fig.update_yaxes(autorange="reversed")
                st.plotly_chart(fig, use_container_width=True)
        except Exception as e:
            st.error(f"Gagal membuat jadwal: {e}")

elif menu == "📈 Kurva S":
    st.header("📈 Kurva S")
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
                st.subheader("📋 Data Kurva S")
                st.dataframe(df, use_container_width=True)

                chart_df = df[
                    ["period", "planned_cumulative", "actual_cumulative"]
                ].copy()
                chart_df = chart_df.rename(
                    columns={
                        "period": "Periode",
                        "planned_cumulative": "Rencana",
                        "actual_cumulative": "Aktual",
                    }
                )
                chart_df = chart_df.melt(
                    id_vars="Periode", var_name="Jenis", value_name="Progress"
                )

                fig = px.line(
                    chart_df,
                    x="Periode",
                    y="Progress",
                    color="Jenis",
                    markers=True,
                    title="Kurva S Proyek",
                )
                fig.update_yaxes(range=[0, 100], title="Progress (%)")
                fig.update_xaxes(title="Periode")
                st.plotly_chart(fig, use_container_width=True)

                status = get_project_status(project_id)
                st.subheader(f"Status Proyek: {status}")
        except Exception as e:
            st.error(f"Gagal membuat Kurva S: {e}")

elif menu == "📋 Dashboard":
    st.header("📋 Dashboard")
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

            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Jumlah Pekerjaan", len(tasks))
            col2.metric("Total Bobot", f"{total_weight:.2f}%")
            col3.metric("Progress Proyek", f"{total_actual:.2f}%")
            col4.metric("Status", get_project_status(project_id))

            st.divider()
            if abs(total_weight - 100) > 0.01:
                st.warning(
                    f"Perhatian: total bobot pekerjaan = {total_weight:.2f}%. Idealnya 100%."
                )
        else:
            st.info("Belum ada pekerjaan.")
            
elif menu == "👷 Arsip Tenaga Kerja":
    st.header("👷 Arsip & Identitas Tenaga Kerja")
    st.caption("Sistem terintegrasi pengelolaan data personil proyek (Supabase Cloud Database).")
    
    # Masukkan link publik website Untitled-8.html yang sudah di-hosting
    hosted_url = "https://superlative-torrone-7c55b0.netlify.app/"
    
    # Render menggunakan iframe dengan tinggi yang disesuaikan
    components.iframe(hosted_url, height=850, scrolling=True)