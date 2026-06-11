from dotenv import load_dotenv
load_dotenv("teacher_coordination.env")

from collections import defaultdict
from datetime import date, datetime, timedelta
import os
import secrets

from flask import Flask, flash, g, redirect, render_template, request, session
from flask_session import Session
from psycopg2 import pool
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from helpers import *

app = Flask(__name__)

app.config["SESSION_PERMANENT"] = False
app.config["SESSION_TYPE"] = "filesystem"
app.config['UPLOAD_FOLDER'] = 'uploads'
Session(app)


connection_pool = pool.SimpleConnectionPool(
    minconn=1,
    maxconn=10,
    host=os.environ.get("DB_HOST", "localhost"),
    database=os.environ.get("DB_NAME"),
    user=os.environ.get("DB_USER"),
    password=os.environ.get("DB_PASSWORD")
)

@app.before_request
def get_db():
    g.db = connection_pool.getconn()

@app.teardown_request
def return_db(exception=None):
    db = g.pop('db', None)
    if db is not None:
        connection_pool.putconn(db)


@app.after_request
def after_request(response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/accept_substitution/<token>")
def accept_substitution(token):
    try:
        with g.db.cursor() as cursor:
            cursor.execute("""
                SELECT
                    s.id,
                    s.status,
                    s.date,
                    ts.start_time,
                    ts.end_time,
                    sub.name AS subject_name,
                    g.name AS grade_name,
                    s.time_slot_id
                FROM substitutions s
                JOIN time_slots ts ON s.time_slot_id = ts.id
                JOIN subjects sub ON ts.subject_id = sub.id
                JOIN grade g ON ts.grade_id = g.id
                WHERE s.token = %s
            """, (token,))
            sub = cursor.fetchone()

            if not sub:
                return error("Invalid or expired link")

            (_, status, absence_date, start_time, end_time,
             subject_name, grade_name, time_slot_id) = sub

            if status == "confirmed":
                return render_template("substitution_taken.html",
                                       subject_name=subject_name,
                                       grade_name=grade_name,
                                       absence_date=absence_date)

            if status == "cancelled":
                return error("This absence request has been cancelled")

    except Exception as e:
        print("ACCEPT SUBSTITUTION ERROR:", e)
        return error("Failed to load substitution details")

    if not session.get("user_id"):
        session["pending_token"] = token
        return redirect("/login")

    # check they're not the absent teacher
    try:
        with g.db.cursor() as cursor:
            cursor.execute("SELECT main_teacher_id FROM time_slots WHERE id = %s", (time_slot_id,))
            main_teacher_id = cursor.fetchone()[0]
    except Exception as e:
        print("ERROR checking main teacher:", e)
        return error("Failed to check substitution eligibility")

    if session["user_id"] == main_teacher_id:
        return error("You can't substitute your own class")

    return render_template("confirm_substitution.html",
                           token=token,
                           subject_name=subject_name,
                           grade_name=grade_name,
                           absence_date=absence_date,
                           start_time=start_time,
                           end_time=end_time)


@app.route("/confirm_substitution/<token>")
@login_required
def confirm_substitution(token):
    try:
        with g.db.cursor() as cursor:
            cursor.execute("""
                SELECT
                    s.id,
                    s.status,
                    s.date,
                    ts.start_time,
                    ts.end_time,
                    sub.name AS subject_name,
                    g.name AS grade_name,
                    u.name AS absent_teacher_name,
                    u.email AS absent_teacher_email,
                    s.time_slot_id
                FROM substitutions s
                JOIN time_slots ts ON s.time_slot_id = ts.id
                JOIN subjects sub ON ts.subject_id = sub.id
                JOIN grade g ON ts.grade_id = g.id
                JOIN users u ON ts.main_teacher_id = u.id
                WHERE s.token = %s
            """, (token,))
            sub = cursor.fetchone()

            if not sub:
                return error("Invalid or expired link")

            (sub_id, status, absence_date, start_time, end_time,
             subject_name, grade_name, absent_teacher_name,
             absent_teacher_email, time_slot_id) = sub

            if status == "confirmed":
                return render_template("substitution_taken.html",
                                       subject_name=subject_name,
                                       grade_name=grade_name,
                                       absence_date=absence_date)

            if status == "cancelled":
                return error("This absence request has been cancelled")

            substitute_id = session["user_id"]

            # lock it
            cursor.execute("""
                UPDATE substitutions
                SET substitute_teacher_id = %s, status = 'confirmed'
                WHERE id = %s AND status = 'pending'
                RETURNING id
            """, (substitute_id, sub_id))

            updated = cursor.fetchone()

            if not updated:
                return render_template("substitution_taken.html",
                                       subject_name=subject_name,
                                       grade_name=grade_name,
                                       absence_date=absence_date)

            g.db.commit()

            # get substitute details for emails
            cursor.execute("SELECT name, email FROM users WHERE id = %s", (substitute_id,))
            substitute = cursor.fetchone()
            substitute_name, substitute_email = substitute

            # notify absent teacher
            send_substitution_confirmed(
                to_email=absent_teacher_email,
                recipient_name=absent_teacher_name,
                substitute_name=substitute_name,
                subject_name=subject_name,
                grade_name=grade_name,
                absence_date=absence_date,
                start_time=start_time,
                end_time=end_time
            )

            # notify substitute
            send_substitution_confirmed(
                to_email=substitute_email,
                recipient_name=substitute_name,
                substitute_name=substitute_name,
                subject_name=subject_name,
                grade_name=grade_name,
                absence_date=absence_date,
                start_time=start_time,
                end_time=end_time,
                is_substitute=True
            )

    except Exception as e:
        g.db.rollback()
        print("CONFIRM SUBSTITUTION ERROR:", e)
        return error("Failed to confirm substitution")

    return render_template("substitution_confirmed.html",
                           subject_name=subject_name,
                           grade_name=grade_name,
                           absence_date=absence_date,
                           start_time=start_time,
                           end_time=end_time)

@app.route("/report_absence", methods=["GET", "POST"])
@login_required
def report_absence():
    user_id = session["user_id"]

    if request.method == "POST":
        slot_id = request.form.get("slot_id")
        absence_date = request.form.get("absence_date")

        # validate the date is at least 1 day away
        chosen_date = datetime.strptime(absence_date, "%Y-%m-%d").date()
        if chosen_date <= date.today() + timedelta(days=2):
            return error("Absence must be reported at least 2 days in advance")
        
        # validate a request doesn't already exist for this slot and date
        try:
            with g.db.cursor() as cursor:
                cursor.execute("SELECT id FROM substitutions WHERE time_slot_id = %s AND date = %s AND status IN ('pending', 'confirmed')", (slot_id, absence_date))
                if cursor.fetchone():
                    return error("An absence request already exists for this slot and date")
                
                # check eligibility before inserting
                cursor.execute("""
                    SELECT u.id FROM teacher_subjects ts_map
                    JOIN users u ON ts_map.teacher_id = u.id
                    WHERE ts_map.subject_id = (SELECT subject_id FROM time_slots WHERE id = %s)
                    AND ts_map.grade_id = (SELECT grade_id FROM time_slots WHERE id = %s)
                    AND ts_map.teacher_id != %s
                """, (slot_id, slot_id, user_id))
                eligible = cursor.fetchall()

                if not eligible:
                    return error("No eligible substitute teachers found for this class. Please contact the admin.")          

                # Only insert if eligible teachers exist
                token = secrets.token_hex(32)

                cursor.execute("INSERT INTO substitutions (time_slot_id, date, token, status) VALUES (%s, %s, %s, 'pending')", (slot_id, absence_date, token))
                
                g.db.commit()

                # Get substitution id for email step
                cursor.execute("SELECT id FROM substitutions WHERE token = %s", (token,))
                sub_id = cursor.fetchone()[0]

        except Exception as e:
            g.db.rollback()
            print("ABSENCE REPORT ERROR:", e)
            return error("Failed to submit absence request. Please try again and contact the admin if the issue persists.")
        
        # Find substitute teachers and send emails
        return redirect(f"/find_substitutes/{sub_id}")
    
    # GET method
    try:
        with g.db.cursor() as cursor:
            cursor.execute("""
                SELECT ts.id,
                ts.day_of_week,
                ts.start_time,
                ts.end_time,
                s.name AS subject,
                g.name AS grade
                FROM time_slots ts
                JOIN subjects s ON ts.subject_id = s.id
                JOIN grade g ON ts.grade_id = g.id
                WHERE ts.main_teacher_id = %s
                ORDER BY ts.day_of_week, ts.start_time
            """, (user_id,))
            slots = cursor.fetchall()

            # get the days of week this teacher has classes
            day_map = {
                "Monday": 0, "Tuesday": 1, "Wednesday": 2,
                "Thursday": 3, "Friday": 4
            }
            teaching_days = set(day_map[slot[1]] for slot in slots)

            # generate valid dates — only days they teach, 2 days ahead, up to 16 weeks
            min_date = date.today() + timedelta(days=2)
            max_date = date.today() + timedelta(weeks=16)
            valid_dates = []
            current = min_date
            while current <= max_date:
                if current.weekday() in teaching_days:
                    valid_dates.append((current.isoformat(), current.strftime("%A, %d %B, %Y")))
                current += timedelta(days=1)

    except Exception as e:
        print("FETCH TEACHER SLOTS ERROR:", e)
        return error("Failed to load your schedule. Please try again and contact the admin if the issue persists.")

    return render_template("report_absence.html", slots=slots, valid_dates=valid_dates)


@app.route("/find_substitutes/<int:sub_id>")
@login_required
def find_substitutes(sub_id):

    try:
        with g.db.cursor() as cursor:
            
            # get the substitution details
            cursor.execute("""
                           SELECT
                            s.id,
                            s.date,
                            s.token,
                            ts.start_time,
                            ts.end_time,
                            ts.subject_id,
                            ts.grade_id,
                            ts.main_teacher_id,
                            sub.name AS subject_name,
                            g.name AS grade_name
                           FROM substitutions s
                           JOIN time_slots ts ON s.time_slot_id = ts.id
                           JOIN subjects sub ON ts.subject_id = sub.id
                           JOIN grade g on ts.grade_id = g.id
                           WHERE s.id = %s
                        """, (sub_id,))
            sub = cursor.fetchone()

            if not sub:
                return error("Substitution error")
            
            (sub_id, absence_date,token, start_time, end_time, subject_id, grade_id, main_teacher_id, subject_name, grade_name) = sub

            # find eligible teachers that can teach this subject for this grade, aren't the absent teacher, don't have another reg or sub class on this day at this time
            cursor.execute("""
                           SELECT u.id, u.name, u.email
                           FROM teacher_subjects ts_map
                           JOIN users u ON ts_map.teacher_id = u.id
                           WHERE ts_map.subject_id = %s
                            AND ts_map.grade_id = %s
                            AND ts_map.teacher_id != %s
                            AND u.id NOT IN (
                                SELECT main_teacher_id FROM time_slots
                                WHERE start_time < %s AND end_time > %s
                                AND day_of_week = (
                                    SELECT day_of_week FROM time_slots
                                    WHERE id = (
                                        SELECT time_slot_id FROM substitutions WHERE id = %s
                                    )
                                )
                           )
                           AND u.id NOT IN (
                            SELECT sub2.substitute_teacher_id
                            FROM substitutions sub2
                            JOIN time_slots ts2 ON sub2.time_slot_id = ts2.id
                            WHERE sub2.date = %s
                                AND sub2.status = 'confirmed'
                                AND ts2.start_time < %s
                                AND ts2.end_time > %s
                                AND sub2.substitute_teacher_id IS NOT NULL
                           )
                        """, (
                            subject_id, grade_id, main_teacher_id, end_time, start_time, sub_id, absence_date, end_time, start_time
                        ))
            
            eligible = cursor.fetchall()

            if not eligible:
                return error("No eligible substitute teachers found. Please contact the admin.")
            
            # send emails to the eligible teachers
            for teacher in eligible:
                send_substitution_request(
                    to_email=teacher[2],
                    teacher_name=teacher[1],
                    subject_name=subject_name,
                    grade_name=grade_name,
                    absence_date=absence_date,
                    start_time=start_time,
                    end_time=end_time,
                    token=token
                )

    except Exception as e:
        print("ERROR FINDING SUBSTITUTES:", e)
        return error("Failed to find substitutes")
    
    return render_template("absence_sent.html", count=len(eligible))


@app.route("/events")
@login_required
def events():
    grade_id = request.args.get("grade_id")
    teacher_id = session["user_id"]
    return load_schedule(grade_id=grade_id, teacher_id=teacher_id)


@app.route("/events/all")
@login_required
def events_all():
    grade_id = request.args.get("grade_id")
    teacher_id = request.args.get("teacher_id")
    return load_schedule(grade_id=grade_id, teacher_id=teacher_id)


@app.route("/timetable")
@login_required
def timetable():
    try:
        with g.db.cursor() as cursor:
            cursor.execute("SELECT id, name FROM grade ORDER BY name")
            grades = cursor.fetchall()
            cursor.execute("SELECT id, name FROM users ORDER BY name")
            teachers = cursor.fetchall()
    except Exception as e:
        print("TIMETABLE ERROR:", e)
        return error("Failed to load timetable")
    
    default_grade = grades[0][0] if grades else None

    return render_template("timetable.html", grades=grades, teachers=teachers, selected_grade=default_grade)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":

        username = request.form.get("username")
        email = request.form.get("email")
        password = request.form.get("password")
        confirmation = request.form.get("confirmation")

        if password != confirmation:
            return error("Passwords must match") 

        hashed_password = generate_password_hash(password)

        try:
            with g.db.cursor() as cursor:
                cursor.execute("SELECT * FROM grade")
                grades = cursor.fetchall()

                cursor.execute("SELECT * FROM subjects")
                subjects = cursor.fetchall()

                user_id = validate_and_insert_user(username, email, hashed_password)

                if not user_id:
                    return error("Username or email already exists")

                with g.db.cursor() as cursor:
                    cursor.execute("SELECT school_name FROM school_settings WHERE id = 1")
                    school_name = cursor.fetchone()[0] or "Teacher Coordination App"
                
                send_registration_confirmation(to_email=email, teacher_name=username, school_name=school_name)
                
                session["user_id"] = user_id

                if user_id:
                    return render_template("teacher_subjects.html", grades=grades, subjects=subjects)

        except Exception as e:
            print("ERROR FETCHING GRADES AND SUBJECTS:", e)
            return error("Failed to load grades and subjects")

    return render_template("register.html")


@app.route("/my_absences")
@login_required
def my_absences():
    user_id = session["user_id"]

    try:
        with g.db.cursor() as cursor:
            cursor.execute("""
                        SELECT
                           sub.id,
                           sub.date,
                           sub.status,
                           sub.requested_at,
                           ts.start_time,
                           ts.end_time,
                           s.name AS subject,
                           g.name AS grade, 
                           u.name AS substitute_name
                        FROM substitutions sub
                        JOIN time_slots ts ON sub.time_slot_id = ts.id
                        JOIN subjects s ON ts.subject_id = s.id
                        JOIN grade g ON ts.grade_id = g.id
                        LEFT JOIN users u ON sub.substitute_teacher_id = u.id
                        WHERE ts.main_teacher_id = %s
                        ORDER BY sub.date DESC
                    """, (user_id,))
            absences = cursor.fetchall()
    except Exception as e:
        print("MY ABSENCES ERROR:", e)
        return error("Failed to load your absences")
    
    return render_template("my_absences.html", absences=absences, today=date.today())


@app.route("/cancel_absence/<int:sub_id>", methods=["POST"])
@login_required
def cancel_absence(sub_id):
    user_id = session["user_id"]

    try:
        with g.db.cursor() as cursor:
            # fetch the substition and double check that it confirms to this teacher
            cursor.execute("""
                        SELECT
                           sub.id,
                           sub.status,
                           sub.date,
                           sub.token,
                           ts.start_time,
                           ts.end_time,
                           ts.main_teacher_id,
                           s.name AS subject,
                           g.name AS grade
                        FROM substitutions sub
                        JOIN time_slots ts ON sub.time_slot_id = ts.id
                        JOIN subjects s ON ts.subject_id = s.id
                        JOIN grade g ON ts.grade_id = g.id
                        WHERE sub.id = %s
                    """, (sub_id,))
            sub = cursor.fetchone()

            if not sub:
                return error("Absence request not found")
            
            (sub_id, status, absence_date, token, start_time, end_time, main_teacher_id, subject_name, grade_name) = sub
             
            #ensure this teacher is the requester of this request
            if main_teacher_id != user_id:
                return error("You can't cancel someone else's absence request. This attempt has been noted.")
            
            if status == "cancelled":
                return error("This request has already been cancelled")
            
            # substitutions accepted can only be cancelled if the class is more than 2 days away
            class_datetime = datetime.combine(absence_date, start_time)
            if datetime.now() >= class_datetime - timedelta(days=2):
                return error("This class is less than 2 days away and can no longer be cancelled. For further help, please contact the admin.")

            # notify sub if cancelled
            if status == "confirmed":
                cursor.execute("SELECT name, email FROM users WHERE id = (SELECT substitute_teacher_id FROM substitutions WHERE id = %s)", (sub_id,))
                substitute = cursor.fetchone()
                if substitute:
                    send_cancellation_notice(
                        to_email=substitute[1],
                        teacher_name=substitute[0],
                        subject_name=subject_name,
                        grade_name=grade_name,
                        absence_date=absence_date,
                        start_time=start_time,
                        end_time=end_time
                    )

            # also notify main teacher
            cursor.execute("SELECT name, email FROM users WHERE id = %s", (user_id,))
            main_teacher = cursor.fetchone()
            send_cancellation_notice(
                to_email=main_teacher[1],
                teacher_name=main_teacher[0],
                subject_name=subject_name,
                grade_name=grade_name,
                absence_date=absence_date,
                start_time=start_time,
                end_time=end_time,
                is_main_teacher=True
            )
            
            # finally cancel it
            cursor.execute("UPDATE substitutions SET status = 'cancelled', substitute_teacher_id = NULL WHERE id = %s", (sub_id,))

            g.db.commit()

    except Exception as e:
        g.db.rollback()
        print("CANCEL ABSENCE ERROR:", e)
        return error("Failed to cancel absence request")
    
    return redirect("/my_absences")


@app.route("/teacher_subjects", methods=["GET", "POST"])
@login_required
def teacher_subjects():

    if request.method == "POST":

        user_id = session["user_id"]
        subjects = request.form.getlist("subjects")
        
        for item in subjects:
            grade_id, subject_id = item.split("-")

            try:
                with g.db.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO teacher_subjects (teacher_id, subject_id, grade_id) VALUES (%s, %s, %s)",
                        (user_id, subject_id, grade_id)
                    )
            
            except Exception as e:
                g.db.rollback()
                print("ERROR INSERTING TEACHER SUBJECTS:", e)
                return error("Failed to save teacher subjects")
            
        g.db.commit()

        return redirect("/")
    
    user_id = session["user_id"] 
    try:
        with g.db.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM teacher_subjects WHERE teacher_id = %s", (user_id,))
            if cursor.fetchone()[0] > 0:
                return redirect("/profile")
            
            cursor.execute("SELECT * FROM grade")
            grades = cursor.fetchall()
            cursor.execute("SELECT * FROM subjects")
            subjects = cursor.fetchall()
            return render_template("teacher_subjects.html", grades=grades, subjects=subjects)
        
    except Exception as e:
        print("ERROR FETCHING GRADES AND SUBJECTS:", e)
        return error("Failed to load grades and subjects")

