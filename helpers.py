from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from flask import g, jsonify, redirect, render_template, request, session
from functools import wraps
import os
import smtplib


UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'csv', 'png', 'jpg', 'jpeg'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get("user_id") is None:
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("is_admin"):
            return error("Access denied", 403)
        return f(*args, **kwargs)
    return decorated_function


def error(message, code=400):
    error_titles = {
        400: "Something went wrong",
        403: "Not allowed",
        404: "Page not found",
        500: "Server error"
    }
    title = error_titles.get(code, "Something went wrong")
    return render_template("error.html", title=title, message=message, code=code), code


# This function was written with the help of Claude (Anthropic)
def load_schedule(grade_id=None, teacher_id=None):

    try:
        with g.db.cursor() as cursor:
            # get regular slots 
            query = """
                SELECT 
                    ts.id,
                    ts.day_of_week,
                    ts.start_time,
                    ts.end_time,
                    s.name AS subject,
                    u.name AS teacher,
                    g.name AS grade
                FROM time_slots ts
                JOIN subjects s ON ts.subject_id = s.id
                JOIN users u ON ts.main_teacher_id = u.id
                JOIN grade g ON ts.grade_id = g.id
            """
            filters = []
            params = []

            if grade_id:
                filters.append("ts.grade_id = %s")
                params.append(grade_id)

            if teacher_id:
                filters.append("ts.main_teacher_id = %s")
                params.append(teacher_id)

            if filters:
                query += " WHERE " + " AND ".join(filters)

            cursor.execute(query, params)
            regular_slots = cursor.fetchall()

            # get confirmed substitutions for these slots so we know which dates are covered
            slot_ids = [row[0] for row in regular_slots]
            covered_dates = set()

            if slot_ids:
                cursor.execute("SELECT time_slot_id, date FROM substitutions WHERE status = 'confirmed' AND time_slot_id = ANY(%s)", (slot_ids,))
                covered_dates = set(cursor.fetchall())

            # get the classes this teacher is covering as substitute
            sub_rows = []
            if teacher_id:
                cursor.execute("""
                            SELECT
                               ts.id,
                               ts.start_time,
                               ts.end_time,
                               s.name AS subject,
                               u.name AS teacher,
                               g.name AS grade,
                               sub.date
                            FROM substitutions sub
                            JOIN time_slots ts ON sub.time_slot_id = ts.id
                            JOIN subjects s ON ts.subject_id = s.id
                            JOIN users u ON ts.main_teacher_id = u.id
                            JOIN grade g ON ts.grade_id = g.id
                            WHERE sub.substitute_teacher_id = %s
                            AND sub.status = 'confirmed'
                        """, (teacher_id,))
                sub_rows = cursor.fetchall()

    except Exception as e:
            print("LOAD SCHEDULE ERROR:", e)
            return error("Failed to load schedule")
    
    day_map = {"Monday": 1, "Tuesday": 2, "Wednesday": 3, "Thursday": 4, "Friday": 5}

    # dated events for the next 6 months
    school_year_start = date(2026, 1, 1)
    end_date = school_year_start + timedelta(weeks=52)
    events = []

    for row in regular_slots:
            slot_id, day_of_week, start_time, end_time, subject, teacher, grade = row
            target_dow = day_map[day_of_week]

            # go through every occurence of this day in the next 6 months
            current = school_year_start
            while current <= end_date:
                if current.isoweekday() == target_dow:
                    is_covered = (slot_id, current) in covered_dates

                    events.append({
                        "id": f"slot_{slot_id}_{current}",
                        "title": f"{subject} ({teacher})" if not is_covered else f"{subject} — covered",
                        "start": f"{current}T{start_time}",
                        "end": f"{current}T{end_time}",
                        "backgroundColor": "#e67e22" if is_covered else "#3788d8",
                        "borderColor": "#e67e22" if is_covered else "#3788d8",
                        "extendedProps": {
                            "teacher": teacher,
                            "grade": grade,
                            "slot_id": slot_id,
                            "is_covered": is_covered
                        }
                    })
                current += timedelta(days=1)

    # add the substitution events (green classes are the ones they're covering)
    for row in sub_rows:
            slot_id, start_time, end_time, subject, teacher, grade, sub_date = row
            events.append({
                "id": f"sub_{slot_id}_{sub_date}",
                "title": f"{subject} ({teacher}) — covering",
                "start": f"{sub_date}T{start_time}",
                "end": f"{sub_date}T{end_time}",
                "backgroundColor": "#036705",
                "borderColor": "#036705",
                "extendedProps": {
                     "teacher": teacher,
                     "grade": grade,
                     "slot_id": slot_id,
                     "is_substitution": True
                }
            })

    return jsonify(events)


