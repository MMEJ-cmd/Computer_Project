from flask import Flask, request, render_template, redirect, url_for, flash, session, send_from_directory, send_file, jsonify
from reportlab.platypus import SimpleDocTemplate, Paragraph
from reportlab.lib.styles import getSampleStyleSheet
import os
from werkzeug.utils import secure_filename
import mysql.connector
from werkzeug.security import generate_password_hash, check_password_hash
from flask import session
from datetime import datetime, date
import requests
import base64
from requests.auth import HTTPBasicAuth


app = Flask(__name__)
app.secret_key = "supersecretkey"

# --- MySQL Connection ---
db = mysql.connector.connect(
    host="localhost",
    user="root",
    password="root",
    database="Pharmaceutical_System"
)
cursor = db.cursor(dictionary=True)

UPLOAD_FOLDER = "static/uploads"
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'pdf'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# =====================================================
# =========  DARAJA M-PESA CONFIGURATION  =============
# =====================================================
# Get credentials at: https://developer.safaricom.co.ke
# 1. Create an account, then create an App
# 2. Copy Consumer Key & Consumer Secret below
# 3. For sandbox testing use ShortCode 174379 and the passkey below
# 4. Expose your local server with ngrok for the CallbackURL:
#       ngrok http 5000
#    Then set MPESA_CALLBACK_URL = "https://<your-ngrok-id>.ngrok.io/mpesa/callback"

MPESA_CONSUMER_KEY    = "2PfufNuXaEoahiSB7Z1RXukHXcqVFc8IcKaqiDRA62wzfgto"        
MPESA_CONSUMER_SECRET = "3Gbneqgj2AEQjElQyBWD4hzaUwmKLLbC7914FdbGLMFpWkS4sW0Q4tUW9vQBgKl4"
MPESA_SHORTCODE       = "174379"                   # sandbox short code
MPESA_PASSKEY         = "bfb279f9aa9bdbcf158e97dd71a467cd2e0c893059b10f78e6b72ada1ed2c919"
MPESA_CALLBACK_URL = "https://deprived-meagan-chalcographic.ngrok-free.dev/mpesa/callback"  # ← replace with ngrok URL
MPESA_BASE_URL        = "https://sandbox.safaricom.co.ke"
#MPESA_BASE_URL      = "https://api.safaricom.co.ke"   # ← uncomment for production


def get_mpesa_access_token():
    """Fetch a fresh OAuth2 token from Daraja."""
    url = f"{MPESA_BASE_URL}/oauth/v1/generate?grant_type=client_credentials"
    response = requests.get(
        url,
        auth=HTTPBasicAuth(MPESA_CONSUMER_KEY, MPESA_CONSUMER_SECRET),
        timeout=15
    )
    response.raise_for_status()
    return response.json().get("access_token")


def generate_mpesa_password():
    """Generate the base64 password + timestamp for STK Push."""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    raw = f"{MPESA_SHORTCODE}{MPESA_PASSKEY}{timestamp}"
    encoded = base64.b64encode(raw.encode()).decode("utf-8")
    return encoded, timestamp


def stk_push(phone_number, amount, account_reference, description):
    """
    Send STK Push (Lipa Na M-Pesa Online) to the customer's phone.
    phone_number must be in format 254XXXXXXXXX.
    """
    access_token = get_mpesa_access_token()
    password, timestamp = generate_mpesa_password()

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    payload = {
        "BusinessShortCode": MPESA_SHORTCODE,
        "Password":          password,
        "Timestamp":         timestamp,
        "TransactionType":   "CustomerPayBillOnline",
        "Amount":            int(amount),
        "PartyA":            phone_number,
        "PartyB":            MPESA_SHORTCODE,
        "PhoneNumber":       phone_number,
        "CallBackURL":       MPESA_CALLBACK_URL,
        "AccountReference":  account_reference[:12],   # max 12 chars
        "TransactionDesc":   description[:13]          # max 13 chars
    }

    url = f"{MPESA_BASE_URL}/mpesa/stkpush/v1/processrequest"
    response = requests.post(url, json=payload, headers=headers, timeout=30)
    return response.json()


def format_phone(phone):
    """
    Normalise Kenyan numbers to 254XXXXXXXXX.
    Accepts: 07XXXXXXXX | +254XXXXXXXXX | 254XXXXXXXXX | 7XXXXXXXX
    """
    phone = phone.strip().replace(" ", "").replace("-", "")
    if phone.startswith("+254"):
        return phone[1:]
    if phone.startswith("254"):
        return phone
    if phone.startswith("07") or phone.startswith("01"):
        return "254" + phone[1:]
    if len(phone) == 9:
        return "254" + phone
    return phone


# =====================================================
# =========  M-PESA CALLBACK  =========================
# =====================================================