@app.route("/admin", methods=["GET", "POST"])
@login_required
@admin_required
def admin():

    # delete or add grades
    response_grade = delete_add_grade()

    if not isinstance(response_grade, list):
        return response_grade
    
    # delete or add subjects
    response_subjects = delete_add_subjects()

    if not isinstance(response_subjects, list):
        return response_subjects

    return render_template("admin_panel.html", grades=response_grade, subjects=response_subjects)


@app.route("/admin/timetable", methods=["GET", "POST"])
@login_required
@admin_required
def admin_timetable():

    selected_grade = request.args.get("grade_id")

    if request.method == "POST":
        action = request.form.get("action")
        selected_grade = request.form.get("selected_grade") or request.args.get("grade_id")

        if action == "add":
            try:
                with g.db.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO time_slots 
                            (grade_id, day_of_week, start_time, end_time, subject_id, main_teacher_id)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (
                        request.form.get("grade_id"),
                        request.form.get("day_of_week"),
                        request.form.get("start_time"),
                        request.form.get("end_time"),
                        request.form.get("subject_id"),
                        request.form.get("teacher_id"),
                    ))
                g.db.commit()
            except Exception as e:
                g.db.rollback()
                print("ADD SLOT ERROR:", e)
                return error("Failed to add time slot")

        elif action == "delete":
            try:
                with g.db.cursor() as cursor:
                    cursor.execute("DELETE FROM time_slots WHERE id = %s", (request.form.get("slot_id"),))
                g.db.commit()
            except Exception as e:
                g.db.rollback()
                print("DELETE SLOT ERROR:", e)
                return error("Failed to delete time slot")

        return redirect("/admin/timetable" + (f"?grade_id={selected_grade}" if selected_grade else ""))

    # GET
    try:
        with g.db.cursor() as cursor:

            cursor.execute("SELECT id, name FROM grade ORDER BY name")
            grades = cursor.fetchall()

            cursor.execute("SELECT id, name FROM subjects ORDER BY name")
            subjects = cursor.fetchall()

            cursor.execute("SELECT id, name FROM users ORDER BY name")
            teachers = cursor.fetchall()

            cursor.execute("""
                           SELECT subject_id, array_agg(teacher_id)
                           FROM teacher_subjects
                           GROUP BY subject_id
                           """)
            subject_teacher_map = {row[0]: row[1] for row in cursor.fetchall()}

            if selected_grade:
                cursor.execute("""
                    SELECT ts.id, g.name, ts.day_of_week, ts.start_time,
                           ts.end_time, s.name, u.name
                    FROM time_slots ts
                    JOIN grade g ON ts.grade_id = g.id
                    JOIN subjects s ON ts.subject_id = s.id
                    JOIN users u ON ts.main_teacher_id = u.id
                    WHERE ts.grade_id = %s
                    ORDER BY ts.day_of_week, ts.start_time
                """, (selected_grade,))
            else:
                cursor.execute("""
                    SELECT ts.id, g.name, ts.day_of_week, ts.start_time,
                           ts.end_time, s.name, u.name
                    FROM time_slots ts
                    JOIN grade g ON ts.grade_id = g.id
                    JOIN subjects s ON ts.subject_id = s.id
                    JOIN users u ON ts.main_teacher_id = u.id
                    ORDER BY g.name, ts.day_of_week, ts.start_time
                """)
            slots = cursor.fetchall()

    except Exception as e:
        print("FETCH TIMETABLE ERROR:", e)
        return error("Failed to load timetable editor")

    return render_template("admin/timetable.html",
                           grades=grades,
                           subjects=subjects,
                           teachers=teachers,
                           slots=slots,
                           selected_grade=selected_grade,
                           subject_teacher_map=subject_teacher_map)