def validate_and_insert_user(username, email, hashed_password):
        try:
            with g.db.cursor() as cursor:
                
                # Check if username or email already exists
                cursor.execute(
                    "SELECT id FROM users WHERE name = %s OR email = %s",
                    (username, email)
                )
                if cursor.fetchone():
                    return None

                # Insert new user into database
                cursor.execute(
                    "INSERT INTO users (name, email, password_hash) VALUES (%s, %s, %s)",
                    (username, email, hashed_password)
                )

                cursor.execute("SELECT id FROM users WHERE name = %s", (username,))
                user_id = cursor.fetchone()[0]

                g.db.commit()

                return user_id

        except Exception as e:
            g.db.rollback()
            print("REG ERROR:", e)
            return error("Registration failed")
        

def delete_add_grade():
    if request.method == "POST":

        # Delete grade (looks for grade_id in form)
        if "grade_id" in request.form:

            grade_id = request.form.get("grade_id")

            try:
                with g.db.cursor() as cursor:
                    cursor.execute("DELETE FROM grade WHERE id = %s", (grade_id,))

                    g.db.commit()

            except Exception as e:
                g.db.rollback()
                print("DELETE GRADE ERROR:", e)
                return error("Failed to delete grade")

            return redirect("/admin")


        # Add grade (looks for grade_name in form)
        if "grade_name" in request.form:

            grade_name = request.form.get("grade_name").strip()

            try:
                with g.db.cursor() as cursor:
                    cursor.execute("INSERT INTO grade (name) VALUES (%s)", (grade_name,))
                    
                    g.db.commit()

            except Exception as e:
                g.db.rollback()
                print("ADD GRADE ERROR:", e)
                return error("Failed to add grade")

            return redirect("/admin")

    # Get request
    grades = []

    try:
        with g.db.cursor() as cursor:
            cursor.execute("SELECT * FROM grade")
            grades = cursor.fetchall()
            return grades

    except Exception as e:
        print("FETCH GRADES ERROR:", e)
        return error("Failed to load grades")
    

def delete_add_subjects():
    if request.method == "POST":

        # Delete subject (looks for subject_id in form)
        if "subject_id" in request.form:

            subject_id = request.form.get("subject_id")

            try:
                with g.db.cursor() as cursor:
                    cursor.execute("DELETE FROM subjects WHERE id = %s", (subject_id,))

                    g.db.commit()

            except Exception as e:
                g.db.rollback()
                print("DELETE SUBJECT ERROR:", e)
                return error("Failed to delete subject")

            return redirect("/admin")


        # Add subject (looks for subject_name in form)
        if "subject_name" in request.form:

            subject_name = request.form.get("subject_name").strip()

            try:
                with g.db.cursor() as cursor:
                    cursor.execute("INSERT INTO subjects (name) VALUES (%s)", (subject_name,))

                    g.db.commit()

            except Exception as e:
                g.db.rollback()
                print("ADD SUBJECT ERROR:", e)
                return error("Failed to add subject")

            return redirect("/admin")

    # Get request
    subjects = []

    try:
        with g.db.cursor() as cursor:
            cursor.execute("SELECT * FROM subjects")
            subjects = cursor.fetchall()
            return subjects

    except Exception as e:
        print("FETCH SUBJECTS ERROR:", e)
        return error("Failed to load subjects")


def send_cancellation_notice(to_email, teacher_name, subject_name, grade_name, absence_date, start_time, end_time, is_main_teacher=False):

    if is_main_teacher:
        body = f"""
Hi {teacher_name},

Your absence request has been successfully cancelled:

    Subject: {subject_name}
    Grade:   {grade_name}
    Date:    {absence_date.strftime('%A, %d %B %Y')}
    Time:    {start_time.strftime('%I:%M %p')} - {end_time.strftime('%I:%M %p')}

You are expected to teach this class as normal.

Regards, 
Shelter Community School 1 Admin
"""
    else:
        body = f"""
Hi {teacher_name},

The following substitution has been cancelled:

    Subject: {subject_name}
    Grade:   {grade_name}
    Date:    {absence_date.strftime('%A, %d %B %Y')}
    Time:    {start_time.strftime('%I:%M %p')} - {end_time.strftime('%I:%M %p')}

You no longer need to cover this class. 

Regards, 
Shelter Community School 1 Admin
"""
    
            
    gmail_address = os.environ.get("GMAIL_ADDRESS")
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD")

    msg = MIMEMultipart()
    msg["From"] = gmail_address
    msg["To"] = to_email
    msg["Subject"] = f"Substitution cancelled: {subject_name} ({grade_name}) on {absence_date}"
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
            smtp.starttls()
            smtp.login(gmail_address, gmail_password)
            smtp.sendmail(gmail_address, to_email, msg.as_string())

    except Exception as e:
        print(f"CANCELLATION EMAIL ERROR TO {to_email}: {e}")