@app.route('/mpesa/callback', methods=['POST'])
def mpesa_callback():
    """
    Safaricom posts payment confirmation here after the customer
    enters their PIN. We update the Payments table accordingly.
    """
    data = request.get_json(silent=True) or {}

    try:
        stk_callback = data["Body"]["stkCallback"]
        result_code  = stk_callback.get("ResultCode")
        checkout_id  = stk_callback.get("CheckoutRequestID")

        if result_code == 0:
            # SUCCESS — extract transaction details
            items = stk_callback["CallbackMetadata"]["Item"]
            meta  = {i["Name"]: i.get("Value") for i in items}

            mpesa_code  = meta.get("MpesaReceiptNumber")
            amount_paid = meta.get("Amount")
            phone_used  = str(meta.get("PhoneNumber", ""))

            cur = db.cursor()
            cur.execute("""
                UPDATE Payments
                SET status               = 'paid',
                    payment_method       = 'M-Pesa',
                    payment_date         = NOW(),
                    mpesa_code           = %s,
                    checkout_request_id  = %s
                WHERE checkout_request_id = %s
            """, (mpesa_code, checkout_id, checkout_id))
            db.commit()
            cur.close()

            print(f"[M-Pesa ✅] Receipt: {mpesa_code} | KES {amount_paid} | Phone: {phone_used}")

        else:
            result_desc = stk_callback.get("ResultDesc", "Unknown error")
            print(f"[M-Pesa ❌] Code: {result_code} | {result_desc}")

    except Exception as e:
        print(f"[M-Pesa] Callback error: {e}")

    # Always return 200 — Safaricom will retry if it doesn't get this
    return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"}), 200


# =====================================================
# =========  INITIATE PAYMENT  ========================
# =====================================================

@app.route('/pay', methods=['POST'])
def pay():
    """
    Called when the patient clicks 'Pay via M-Pesa'.
    Returns JSON so the frontend can show live feedback.
    """
    payment_id = request.form.get('payment_id', '').strip()
    raw_phone  = request.form.get('phone', '').strip()

    if not payment_id or not raw_phone:
        return jsonify({"status": "error", "message": "Phone number and payment ID are required."}), 400

    phone = format_phone(raw_phone)

    # Validate: must be 12 digits starting with 254
    if len(phone) != 12 or not phone.startswith("254"):
        return jsonify({"status": "error",
                        "message": "Invalid phone number. Use format 07XXXXXXXX."}), 400

    # Fetch payment record
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT * FROM Payments WHERE payment_id=%s", (payment_id,))
    payment = cur.fetchone()

    if not payment:
        return jsonify({"status": "error", "message": "Payment record not found."}), 404

    if payment['status'] == 'paid':
        return jsonify({"status": "error", "message": "This bill has already been paid."}), 400

    amount      = int(payment['amount'])
    account_ref = f"Bill-{payment_id}"
    description = "HealthPay"

    try:
        result = stk_push(phone, amount, account_ref, description)
        print(f"[M-Pesa] STK response: {result}")

        if result.get("ResponseCode") == "0":
            checkout_id = result.get("CheckoutRequestID")

            # Store CheckoutRequestID so the callback can match the payment
            cur.execute("""
                UPDATE Payments
                SET checkout_request_id = %s,
                    payment_method      = 'M-Pesa'
                WHERE payment_id = %s
            """, (checkout_id, payment_id))
            db.commit()
            cur.close()

            return jsonify({
                "status":               "pending",
                "message":              "STK push sent! Enter your M-Pesa PIN on your phone.",
                "checkout_request_id":  checkout_id
            })
        else:
            error_msg = (result.get("errorMessage")
                         or result.get("ResponseDescription")
                         or "STK push failed. Try again.")
            return jsonify({"status": "error", "message": error_msg})

    except Exception as e:
        print(f"[M-Pesa] Exception: {e}")
        return jsonify({"status": "error",
                        "message": "Could not reach M-Pesa. Please try again."}), 500


# =====================================================
# =========  PAYMENT STATUS POLLING  ==================
# =====================================================

@app.route('/pay/status/<int:payment_id>', methods=['GET'])
def check_payment_status(payment_id):
    """
    Frontend polls this every few seconds after an STK push
    to detect when Safaricom posts the callback.
    """
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT status, mpesa_code FROM Payments WHERE payment_id=%s", (payment_id,))
    payment = cur.fetchone()
    cur.close()

    if not payment:
        return jsonify({"status": "not_found"}), 404

    return jsonify({
        "status":     payment['status'],
        "mpesa_code": payment.get('mpesa_code')
    })


# =====================================================
# =========  ALL EXISTING ROUTES  =====================
# =====================================================

@app.route('/')
def home():
    return redirect(url_for('show_register'))


@app.route('/register', methods=['GET'])
def show_register():
    return render_template('Register.html')