@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":
        pending_token = session.get("pending_token")
        session.clear()

        username = request.form.get("username")
        password = request.form.get("password")

        try:
            with g.db.cursor() as cursor:
                cursor.execute(
                    "SELECT id, password_hash, is_admin, name FROM users WHERE name = %s", (username,))
                user = cursor.fetchone()

            if user is None or not check_password_hash(user[1], password):
                return error("Invalid credentials")

            session["user_id"] = user[0]
            session["is_admin"] = user[2]
            session["username"] = user[3]
            if pending_token:
                return redirect(f"/accept_substitution/{pending_token}")
            return redirect("/")

        except Exception as e:
            g.db.rollback()
            print("LOGIN ERROR:", e)
            return error("Login failed")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    user_id = session["user_id"]

    if request.method == "POST":
        action = request.form.get("action")

        # change name or email
        if action == "update_info":
            name = request.form.get("name").strip()
            email = request.form.get("email").strip()

            try:
                with g.db.cursor() as cursor:
                    # ensure name or email isn't already in use in another account
                    cursor.execute("SELECT id FROM users WHERE (name = %s OR email = %s) AND id != %s", (name, email, user_id))
                    if cursor.fetchone():
                        return error("Name or email already taken by another account")

                    cursor.execute("UPDATE users SET name = %s, email = %s WHERE id = %s", (name, email, user_id))
                g.db.commit()
                flash("Profile updated successfully.")
                return redirect("/profile")
            except Exception as e:
                g.db.rollback()
                print("UPDATE INFO ERROR:", e)
                return error("Failed to update profile")

        # change password
        elif action == "change_password":
            current_password = request.form.get("current_password")
            new_password = request.form.get("new_password")
            confirmation = request.form.get("confirmation")

            if new_password != confirmation:
                return error("New passwords do not match")

            try:
                with g.db.cursor() as cursor:
                    cursor.execute("SELECT password_hash FROM users WHERE id = %s", (user_id,))
                    stored_hash = cursor.fetchone()[0]

                    if not check_password_hash(stored_hash, current_password):
                        return error("Current password is incorrect")

                    cursor.execute("UPDATE users SET password_hash = %s WHERE id = %s", (generate_password_hash(new_password), user_id))
                g.db.commit()
                flash("Password changed successfully.")
                return redirect("/profile")
            except Exception as e:
                g.db.rollback()
                print("CHANGE PASSWORD ERROR:", e)
                return error("Failed to change password")

        # update subjects
        elif action == "update_subjects":
            subjects = request.form.getlist("subjects")

            try:
                with g.db.cursor() as cursor:
                    # Wipe existing and reinsert
                    cursor.execute("DELETE FROM teacher_subjects WHERE teacher_id = %s", (user_id,))
                    for item in subjects:
                        grade_id, subject_id = item.split("-")
                        cursor.execute("INSERT INTO teacher_subjects (teacher_id, subject_id, grade_id) VALUES (%s, %s, %s)", (user_id, subject_id, grade_id))
                g.db.commit()
                flash("Subjects updated successfully.")
                return redirect("/profile")
            except Exception as e:
                g.db.rollback()
                print("UPDATE SUBJECTS ERROR:", e)
                return error("Failed to update subjects")

        # delete account
        elif action == "delete_account":
            password = request.form.get("delete_password")

            try:
                with g.db.cursor() as cursor:

                    # Block deletion if there is a pending substitution request from main teacher
                    cursor.execute("""SELECT COUNT(*) FROM substitutions sub
                                      JOIN time_slots ts ON sub.time_slot_id = ts.id
                                      WHERE ts.main_teacher_id = %s AND sub.status = 'pending'
                                   """, (user_id,))
                    if cursor.fetchone()[0] > 0:
                        return error("You have pending substitution requests and cannot delete your account. Please cancel these requests before deleting your account.")
                    
                    # Block deletion if substitute is confirmed for an upcoming class
                    cursor.execute("""
                                   SELECT COUNT(*) FROM substitutions
                                   WHERE substitute_teacher_id = %s
                                   AND status = 'confirmed'
                                   AND date >= CURRENT_DATE
                                   """, (user_id,))
                    if cursor.fetchone()[0] > 0:
                        return error("You are confirmed to cover upcoming classes. Please cancel these substitutions before deleting your account.")
                    
                    cursor.execute("SELECT password_hash FROM users WHERE id = %s", (user_id,))
                    stored_hash = cursor.fetchone()[0]

                    if not check_password_hash(stored_hash, password):
                        return error("Incorrect password — account not deleted")

                    cursor.execute("DELETE FROM substitutions WHERE time_slot_id IN (SELECT id FROM time_slots WHERE main_teacher_id = %s)", (user_id,))
                    cursor.execute("DELETE FROM time_slots WHERE main_teacher_id = %s", (user_id,))
                    cursor.execute("DELETE FROM teacher_subjects WHERE teacher_id = %s", (user_id,))
                    cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
                g.db.commit()

            except Exception as e:
                g.db.rollback()
                print("DELETE ERROR:", e)
                return error("Failed to delete account")

            session.clear()
            return redirect("/login")

        return redirect("/profile")

    # GET method
    try:
        with g.db.cursor() as cursor:
            cursor.execute("SELECT name, email FROM users WHERE id = %s", (user_id,))
            user = cursor.fetchone()

            cursor.execute("SELECT id, name FROM grade ORDER BY name")
            grades = cursor.fetchall()

            cursor.execute("SELECT id, name FROM subjects ORDER BY name")
            subjects = cursor.fetchall()

            # Get this teacher's current grades and subejcts
            cursor.execute("SELECT subject_id, grade_id FROM teacher_subjects WHERE teacher_id = %s", (user_id,))
            current_subjects = set(f"{row[1]}-{row[0]}" for row in cursor.fetchall())

            # number of classes covered
            cursor.execute("SELECT COUNT(*) FROM substitutions WHERE substitute_teacher_id = %s AND status = 'confirmed' AND date < CURRENT_DATE", (user_id,))
            classes_covered = cursor.fetchone()[0]

    except Exception as e:
        print("PROFILE ERROR:", e)
        return error("Failed to load profile")

    return render_template("profile.html", user=user, grades=grades, subjects=subjects, current_subjects=current_subjects, classes_covered=classes_covered)