def send_substitution_request(to_email, teacher_name, subject_name, grade_name, absence_date, start_time, end_time, token):
    gmail_address = os.environ.get("GMAIL_ADDRESS")
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD")
    base_url = os.environ.get("BASE_URL", "http://localhost:5000")

    accept_link = f"{base_url}/accept_substitution/{token}"

    body = f"""
Hi {teacher_name},

A substitute teacher is needed for the following class:

    Subject: {subject_name}
    Grade:   {grade_name}
    Date:    {absence_date.strftime('%A, %d %B %Y')}
    Time:    {start_time.strftime('%I:%M %p')} - {end_time.strftime('%I:%M %p')}

Please click this link if you are able to cover this class. 

    {accept_link}

Note: The first teacher to accept will be assigned. 
If you are not available, please ignore this email. 

Regards, 
Shelter Community School 1 Admin
"""
    msg = MIMEMultipart()
    msg["From"] = gmail_address
    msg["To"] = to_email
    msg["Subject"] = f"Substitute request: {subject_name} ({grade_name}) on {absence_date}"
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
            smtp.starttls()
            smtp.login(gmail_address, gmail_password)
            smtp.sendmail(gmail_address, to_email, msg.as_string())
        print(f"Email sent to {to_email}")

    except Exception as e:
        print(f"EMAIL ERROR TO {to_email}: {e}")


def send_substitution_confirmed(to_email, recipient_name, substitute_name, subject_name, grade_name, absence_date, start_time, end_time, is_substitute=False):
    gmail_address = os.environ.get("GMAIL_ADDRESS")
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD")

    if is_substitute:
        body = f"""
Hi {recipient_name},

You have been confirmed as the substitute teacher for the following class:

    Subject: {subject_name}
    Grade:   {grade_name}
    Date:    {absence_date.strftime('%A, %d %B %Y')}
    Time:    {start_time.strftime('%I:%M %p')} - {end_time.strftime('%I:%M %p')}

See you in class!

Regards, 
Shelter Community School 1 Admin
"""
    else:
        body = f"""
Hi {recipient_name},

Your absence request has been covered! {substitute_name} will be replacing you for the following class:

    Subject: {subject_name}
    Grade:   {grade_name}
    Date:    {absence_date.strftime('%A, %d %B %Y')}
    Time:    {start_time.strftime('%I:%M %p')} - {end_time.strftime('%I:%M %p')}

Regards, 
Shelter Community School 1 Admin
"""
    
    msg = MIMEMultipart()
    msg["From"] = gmail_address
    msg["To"] = to_email
    msg["Subject"] = f"Substitution Confirmed: {subject_name} ({grade_name}) on {absence_date}"
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
            print(f"Attempting login with: {gmail_address}")
            print(f"Password length: {len(gmail_password) if gmail_password else 'None'}")
            smtp.starttls()
            smtp.login(gmail_address, gmail_password)
            smtp.sendmail(gmail_address, to_email, msg.as_string())

    except Exception as e:
        print(f"CONFIRMATION EMAIL ERROR TO {to_email}: {e}")


def send_substitution_cancelled_by_sub(to_email, absent_teacher_name, substitute_name,
                                        subject_name, grade_name, absence_date,
                                        start_time, end_time):
    gmail_address = os.environ.get("GMAIL_ADDRESS")
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD")

    body = f"""
Hi {absent_teacher_name},

Unfortunately {substitute_name} has cancelled their substitution for your class:

    Subject: {subject_name}
    Grade:   {grade_name}
    Date:    {absence_date.strftime('%A, %d %B %Y')}
    Time:    {start_time.strftime('%I:%M %p')} - {end_time.strftime('%I:%M %p')}

A new substitution request has been sent out to eligible teachers.

Regards,
School Coordination System
"""

    msg = MIMEMultipart()
    msg["From"] = gmail_address
    msg["To"] = to_email
    msg["Subject"] = f"Substitution Cancelled: {subject_name} ({grade_name}) on {absence_date}"
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
            smtp.starttls()
            smtp.login(gmail_address, gmail_password)
            smtp.sendmail(gmail_address, to_email, msg.as_string())

    except Exception as e:
        print(f"CANCELLATION BY SUB EMAIL ERROR: {e}")


def send_registration_confirmation(to_email, teacher_name, school_name):
    gmail_address = os.environ.get("GMAIL_ADDRESS")
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD")

    body = f"""
Hi {teacher_name},

Your account has been successfully created for {school_name}.

You can now log in and manage your schedule, report absences, and cover classes for your colleagues.

Regards,
{school_name}
"""

    msg = MIMEMultipart()
    msg["From"] = gmail_address
    msg["To"] = to_email
    msg["Subject"] = f"Welcome to {school_name}"
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
            smtp.starttls()
            smtp.login(gmail_address, gmail_password)
            smtp.sendmail(gmail_address, to_email, msg.as_string())

    except Exception as e:
        print(f"REGISTRATION EMAIL ERROR: {e}")