@app.route('/register', methods=['POST'])
def register():
    fullname         = request.form['fullname']
    email            = request.form['email']
    password         = request.form['password']
    confirm_password = request.form['confirm_password']

    if password != confirm_password:
        flash("Passwords do not match!")
        return render_template("Register.html")

    cursor.execute("SELECT * FROM Users WHERE email=%s", (email,))
    if cursor.fetchone():
        flash("Email already registered!")
        return render_template("Register.html")

    hashed_password = generate_password_hash(password)
    cursor.execute(
        "INSERT INTO Users (full_name, email, password, role) VALUES (%s, %s, %s, %s)",
        (fullname, email, hashed_password, 'patient')
    )
    db.commit()

    user_id = cursor.lastrowid
    cursor.execute("INSERT INTO Patients (user_id) VALUES (%s)", (user_id,))
    db.commit()

    flash("Registration successful!")
    return redirect(url_for('patient_dashboard'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email    = request.form['email']
        password = request.form['password']

        cursor.execute("SELECT * FROM Users WHERE email=%s", (email,))
        user = cursor.fetchone()

        if not user or not check_password_hash(user['password'], password):
            flash("Incorrect email or password!")
            return render_template("login.html")

        session['user_id'] = user['user_id']
        session['role']    = user['role']
        role = user['role']

        if role == 'patient':
            cursor.execute("SELECT patient_id FROM Patients WHERE user_id=%s", (user['user_id'],))
            if not cursor.fetchone():
                cursor.execute("INSERT INTO Patients (user_id) VALUES (%s)", (user['user_id'],))
                db.commit()
            return redirect(url_for('patient_dashboard'))
        elif role == 'doctor':
            return redirect(url_for('doctor_dashboard'))
        elif role == 'pharmacist':
            return redirect(url_for('pharmacist_dashboard'))
        elif role == 'admin':
            return redirect(url_for('admin_dashboard'))
        else:
            flash("Invalid role assigned!")
            return render_template("login.html")

    return render_template("login.html")


@app.route('/patient_dashboard')
def patient_dashboard():
    return render_template("patient_dashboard.html")


# ====== DOCTOR DASHBOARD ======
@app.route('/doctor_dashboard')
def doctor_dashboard():
    doctor_user_id = session.get('user_id')

    cursor = db.cursor(dictionary=True)

    # Get doctor record
    cursor.execute("SELECT doctor_id, specialization FROM Doctors WHERE user_id=%s", (doctor_user_id,))
    doctor = cursor.fetchone()

    total_appointments = 0
    total_consultations = 0
    total_prescriptions = 0
    total_records = 0

    if doctor:
        specialty    = doctor['specialization']
        doctor_id    = doctor['doctor_id']

        # Count appointments for this specialty
        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM Appointments WHERE specialty=%s",
            (specialty,)
        )
        total_appointments = cursor.fetchone()['cnt']

        # Count prescriptions issued by this doctor
        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM DoctorPrescriptions WHERE doctor_id=%s",
            (doctor_id,)
        )
        total_prescriptions = cursor.fetchone()['cnt']

        # Count medical records linked to patients with appointments for this specialty
        cursor.execute("""
            SELECT COUNT(DISTINCT m.record_id) AS cnt
            FROM MedicalRecords m
            JOIN Patients p ON m.patient_id = p.patient_id
            JOIN Appointments a ON a.patient_id = p.patient_id
            WHERE a.specialty = %s
        """, (specialty,))
        total_records = cursor.fetchone()['cnt']

        # Count consultations (same as medical records — one per consultation saved)
        total_consultations = total_records

    return render_template(
        "Doctor_Dashboard.html",
        total_appointments=total_appointments,
        total_consultations=total_consultations,
        total_prescriptions=total_prescriptions,
        total_records=total_records
    )


#=========== PHARMACIST DASHBOARD ===========#
@app.route('/pharmacist/dashboard')
def pharmacist_dashboard():
    cursor = db.cursor()

    # ── Total: patient uploads + doctor prescriptions combined ──
    cursor.execute("""
        SELECT 
            (SELECT COUNT(*) FROM PatientPrescriptions) +
            (SELECT COUNT(*) FROM DoctorPrescriptions)
        AS total
    """)
    total = cursor.fetchone()[0]

    # ── Pending from both tables ──
    cursor.execute("""
        SELECT 
            (SELECT COUNT(*) FROM PatientPrescriptions WHERE status='pending') +
            (SELECT COUNT(*) FROM DoctorPrescriptions   WHERE status='pending')
        AS pending
    """)
    pending = cursor.fetchone()[0]

    # ── Ready from both tables ──
    cursor.execute("""
        SELECT 
            (SELECT COUNT(*) FROM PatientPrescriptions WHERE status='ready') +
            (SELECT COUNT(*) FROM DoctorPrescriptions   WHERE status='ready')
        AS ready
    """)
    ready = cursor.fetchone()[0]

    # ── Completed from both tables ──
    cursor.execute("""
        SELECT 
            (SELECT COUNT(*) FROM PatientPrescriptions WHERE status='completed') +
            (SELECT COUNT(*) FROM DoctorPrescriptions   WHERE status='completed')
        AS completed
    """)
    completed = cursor.fetchone()[0]

    cursor.close()

    return render_template(
        "pharmacist_dashboard.html",
        total_prescriptions=total,
        pending_count=pending,
        ready_count=ready,
        completed_count=completed
    )

@app.route('/admin_dashboard')
def admin_dashboard():
    return render_template("admin_dashboard.html")


@app.route('/Uploaded_Doctor')
def uploaded_doctor():
    cur = db.cursor(dictionary=True)
    cur.execute("""
        SELECT 
            dp.prescription_id,
            u_patient.full_name AS patient_name,
            u_doctor.full_name  AS doctor_name,
            d.specialization    AS specialty,
            dp.medication,
            dp.dosage_instructions,
            IFNULL(dp.additional_notes, 'None') AS additional_notes,
            dp.status,
            dp.date_issued
        FROM DoctorPrescriptions dp
        JOIN Patients p        ON dp.patient_id = p.patient_id
        JOIN Users u_patient   ON p.user_id = u_patient.user_id
        JOIN Doctors d         ON dp.doctor_id = d.doctor_id
        JOIN Users u_doctor    ON d.user_id = u_doctor.user_id
        ORDER BY dp.date_issued DESC
    """)
    prescriptions = cur.fetchall()
    return render_template("Uploaded_Doctor.html", prescriptions=prescriptions)