@app.route("/teachers")
@login_required
def teachers():
    try:
        with g.db.cursor() as cursor:
            cursor.execute("SELECT id, name, email, is_admin FROM users ORDER BY is_admin DESC, name ASC")
            teachers = cursor.fetchall()

            cursor.execute("SELECT id, name FROM grade ORDER BY name")
            grades = cursor.fetchall()

            cursor.execute("SELECT id, name FROM subjects ORDER BY name")
            subjects = cursor.fetchall()

            # Get all teacher_subjects
            cursor.execute("SELECT ts.teacher_id, ts.subject_id, ts.grade_id FROM teacher_subjects ts")
            teacher_subjects_rows = cursor.fetchall()

            # Get all main teacher assignments from time_slots
            cursor.execute("SELECT DISTINCT main_teacher_id, subject_id, grade_id FROM time_slots")
            main_assignments = set(cursor.fetchall())

            # classes covered as a substitute
            cursor.execute("SELECT substitute_teacher_id, COUNT(*) FROM substitutions WHERE status = 'confirmed' AND date < CURRENT_DATE GROUP BY substitute_teacher_id")
            coverage_counts = dict(cursor.fetchall())

    except Exception as e:
        print("TEACHERS ERROR:", e)
        return error("Failed to load teachers")

    # Build a lookup: teacher_id -> {(subject_id, grade_id): is_main}
    teacher_map = defaultdict(dict)
    for teacher_id, subject_id, grade_id in teacher_subjects_rows:
        is_main = (teacher_id, subject_id, grade_id) in main_assignments
        teacher_map[teacher_id][(subject_id, grade_id)] = is_main

    return render_template("teachers.html", teachers=teachers, grades=grades, subjects=subjects, teacher_map=teacher_map, coverage_counts=coverage_counts)


@app.context_processor
def inject_settings():
    try:
        with g.db.cursor() as cursor:
            cursor.execute("""
                SELECT logo_filename, primary_colour, secondary_colour, accent_colour, 
                       school_name, school_start, school_end 
                FROM school_settings WHERE id = 1
            """)
            settings = cursor.fetchone()
            if settings:
                return {
                    "settings": {
                        "logo_filename": settings[0],
                        "primary_colour": settings[1],
                        "secondary_colour": settings[2],
                        "accent_colour": settings[3],
                        "school_name": settings[4] or "School Scheduler",
                        "school_start": str(settings[5]) if settings[5] else "08:00:00",
                        "school_end": str(settings[6]) if settings[6] else "15:00:00"
                    }
                }
    except Exception as e:
        print("ERROR FETCHING SETTINGS:", e)

    return {
        "settings": {
            "logo_filename": None,
            "primary_colour": "#2c3e50",
            "secondary_colour": "#3498db",
            "accent_colour": "#e67e22",
            "school_name": "School Scheduler",
            "school_start": "08:00:00",
            "school_end": "15:00:00"
        }
    }


@app.route("/admin/settings", methods=["GET", "POST"])
@login_required
@admin_required
def admin_settings():

    if request.method == "POST":
        action = request.form.get("action")

        if action == "update_name":
            school_name = request.form.get("school_name").strip()
            try:
                with g.db.cursor() as cursor:
                    cursor.execute("UPDATE school_settings SET school_name = %s WHERE id = 1", (school_name,))
                g.db.commit()
            except Exception as e:
                g.db.rollback()
                print("UPDATE NAME ERROR:", e)
                return error("Failed to update school name")

        elif action == "update_colours":
            primary = request.form.get("primary_colour")
            secondary = request.form.get("secondary_colour")
            accent = request.form.get("accent_colour")
            try:
                with g.db.cursor() as cursor:
                    cursor.execute("""
                        UPDATE school_settings 
                        SET primary_colour = %s, secondary_colour = %s, accent_colour = %s 
                        WHERE id = 1
                    """, (primary, secondary, accent))
                g.db.commit()
            except Exception as e:
                g.db.rollback()
                print("UPDATE COLOURS ERROR:", e)
                return error("Failed to update colours")

        elif action == "upload_logo":
            file = request.files.get("logo")
            if file and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                os.makedirs("static/uploads", exist_ok=True)
                file.save(os.path.join("static/uploads", filename))
                try:
                    with g.db.cursor() as cursor:
                        cursor.execute("UPDATE school_settings SET logo_filename = %s WHERE id = 1", (filename,))
                    g.db.commit()
                except Exception as e:
                    g.db.rollback()
                    print("UPLOAD LOGO ERROR:", e)
                    return error("Failed to save logo")
            else:
                return error("Invalid file type — please upload a PNG or JPG")

        elif action == "remove_logo":
            try:
                with g.db.cursor() as cursor:
                    cursor.execute("SELECT logo_filename FROM school_settings WHERE id = 1")
                    row = cursor.fetchone()
                    if row and row[0]:
                        filepath = os.path.join("static/uploads", row[0])
                        if os.path.exists(filepath):
                            os.remove(filepath)
                    cursor.execute("UPDATE school_settings SET logo_filename = NULL WHERE id = 1")
                g.db.commit()
            except Exception as e:
                g.db.rollback()
                print("REMOVE LOGO ERROR:", e)
                return error("Failed to remove logo")
            
        elif action == "update_hours":
            school_start = request.form.get("school_start")
            school_end = request.form.get("school_end")
            try:
                with g.db.cursor() as cursor:
                    cursor.execute("UPDATE school_settings SET school_start = %s, school_end = %s WHERE id = 1", (school_start, school_end))
                g.db.commit()
            except Exception as e:
                g.db.rollback()
                print("UPDATE HOURS ERROR:", e)
                return error("Failed to update school hours")

        elif action == "reset_colours":
            try:
                with g.db.cursor() as cursor:
                    cursor.execute("""
                        UPDATE school_settings 
                        SET primary_colour = '#2c3e50', secondary_colour = '#3498db', accent_colour = '#e67e22' 
                        WHERE id = 1
                    """)
                g.db.commit()
            except Exception as e:
                g.db.rollback()
                print("RESET COLOURS ERROR:", e)
                return error("Failed to reset colours")

        return redirect("/admin/settings")

    try:
        with g.db.cursor() as cursor:
            cursor.execute("""
                SELECT logo_filename, primary_colour, secondary_colour, accent_colour, school_name, school_start, school_end 
                FROM school_settings WHERE id = 1
            """)
            row = cursor.fetchone()
    except Exception as e:
        print("SETTINGS FETCH ERROR:", e)
        return error("Failed to load settings")

    return render_template("admin/settings.html", current=row)