@app.route('/pharmacy/update_doctor_status', methods=['POST'])
def pharmacist_doctor_update_status():
    prescription_id = request.form.get('prescription_id')
    new_status = request.form.get('status')

    if not prescription_id or not new_status:
        return {"success": False, "message": "Missing data"}, 400

    cursor.execute("""
        UPDATE DoctorPrescriptions
        SET status=%s
        WHERE prescription_id=%s
    """, (new_status, prescription_id))
    db.commit()

    return {"success": True, "message": "Status updated successfully"}


@app.route('/patient/appointments')
def appointments():
    return render_template("Appointment.html")


@app.route('/patient/review_request')
def review_request():
    if 'user_id' not in session:
        flash("You must log in first.")
        return redirect(url_for('login'))

    patient_user_id = session['user_id']
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT patient_id FROM Patients WHERE user_id=%s", (patient_user_id,))
    patient = cur.fetchone()
    if not patient:
        flash("Patient record not found.")
        return redirect(url_for('patient_dashboard'))

    patient_id = patient['patient_id']

    cur.execute("""
        SELECT 'Appointment' AS type, specialty AS description, status, appointment_date AS date
        FROM Appointments WHERE patient_id=%s
    """, (patient_id,))
    appts = cur.fetchall()
    for a in appts:
        if isinstance(a['date'], date) and not isinstance(a['date'], datetime):
            a['date'] = datetime.combine(a['date'], datetime.min.time())

    cur.execute("""
        SELECT 'Prescription' AS type, file_name AS description, status, upload_date AS date
        FROM PatientPrescriptions WHERE patient_id=%s
    """, (patient_id,))
    prescriptions = cur.fetchall()

    all_requests = appts + prescriptions
    all_requests.sort(key=lambda x: x['date'], reverse=True)
    return render_template('review_request.html', requests=all_requests)


#=========== Admin Appointments page==============
@app.route('/admin/appointments')
def admin_appointments():
    query = """
    SELECT 
        a.appointment_id,
        u_patient.full_name AS patient_name,
        u_doctor.full_name AS doctor_name,
        a.specialty AS appointment_type,
        a.appointment_date,
        a.appointment_time,
        a.status
    FROM Appointments a
    JOIN Patients p ON a.patient_id = p.patient_id
    JOIN Users u_patient ON p.user_id = u_patient.user_id
    LEFT JOIN Doctors d ON a.doctor_id = d.doctor_id
    LEFT JOIN Users u_doctor ON d.user_id = u_doctor.user_id
    ORDER BY a.appointment_date DESC, a.appointment_time DESC
    """
    cursor.execute(query)
    appointments = cursor.fetchall()
    return render_template("Admin_Appointments.html", appointments=appointments)

@app.route('/admin/users')
def admin_users():
    cursor.execute("SELECT * FROM Users ORDER BY created_at DESC")
    users = cursor.fetchall()
    return render_template("User_management.html", users=users)


@app.route('/admin/create_user', methods=['GET', 'POST'])
def admin_create_user():
    if request.method == 'POST':
        full_name = request.form['fullName']
        email     = request.form['email']
        password  = request.form['password']
        role      = request.form['role']
        specialty = request.form.get('specialty', 'None')

        cursor.execute("SELECT * FROM Users WHERE email=%s", (email,))
        if cursor.fetchone():
            flash("Email already registered!")
            return render_template("User_creation.html")

        hashed_password = generate_password_hash(password)
        cursor.execute(
            "INSERT INTO Users (full_name, email, password, role) VALUES (%s, %s, %s, %s)",
            (full_name, email, hashed_password, role)
        )
        db.commit()

        if role.lower() == "doctor":
            cursor.execute("SELECT user_id FROM Users WHERE email=%s", (email,))
            row = cursor.fetchone()
            if row:
                cursor.execute("INSERT INTO Doctors (user_id, specialization) VALUES (%s, %s)",
                               (row['user_id'], specialty))
                db.commit()

        flash(f"User {full_name} ({role}) created successfully!")
        return redirect(url_for('admin_create_user'))

    return render_template("User_creation.html")


@app.route('/admin/delete_user/<int:user_id>', methods=['POST'])
def delete_user(user_id):
    cursor.execute("DELETE FROM Users WHERE user_id=%s", (user_id,))
    db.commit()
    return "success", 200


@app.route('/patient/book_appointment', methods=['POST'])
def book_appointment():
    patient_user_id = session.get('user_id')
    if not patient_user_id:
        flash("You must be logged in.")
        return redirect(url_for('appointments'))

    cursor.execute("SELECT patient_id FROM Patients WHERE user_id=%s", (patient_user_id,))
    patient = cursor.fetchone()
    if not patient:
        flash("Patient record not found!")
        return redirect(url_for('appointments'))

    specialty        = request.form.get('specialty')
    appointment_date = request.form['appointment_date']
    appointment_time = request.form['appointment_time']

    if not specialty:
        flash("Please select a specialty!")
        return redirect(url_for('appointments'))

    cursor.execute("""
        INSERT INTO Appointments (patient_id, doctor_id, appointment_date, appointment_time, specialty, status)
        VALUES (%s, %s, %s, %s, %s, 'pending')
    """, (patient['patient_id'], None, appointment_date, appointment_time, specialty))
    db.commit()
    flash("Appointment submitted successfully!")
    return redirect(url_for('appointments'))