@app.route("/admin/logs")
@login_required
@admin_required
def admin_logs():
    try:
        with g.db.cursor() as cursor:
            cursor.execute("""
                SELECT
                    sub.id,
                    sub.date,
                    sub.status,
                    sub.requested_at,
                    s.name AS subject,
                    g.name AS grade,
                    ts.start_time,
                    ts.end_time,
                    absent.name AS absent_teacher,
                    COALESCE(cover.name, '—') AS substitute
                FROM substitutions sub
                JOIN time_slots ts ON sub.time_slot_id = ts.id
                JOIN subjects s ON ts.subject_id = s.id
                JOIN grade g ON ts.grade_id = g.id
                JOIN users absent ON ts.main_teacher_id = absent.id
                LEFT JOIN users cover ON sub.substitute_teacher_id = cover.id
                ORDER BY sub.requested_at DESC
            """)
            logs = cursor.fetchall()

            # summary counts
            cursor.execute("SELECT COUNT(*) FROM substitutions WHERE status = 'pending'")
            pending_count = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM substitutions WHERE status = 'confirmed'")
            confirmed_count = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM substitutions WHERE status = 'cancelled'")
            cancelled_count = cursor.fetchone()[0]

    except Exception as e:
        print("ADMIN LOGS ERROR:", e)
        return error("Failed to load logs")

    return render_template("admin/logs.html",
                           logs=logs,
                           pending_count=pending_count,
                           confirmed_count=confirmed_count,
                           cancelled_count=cancelled_count)