@app.route('/doctor/appointments')
def doctor_appointments():
    doctor_user_id = session.get('user_id')
    cursor.execute("SELECT doctor_id, specialization FROM Doctors WHERE user_id=%s", (doctor_user_id,))
    doctor = cursor.fetchone()
    if not doctor:
        flash("Doctor record not found!")
        return redirect(url_for('doctor_dashboard'))

    cursor.execute("""
        SELECT 
            a.appointment_id,
            u.full_name    AS patient_name,
            a.specialty    AS appointment_type,
            a.appointment_date,
            a.appointment_time,
            a.status
        FROM Appointments a
        JOIN Patients p ON a.patient_id = p.patient_id
        JOIN Users u    ON p.user_id = u.user_id
        WHERE a.specialty=%s
        ORDER BY a.appointment_date DESC, a.appointment_time DESC
    """, (doctor['specialization'],))
    appointments = cursor.fetchall()
    return render_template("Doctor_Appointment.html", appointments=appointments, specialty=doctor['specialization'])


@app.route('/doctor/update_appointment_status', methods=['POST'])
def doctor_update_appointment_status():
    appointment_id = request.form.get('appointment_id')
    new_status     = request.form.get('status')
    if not appointment_id or not new_status:
        return 'error', 400
    try:
        cursor.execute("UPDATE Appointments SET status=%s WHERE appointment_id=%s",
                       (new_status, int(appointment_id)))
        db.commit()
        return 'success'
    except Exception as e:
        print("Error:", e)
        return 'error', 500


@app.route('/doctor/consultation', methods=['GET', 'POST'])
def doctor_consultation():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    cur = db.cursor(dictionary=True)
    doctor_user_id = session['user_id']

    cur.execute("SELECT specialization FROM Doctors WHERE user_id=%s", (doctor_user_id,))
    doctor = cur.fetchone()

    cur.execute("""
        SELECT DISTINCT p.patient_id, u.full_name
        FROM Appointments a
        JOIN Patients p ON a.patient_id = p.patient_id
        JOIN Users u    ON p.user_id = u.user_id
        WHERE a.specialty = %s AND a.status = 'pending'
    """, (doctor['specialization'],))
    patients = cur.fetchall()

    if request.method == 'POST':
        cur.execute("""
            INSERT INTO MedicalRecords (patient_id, symptoms, diagnosis)
            VALUES (%s, %s, %s)
        """, (request.form['patient_id'], request.form['symptoms'], request.form['diagnosis']))
        db.commit()
        flash("Consultation saved successfully")
        return redirect(url_for('doctor_consultation'))

    return render_template("Consultation.html", patients=patients)


@app.route('/doctor/prescription', methods=['GET', 'POST'])
def doctor_prescription():
    doctor_user_id = session.get('user_id')
    if not doctor_user_id:
        flash("You must log in first.")
        return redirect(url_for('login'))

    cur = db.cursor(dictionary=True)
    cur.execute("SELECT specialization, doctor_id FROM Doctors WHERE user_id=%s", (doctor_user_id,))
    doctor = cur.fetchone()
    if not doctor:
        flash("Doctor record not found.")
        return redirect(url_for('doctor_dashboard'))

    cur.execute("""
        SELECT u.user_id, u.full_name
        FROM Users u
        JOIN Patients p    ON u.user_id = p.user_id
        JOIN Appointments a ON p.patient_id = a.patient_id
        WHERE a.specialty = %s
        GROUP BY u.user_id
    """, (doctor['specialization'],))
    patients = cur.fetchall()

    if request.method == 'POST':
        patient_user_id = request.form.get('patient')
        medication      = request.form.get('medication')
        dosage          = request.form.get('dosage')
        notes           = request.form.get('notes')

        if not patient_user_id or not medication or not dosage:
            flash("Please fill all required fields.")
            return redirect(url_for('doctor_prescription'))

        cur.execute("SELECT patient_id FROM Patients WHERE user_id=%s", (patient_user_id,))
        patient = cur.fetchone()
        if not patient:
            flash("Patient record not found.")
            return redirect(url_for('doctor_prescription'))

        cur.execute("""
            INSERT INTO DoctorPrescriptions (patient_id, doctor_id, medication, dosage_instructions, additional_notes)
            VALUES (%s, %s, %s, %s, %s)
        """, (patient['patient_id'], doctor['doctor_id'], medication, dosage, notes))
        db.commit()

        cur.execute("""
            INSERT INTO Payments (patient_id, source, amount, status)
            VALUES (%s, %s, %s, 'pending')
        """, (patient['patient_id'], "doctor_prescription", calculate_price("doctor_prescription")))
        db.commit()

        flash("Prescription issued! Billing generated.")
        return redirect(url_for('doctor_prescription'))

    return render_template("Doctor_Prescription.html", patients=patients)


#================== Doctor Medical Records page
@app.route('/doctor/medical_records')
def doctor_medical_records():
    doctor_user_id = session.get('user_id')
    if not doctor_user_id:
        flash("You must log in first.")
        return redirect(url_for('login'))

    cursor = db.cursor(dictionary=True)

    # Get doctor's specialization
    cursor.execute("SELECT specialization FROM Doctors WHERE user_id=%s", (doctor_user_id,))
    doctor = cursor.fetchone()
    if not doctor:
        flash("Doctor record not found.")
        return redirect(url_for('doctor_dashboard'))

    doctor_specialization = doctor['specialization']

    # Fetch medical records for patients who have an appointment
    # with this doctor's specialty.
    # FIX: Using a subquery instead of a JOIN on Appointments so that
    # a patient with multiple appointments does not cause their record
    # to appear multiple times (one duplicate per extra appointment row).
    query = """
        SELECT
            u.full_name AS patient_name,
            COALESCE(m.diagnosis, 'None')   AS diagnosis,
            COALESCE(m.symptoms,  'None')   AS symptoms
        FROM MedicalRecords m
        JOIN Patients p ON m.patient_id = p.patient_id
        JOIN Users    u ON p.user_id    = u.user_id
        WHERE p.patient_id IN (
            SELECT DISTINCT patient_id
            FROM Appointments
            WHERE specialty = %s
        )
        ORDER BY m.record_date DESC
    """
    cursor.execute(query, (doctor_specialization,))
    records = cursor.fetchall()

    return render_template("Medical_Records.html", records=records)

@app.route('/patient/prescription_upload', methods=['GET', 'POST'])
def prescription_upload():
    patient_user_id = session.get('user_id')
    if not patient_user_id:
        flash("You must be logged in.")
        return redirect(url_for('login'))

    cursor.execute("SELECT patient_id FROM Patients WHERE user_id=%s", (patient_user_id,))
    patient = cursor.fetchone()
    if not patient:
        flash("Patient record not found!")
        return redirect(url_for('patient_dashboard'))

    if request.method == 'POST':
        file      = request.files.get('prescription')
        allergies = request.form.get('allergies', '')

        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))

            cursor.execute("""
                INSERT INTO PatientPrescriptions (patient_id, file_name, allergies, status)
                VALUES (%s, %s, %s, 'pending')
            """, (patient['patient_id'], filename, allergies))
            db.commit()

            cursor.execute("""
                INSERT INTO Payments (patient_id, source, amount, status)
                VALUES (%s, %s, %s, 'pending')
            """, (patient['patient_id'], "uploaded_prescription", calculate_price("uploaded_prescription")))
            db.commit()

            flash("Prescription uploaded! Go to Billing to complete payment.")
            return redirect(url_for('prescription_upload'))
        else:
            flash("Invalid file type!")
            return redirect(url_for('prescription_upload'))

    return render_template('prescription_upload.html')


@app.route('/Upload_Prescription')
def upload_prescription():
    cursor.execute("""
        SELECT pp.upload_id, u.full_name AS patient_name, pp.file_name, pp.status, pp.upload_date
        FROM PatientPrescriptions pp
        JOIN Patients p ON pp.patient_id = p.patient_id
        JOIN Users u    ON p.user_id = u.user_id
        ORDER BY pp.upload_date DESC
    """)
    prescriptions = cursor.fetchall()
    return render_template('Upload_Prescription.html', prescriptions=prescriptions)


@app.route('/download/<filename>')
def download_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename, as_attachment=True)


@app.route('/pharmacy/update_patient_status', methods=['POST'])
def update_prescription_status():
    upload_id = request.form.get('upload_id')
    new_status = request.form.get('status')

    if not upload_id or not new_status:
        return 'error', 400

    try:
        upload_id = int(upload_id)
    except ValueError:
        return 'error', 400

    try:
        cursor.execute(
            "UPDATE PatientPrescriptions SET status=%s WHERE upload_id=%s",
            (new_status, upload_id)
        )
        db.commit()
    except Exception as e:
        print("Database error:", e)
        return 'error', 500

    return 'success'


@app.route('/patient/billing')
def patient_billing():
    user_id = session.get('user_id')
    cursor.execute("SELECT patient_id FROM Patients WHERE user_id=%s", (user_id,))
    patient = cursor.fetchone()
    cursor.execute("SELECT * FROM Payments WHERE patient_id=%s", (patient['patient_id'],))
    payments = cursor.fetchall()
    return render_template("Patient_Billing.html", payments=payments)


def calculate_price(source):
    if source == "doctor_prescription":
        return 1
    elif source == "uploaded_prescription":
        return 1
    return 1


@app.route('/receipt/<int:payment_id>')
def generate_receipt(payment_id):
    cursor.execute("""
        SELECT p.*, u.full_name
        FROM Payments p
        JOIN Patients pt ON p.patient_id = pt.patient_id
        JOIN Users u     ON pt.user_id = u.user_id
        WHERE p.payment_id=%s
    """, (payment_id,))
    payment = cursor.fetchone()
    if not payment:
        return "Payment not found", 404

    file_path = f"receipt_{payment_id}.pdf"
    doc = SimpleDocTemplate(file_path)
    styles = getSampleStyleSheet()
    content = [
        Paragraph(f"Receipt ID: {payment_id}", styles['Normal']),
        Paragraph(f"Patient: {payment['full_name']}", styles['Normal']),
        Paragraph(f"Amount: KES {payment['amount']}", styles['Normal']),
        Paragraph(f"Status: {payment['status']}", styles['Normal']),
    ]
    if payment.get('mpesa_code'):
        content.append(Paragraph(f"M-Pesa Receipt: {payment['mpesa_code']}", styles['Normal']))

    doc.build(content)
    return send_file(file_path, as_attachment=True)