@app.route("/my_substitutions")
@login_required
def my_substitutions():
    user_id = session["user_id"]

    try:
        with g.db.cursor() as cursor:
            cursor.execute("""
                SELECT
                    sub.id,
                    sub.date,
                    sub.status,
                    ts.start_time,
                    ts.end_time,
                    s.name AS subject,
                    g.name AS grade,
                    u.name AS absent_teacher
                FROM substitutions sub
                JOIN time_slots ts ON sub.time_slot_id = ts.id
                JOIN subjects s ON ts.subject_id = s.id
                JOIN grade g ON ts.grade_id = g.id
                JOIN users u ON ts.main_teacher_id = u.id
                WHERE sub.substitute_teacher_id = %s
                AND sub.status = 'confirmed'
                ORDER BY sub.date DESC
            """, (user_id,))
            substitutions = cursor.fetchall()

    except Exception as e:
        print("MY SUBSTITUTIONS ERROR:", e)
        return error("Failed to load your substitutions")

    return render_template("my_substitutions.html",
                           substitutions=substitutions,
                           today=date.today())


@app.route("/cancel_substitution/<int:sub_id>", methods=["POST"])
@login_required
def cancel_substitution(sub_id):
    user_id = session["user_id"]

    try:
        with g.db.cursor() as cursor:
            cursor.execute("""
                SELECT
                    sub.id,
                    sub.status,
                    sub.date,
                    sub.token,
                    ts.start_time,
                    ts.end_time,
                    ts.main_teacher_id,
                    s.name AS subject,
                    g.name AS grade
                FROM substitutions sub
                JOIN time_slots ts ON sub.time_slot_id = ts.id
                JOIN subjects s ON ts.subject_id = s.id
                JOIN grade g ON ts.grade_id = g.id
                WHERE sub.id = %s
            """, (sub_id,))
            sub = cursor.fetchone()

            if not sub:
                return error("Substitution not found")

            (sub_id, status, absence_date, token, start_time, end_time,
             main_teacher_id, subject_name, grade_name) = sub

            # make sure this teacher is the substitute
            cursor.execute("""
                SELECT substitute_teacher_id FROM substitutions WHERE id = %s
            """, (sub_id,))
            substitute_teacher_id = cursor.fetchone()[0]

            if substitute_teacher_id != user_id:
                return error("You can't cancel someone else's substitution")

            # enforce 2 day rule
            class_datetime = datetime.combine(absence_date, start_time)
            if datetime.now() >= class_datetime - timedelta(days=2):
                return error("This class is less than 2 days away and can no longer be cancelled. Please contact the admin.")

            # reset substitution back to pending
            cursor.execute("""
                UPDATE substitutions
                SET substitute_teacher_id = NULL, status = 'pending'
                WHERE id = %s
            """, (sub_id,))

            g.db.commit()

            # notify absent teacher their cover has cancelled
            cursor.execute("SELECT name, email FROM users WHERE id = %s", (main_teacher_id,))
            absent_teacher = cursor.fetchone()

            # get substitute name for the email
            cursor.execute("SELECT name FROM users WHERE id = %s", (user_id,))
            substitute_name = cursor.fetchone()[0]

            # re-send substitution request emails to eligible teachers
            cursor.execute("""
                SELECT u.id, u.name, u.email
                FROM teacher_subjects ts_map
                JOIN users u ON ts_map.teacher_id = u.id
                WHERE ts_map.subject_id = (
                    SELECT subject_id FROM time_slots 
                    WHERE id = (SELECT time_slot_id FROM substitutions WHERE id = %s)
                )
                AND ts_map.grade_id = (
                    SELECT grade_id FROM time_slots 
                    WHERE id = (SELECT time_slot_id FROM substitutions WHERE id = %s)
                )
                AND ts_map.teacher_id != %s
                AND ts_map.teacher_id != %s
            """, (sub_id, sub_id, main_teacher_id, user_id))
            eligible = cursor.fetchall()

    except Exception as e:
        g.db.rollback()
        print("CANCEL SUBSTITUTION ERROR:", e)
        return error("Failed to cancel substitution")

    # notify absent teacher
    send_substitution_cancelled_by_sub(
        to_email=absent_teacher[1],
        absent_teacher_name=absent_teacher[0],
        substitute_name=substitute_name,
        subject_name=subject_name,
        grade_name=grade_name,
        absence_date=absence_date,
        start_time=start_time,
        end_time=end_time
    )

    # re-send request emails to eligible teachers
    for teacher in eligible:
        send_substitution_request(
            to_email=teacher[2],
            teacher_name=teacher[1],
            subject_name=subject_name,
            grade_name=grade_name,
            absence_date=absence_date,
            start_time=start_time,
            end_time=end_time,
            token=token
        )

    return redirect("/my_substitutions")

if __name__ == "__main__":
    app.run(debug=True)