@app.route('/admin/billing')
def admin_billing():
    cursor.execute("""
        SELECT p.*, u.full_name
        FROM Payments p
        JOIN Patients pt ON p.patient_id = pt.patient_id
        JOIN Users u     ON pt.user_id = u.user_id
    """)
    payments = cursor.fetchall()
    return render_template("Admin_Billing.html", payments=payments)


@app.route('/admin/appointments/report')
def admin_appointments_report():
    from reportlab.lib.pagesizes import landscape, A4
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    import io

    # Fetch all appointments
    cur = db.cursor(dictionary=True)
    cur.execute("""
        SELECT
            a.appointment_id,
            u_patient.full_name AS patient_name,
            COALESCE(u_doctor.full_name, 'Not Assigned') AS doctor_name,
            a.specialty         AS appointment_type,
            a.appointment_date,
            a.appointment_time,
            a.status
        FROM Appointments a
        JOIN Patients p       ON a.patient_id = p.patient_id
        JOIN Users u_patient  ON p.user_id    = u_patient.user_id
        LEFT JOIN Doctors d   ON a.doctor_id  = d.doctor_id
        LEFT JOIN Users u_doctor ON d.user_id = u_doctor.user_id
        ORDER BY a.appointment_date DESC, a.appointment_time DESC
    """)
    appointments = cur.fetchall()
    cur.close()

    # Summary counts
    total     = len(appointments)
    pending   = sum(1 for a in appointments if a['status'] == 'pending')
    completed = sum(1 for a in appointments if a['status'] == 'completed')

    # Build PDF in memory
    buffer = io.BytesIO()
    doc    = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=1.5*cm, leftMargin=1.5*cm,
        topMargin=1.5*cm,   bottomMargin=1.5*cm
    )

    styles  = getSampleStyleSheet()
    story   = []

    # ── Title ──
    title_style = ParagraphStyle(
        'ReportTitle',
        parent=styles['Title'],
        fontSize=18,
        textColor=colors.HexColor('#1f2d3d'),
        spaceAfter=6
    )
    story.append(Paragraph("Appointments Report", title_style))
    story.append(Paragraph(
        f"Generated: {datetime.now().strftime('%d %B %Y, %H:%M')}",
        styles['Normal']
    ))
    story.append(Spacer(1, 0.4*cm))

    # ── Summary row ──
    summary_data = [
        ['Total Appointments', 'Pending', 'Completed'],
        [str(total), str(pending), str(completed)]
    ]
    summary_table = Table(summary_data, colWidths=[8*cm, 8*cm, 8*cm])
    summary_table.setStyle(TableStyle([
        ('BACKGROUND',  (0,0), (-1,0), colors.HexColor('#1f2d3d')),
        ('TEXTCOLOR',   (0,0), (-1,0), colors.white),
        ('FONTNAME',    (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE',    (0,0), (-1,-1), 11),
        ('ALIGN',       (0,0), (-1,-1), 'CENTER'),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.HexColor('#eef5ff'), colors.white]),
        ('BOX',         (0,0), (-1,-1), 0.5, colors.HexColor('#cccccc')),
        ('GRID',        (0,0), (-1,-1), 0.5, colors.HexColor('#cccccc')),
        ('TOPPADDING',  (0,0), (-1,-1), 8),
        ('BOTTOMPADDING',(0,0),(-1,-1), 8),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 0.5*cm))

    # ── Main table ──
    headers = ['#', 'Patient Name', 'Doctor', 'Specialty', 'Date', 'Time', 'Status']
    rows    = [headers]
    for i, appt in enumerate(appointments, 1):
        date_str = appt['appointment_date'].strftime('%d %b %Y') \
                   if hasattr(appt['appointment_date'], 'strftime') \
                   else str(appt['appointment_date'])
        rows.append([
            str(i),
            appt['patient_name'],
            appt['doctor_name'],
            appt['appointment_type'] or '—',
            date_str,
            str(appt['appointment_time']),
            appt['status'].capitalize()
        ])

    col_widths = [1*cm, 6*cm, 5.5*cm, 5*cm, 3.5*cm, 3*cm, 3*cm]
    main_table = Table(rows, colWidths=col_widths, repeatRows=1)
    main_table.setStyle(TableStyle([
        ('BACKGROUND',    (0,0), (-1,0), colors.HexColor('#1f2d3d')),
        ('TEXTCOLOR',     (0,0), (-1,0), colors.white),
        ('FONTNAME',      (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE',      (0,0), (-1,-1), 9),
        ('ALIGN',         (0,0), (-1,-1), 'LEFT'),
        ('ALIGN',         (0,0), (0,-1), 'CENTER'),
        ('ROWBACKGROUNDS',(0,1), (-1,-1), [colors.white, colors.HexColor('#f4f6f9')]),
        ('BOX',           (0,0), (-1,-1), 0.5, colors.HexColor('#cccccc')),
        ('GRID',          (0,0), (-1,-1), 0.5, colors.HexColor('#e0e0e0')),
        ('TOPPADDING',    (0,0), (-1,-1), 7),
        ('BOTTOMPADDING', (0,0), (-1,-1), 7),
        ('LEFTPADDING',   (0,0), (-1,-1), 8),
    ]))
    story.append(main_table)

    doc.build(story)
    buffer.seek(0)

    filename = f"appointments_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    return send_file(
        buffer,
        as_attachment=True,
        download_name=filename,
        mimetype='application/pdf'
    )


@app.route('/admin/billing/report')
def admin_billing_report():
    from reportlab.lib.pagesizes import landscape, A4
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    import io

    # Fetch all payments
    cur = db.cursor(dictionary=True)
    cur.execute("""
        SELECT p.*, u.full_name
        FROM Payments p
        JOIN Patients pt ON p.patient_id = pt.patient_id
        JOIN Users u     ON pt.user_id   = u.user_id
        ORDER BY p.payment_id DESC
    """)
    payments = cur.fetchall()
    cur.close()

    # Summary
    total    = len(payments)
    paid     = sum(1 for p in payments if p['status'] == 'paid')
    pending  = total - paid
    revenue  = sum(float(p['amount']) for p in payments if p['status'] == 'paid')

    # Build PDF in memory
    buffer = io.BytesIO()
    doc    = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=1.5*cm, leftMargin=1.5*cm,
        topMargin=1.5*cm,   bottomMargin=1.5*cm
    )

    styles = getSampleStyleSheet()
    story  = []

    # ── Title ──
    title_style = ParagraphStyle(
        'ReportTitle',
        parent=styles['Title'],
        fontSize=18,
        textColor=colors.HexColor('#1f2d3d'),
        spaceAfter=6
    )
    story.append(Paragraph("Billing & Payments Report", title_style))
    story.append(Paragraph(
        f"Generated: {datetime.now().strftime('%d %B %Y, %H:%M')}",
        styles['Normal']
    ))
    story.append(Spacer(1, 0.4*cm))

    # ── Summary row ──
    summary_data = [
        ['Total Payments', 'Paid', 'Pending', 'Total Revenue (KES)'],
        [str(total), str(paid), str(pending), f"{revenue:,.2f}"]
    ]
    summary_table = Table(summary_data, colWidths=[6*cm, 6*cm, 6*cm, 6*cm])
    summary_table.setStyle(TableStyle([
        ('BACKGROUND',  (0,0), (-1,0), colors.HexColor('#1f2d3d')),
        ('TEXTCOLOR',   (0,0), (-1,0), colors.white),
        ('FONTNAME',    (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE',    (0,0), (-1,-1), 11),
        ('ALIGN',       (0,0), (-1,-1), 'CENTER'),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.HexColor('#eef5ff'), colors.white]),
        ('BOX',         (0,0), (-1,-1), 0.5, colors.HexColor('#cccccc')),
        ('GRID',        (0,0), (-1,-1), 0.5, colors.HexColor('#cccccc')),
        ('TOPPADDING',  (0,0), (-1,-1), 8),
        ('BOTTOMPADDING',(0,0),(-1,-1), 8),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 0.5*cm))

    # ── Main table ──
    headers = ['#', 'Patient Name', 'Service Type', 'Amount (KES)', 'Status', 'M-Pesa Code', 'Date']
    rows    = [headers]
    for i, p in enumerate(payments, 1):
        date_str = p['payment_date'].strftime('%d %b %Y') \
                   if p.get('payment_date') and hasattr(p['payment_date'], 'strftime') \
                   else 'N/A'
        source = (p.get('source') or '').replace('_', ' ').title()
        rows.append([
            str(i),
            p['full_name'],
            source,
            f"KES {float(p['amount']):,.2f}",
            p['status'].capitalize(),
            p.get('mpesa_code') or '—',
            date_str
        ])

    col_widths = [1*cm, 6*cm, 5*cm, 4*cm, 3*cm, 4.5*cm, 3.5*cm]
    main_table = Table(rows, colWidths=col_widths, repeatRows=1)

    def status_color(val):
        return colors.HexColor('#155724') if val == 'Paid' else colors.HexColor('#856404')

    main_table.setStyle(TableStyle([
        ('BACKGROUND',    (0,0), (-1,0), colors.HexColor('#1f2d3d')),
        ('TEXTCOLOR',     (0,0), (-1,0), colors.white),
        ('FONTNAME',      (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE',      (0,0), (-1,-1), 9),
        ('ALIGN',         (0,0), (-1,-1), 'LEFT'),
        ('ALIGN',         (0,0), (0,-1), 'CENTER'),
        ('ROWBACKGROUNDS',(0,1), (-1,-1), [colors.white, colors.HexColor('#f4f6f9')]),
        ('BOX',           (0,0), (-1,-1), 0.5, colors.HexColor('#cccccc')),
        ('GRID',          (0,0), (-1,-1), 0.5, colors.HexColor('#e0e0e0')),
        ('TOPPADDING',    (0,0), (-1,-1), 7),
        ('BOTTOMPADDING', (0,0), (-1,-1), 7),
        ('LEFTPADDING',   (0,0), (-1,-1), 8),
    ]))
    story.append(main_table)

    doc.build(story)
    buffer.seek(0)

    filename = f"billing_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    return send_file(
        buffer,
        as_attachment=True,
        download_name=filename,
        mimetype='application/pdf'
    )


@app.route('/logout')
def logout():
    session.clear()
    flash("You have been logged out.")
    return redirect(url_for('login'))


if __name__ == "__main__":
    app.run(debug=True)