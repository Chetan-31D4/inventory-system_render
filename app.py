import os
import time
import pandas as pd
from flask     import Flask, render_template, request, redirect, url_for, session, flash, send_file, send_from_directory
from io        import BytesIO
from datetime  import datetime
from zoneinfo  import ZoneInfo
from datetime import datetime
import psycopg2
import psycopg2.extras
from flask_mail import Mail, Message
import os
from werkzeug.utils import secure_filename
from flask import Flask, render_template, session, redirect, url_for
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv  
from werkzeug.utils import secure_filename
from flask import request, session, redirect, url_for
from datetime import date, timedelta
import os
from flask import redirect, abort
from boto3 import client
from botocore.exceptions import ClientError

ALLOWED_INVOICE_EXT = {'pdf'}

load_dotenv()
import boto3

# === Cloudflare R2 client ===
R2_KEY    = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET = os.getenv("R2_SECRET_ACCESS_KEY")
R2_ACC    = os.getenv("R2_ACCOUNT_ID")
R2_BUCKET = os.getenv("R2_BUCKET")

r2 = boto3.client(
    "s3",
    endpoint_url=f"https://{R2_ACC}.r2.cloudflarestorage.com",
    aws_access_key_id=R2_KEY,
    aws_secret_access_key=R2_SECRET,
    region_name="auto",
)

app = Flask(__name__)
app.secret_key = os.urandom(24)

app.config['MAIL_SERVER']        = 'smtp.gmail.com'
app.config['MAIL_PORT']          = 587
app.config['MAIL_USE_TLS']       = True
app.config['MAIL_USERNAME']      = 'chetansinghal.fin@gmail.com'
app.config['MAIL_PASSWORD']      = 'ogjz xgug kbwy sfry'
app.config['MAIL_DEFAULT_SENDER']= ('Inventory System', 'no-reply@mydomain.com')

mail = Mail(app)

# PostgreSQL connection parameters (adjust as needed or via environment variables)
# DB_USER = os.environ.get('PG_USER', 'inv_user')
# DB_PASS = os.environ.get('PG_PASS', 'inv_pass123')
# DB_HOST = os.environ.get('PG_HOST', 'localhost')
# DB_PORT = os.environ.get('PG_PORT', '5432')
# DB_NAME = os.environ.get('PG_DB',   'inventorydb')

# DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

DATABASE_URL = os.environ['DATABASE_URL']

UPLOAD_FOLDER = os.path.join(os.getcwd(), 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
ALLOWED_EXT = {'png','jpg','jpeg','pdf','docx'}

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER


BUCKET = os.getenv('R2_BUCKET')

@app.route('/download/<path:key>')
def download_r2_object(key):
    """Redirect to a pre-signed R2 URL for the given object key."""
    try:
        url = r2.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": BUCKET, "Key": key},
            ExpiresIn=300,  # URL valid for 5 minutes
        )
    except ClientError as e:
        # e.response['Error']['Code'] == 'NoSuchKey', etc.
        return abort(404)
    return redirect(url)

def get_db():
    """
    Opens a new connection to PostgreSQL (using DATABASE_URL) and returns it.
    The cursor_factory is set to RealDictCursor, so row fetches return dict-like objects.
    """
    conn = psycopg2.connect(DATABASE_URL)
    # Ensure that subsequent .cursor() calls return RealDictCursor by default:
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn


# ────────────────────────────────────────────────────────────────────
# 2.b) get_db_cursor(): convenience function to get (conn, cur) at once
#                      with RealDictCursor
# ────────────────────────────────────────────────────────────────────
def get_db_cursor():
    """
    Returns a tuple (conn, cur) where:
      - conn is a new psycopg2 connection (RealDictCursor by default)
      - cur  is conn.cursor(), so it yields rows as dictionaries.
    Caller is responsible for conn.commit() and conn.close() when done.
    """
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    return conn, cur


from flask import session
# … your other imports …

@app.context_processor
def inject_unread_comments():
    if 'username' not in session:
        return {}
    conn, cur = get_db_cursor()
    cur.execute("""
      SELECT
        rc.request_id,
        COUNT(*) AS cnt
      FROM request_comments rc
      LEFT JOIN discussion_read dr
        ON dr.request_id = rc.request_id
       AND dr.username   = %s
      WHERE rc.commented_at > COALESCE(dr.last_read_at, '1970-01-01')
        AND rc.commenter  != %s
      GROUP BY rc.request_id
    """, (session['username'], session['username']))
    rows = cur.fetchall()
    conn.close()

    unread_per_request = { r['request_id']: r['cnt'] for r in rows }
    total_unread       = sum(unread_per_request.values())

    return {
      'unread_comments':      total_unread,
      'unread_per_request':   unread_per_request
    }

@app.route('/contact')
def contact_us():
    # Only viewers should see it:
    if 'username' not in session or session.get('role') != 'viewer':
        return "Unauthorized", 403

    last_updated = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    return render_template('contact_us.html', last_updated=last_updated)

# @app.route('/')
# def dashboard():
#     if 'username' not in session:
#         return redirect(url_for('login'))

#     conn = get_db()
#     c = conn.cursor()
#     c.execute("SELECT * FROM products")
#     products = c.fetchall()

#     edit_requests = []
#     if session.get('role') == 'admin':
#         # OLD:
#         # c.execute("SELECT * FROM edit_requests WHERE status='pending'")
#         # edit_requests = c.fetchall()

#         # NEW:
#         c.execute("SELECT * FROM request_history WHERE status='pending' ORDER BY requested_at DESC")
#         edit_requests = c.fetchall()

#     conn.close()
#     return render_template('dashboard.html', products=products, role=session.get('role'), edit_requests=edit_requests)


# inside app.py (or wherever you defined dashboard)
from flask import request  # make sure this is already imported

@app.route('/')
def dashboard():
    if 'username' not in session:
        return redirect(url_for('login'))

    # 1) grab the 'search' parameter (GET)
    search_term = request.args.get('search', '').strip()

    conn, cur = get_db_cursor()
    if search_term:
        # Use ILIKE for case‐insensitive match in PostgreSQL
        cur.execute("""
            SELECT *
              FROM products
             WHERE name ILIKE %s
             ORDER BY id
        """, (f'%{search_term}%',))
    else:
        cur.execute("SELECT * FROM products ORDER BY id")
    products = cur.fetchall()
    conn.close()

    # 2) If admin, fetch pending requests exactly as before
    edit_requests = []
    if session.get('role') == 'admin':
        conn2, cur2 = get_db_cursor()
        cur2.execute("""
            SELECT *
              FROM request_history
             WHERE status = 'pending'
             ORDER BY requested_at DESC
        """)
        edit_requests = cur2.fetchall()
        conn2.close()

    # 3) Count open jobs for the badge / summary
    conn3, cur3 = get_db_cursor()
    if session.get('role') == 'admin':
        cur3.execute("SELECT COUNT(*) AS cnt FROM job_assignment WHERE status='pending'")
    else:
        cur3.execute(
            "SELECT COUNT(*) AS cnt FROM job_assignment "
            "WHERE assigned_to = %s AND status != 'completed'",
            (session['username'],)
        )
    pending_jobs = cur3.fetchone()['cnt']
    conn3.close()

    # 4) Pass everything into dashboard.html
    return render_template(
        'dashboard.html',
        products      = products,
        role          = session.get('role'),
        edit_requests = edit_requests,
        search        = search_term,
        pending_jobs  = pending_jobs
        )


@app.route('/add', methods=['POST'])
def add_product():
    if session.get('role') != 'admin':
        return "Unauthorized", 403
    name = request.form['name']
    type_ = request.form['type']
    quantity = request.form['quantity']
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT INTO products (name, type, quantity) VALUES (%s,%s,%s)', (name,type_,quantity))
    conn.commit()
    conn.close()
    return redirect(url_for('dashboard'))

def send_low_stock_alert_if_needed(product_id, new_quantity):
    """
    If new_quantity < reorder_level, fetch all admin emails and send an alert.
    """
    conn, cur = get_db_cursor()
    # 1) Fetch reorder_level and name
    cur.execute(
        "SELECT name, reorder_level FROM products WHERE id = %s",
        (product_id,)
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return

    name         = row['name']
    reorder_lvl  = row['reorder_level']

    if new_quantity < reorder_lvl:
        # 2) Fetch all admin emails
        conn, cur = get_db_cursor()
        cur.execute("SELECT email FROM users WHERE role = 'admin' AND email IS NOT NULL")
        admins = cur.fetchall()
        conn.close()

        admin_emails = [r['email'] for r in admins if r.get('email')]
        if not admin_emails:
            return

        # 3) Compose and send the email
        msg = Message(
            subject=f"[ALERT] Low stock: {name}",
            recipients=admin_emails
        )
        msg.body = (
            f"Attention Inventory Admins,\n\n"
            f"The product “{name}” (ID {product_id}) has fallen below its reorder level ({reorder_lvl}).\n"
            f"Current quantity is {new_quantity}.\n\n"
            "Please consider re‐ordering soon.\n\n"
            "– Inventory System"
        )
        mail.send(msg)


@app.route('/edit/<int:id>', methods=['POST'])
def edit_product(id):
    if session.get('role') != 'admin':
        return "Unauthorized", 403

    name = request.form['name']
    type_ = request.form['type']
    new_quantity = int(request.form['quantity'])

    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT * FROM products WHERE id = %s", (id,))
    product = c.fetchone()

    if not product:
        conn.close()
        flash("Product not found.", "error")
        return redirect(url_for('dashboard'))

    old_quantity = product['quantity']
    change_amount = new_quantity - old_quantity

    # Update product
    c.execute('UPDATE products SET name = %s, type = %s, quantity = %s WHERE id = %s', 
              (name, type_, new_quantity, id))

    # Log to stock_history
    from datetime import datetime
    from zoneinfo import ZoneInfo
    changed_at = datetime.now(ZoneInfo("Asia/Kolkata")).strftime('%Y-%m-%d %H:%M:%S')

    c.execute('''
        INSERT INTO stock_history (
            product_id, product_name, changed_by, old_quantity, new_quantity, change_amount, changed_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
    ''', (id, name, session['username'], old_quantity, new_quantity, change_amount, changed_at))

    conn.commit()
    conn.close()
    flash("Product updated and change logged.", "success")
    return redirect(url_for('dashboard'))



@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        # conn = sqlite3.connect('inventory.db')
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE username=%s AND password=%s", (username, password))
        user = c.fetchone()
        conn.close()

        if user:
            session['username'] = user['username']
            session['role'] = user['role']  # 'admin' or 'viewer'
            return redirect(url_for('dashboard'))
        else:
            return "Invalid credentials"
    return render_template('login.html')


from datetime import datetime
from zoneinfo import ZoneInfo  # Requires Python 3.9+

@app.route('/request_edit/<int:id>', methods=['POST'])
def request_edit(id):
    if session.get('role') != 'viewer':
        return "Unauthorized", 403

    requested_quantity = int(request.form['requested_quantity'])
    requested_by = session['username']

    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT * FROM products WHERE id = %s", (id,))
    product = c.fetchone()

    if product is None:
        conn.close()
        flash("Product not found.", "error")
        return redirect(url_for('dashboard'))

    if requested_quantity > product['quantity']:
        conn.close()
        flash("Requested quantity exceeds available stock.", "error")
        return redirect(url_for('dashboard'))

    # Get current time in desired timezone (e.g., Asia/Kolkata)
    requested_at = datetime.now(ZoneInfo("Asia/Kolkata")).strftime('%Y-%m-%d %H:%M:%S')

    # Insert into request_history with requested_at
    c.execute('''
        INSERT INTO request_history (
            product_id, product_name, requested_quantity, requested_by, status, requested_at
        ) VALUES (%s, %s, %s, %s, 'pending', %s)
    ''', (id, product['name'], requested_quantity, requested_by, requested_at))

    conn.commit()
    conn.close()

    flash("Item request submitted to admin.", "info")
    return redirect(url_for('dashboard'))

from datetime import datetime
from zoneinfo import ZoneInfo  # Python 3.9+

from datetime import datetime
from zoneinfo import ZoneInfo


@app.route('/approve_request/<int:request_id>', methods=['GET', 'POST'])
def approve_request(request_id):
    if session.get('role') != 'admin':
        return "Unauthorized", 403

    # 1) Fetch the pending request from request_history
    conn, cur = get_db_cursor()
    cur.execute("SELECT * FROM request_history WHERE id = %s", (request_id,))
    req = cur.fetchone()
    conn.close()

    if not req or req['status'] != 'pending':
        # Either no such request or it's already handled
        flash("Request not found or already handled.", "error")
        return redirect(url_for('dashboard'))

    # ─────────────── GET: Show the "Approve" form with comment box ───────────────
    if request.method == 'GET':
        return render_template(
            'approve_request.html',
            req_id          = request_id,
            product_name    = req['product_name'],
            requested_qty   = req['quantity'],
            current_comment = req.get('comment', '') or ''
        )

    # ─────────────── POST: Process the approval ───────────────
    admin_comment = request.form.get('admin_comment', '').strip()

    # 2) Re‐fetch product to check stock
    conn, cur = get_db_cursor()
    cur.execute("SELECT * FROM products WHERE id = %s", (req['product_id'],))
    product = cur.fetchone()

    if not product or product['quantity'] < req['quantity']:
        conn.close()
        flash("Insufficient stock to approve this request.", "error")
        return redirect(url_for('dashboard'))

    # 3) Deduct inventory
    new_qty = product['quantity'] - req['quantity']
    cur.execute(
        "UPDATE products SET quantity = %s WHERE id = %s",
        (new_qty, req['product_id'])
    )

    # 4) Compute GST amounts
    approved_qty    = req['quantity']
    price_per_item  = product['price']   # assumes a "price" column on products
    gst_exclusive   = price_per_item * approved_qty
    total_inclusive = round(gst_exclusive * 1.18, 2)

    decision_at_str = datetime.now(ZoneInfo("Asia/Kolkata")).strftime('%Y-%m-%d %H:%M:%S')

    # 5) Update request_history, including the admin's comment
    cur.execute(
        """
        UPDATE request_history
        SET 
            status          = 'approved',
            decision_at     = %s,
            decided_by      = %s,
            used            = 0,
            remaining       = quantity,
            gst_exclusive   = %s,
            total_inclusive = %s,
            comment         = %s
        WHERE id = %s
        """,
        (
            decision_at_str,
            session['username'],   # admin's username
            gst_exclusive,
            total_inclusive,
            admin_comment,
            request_id
        )
    )
    conn.commit()
    conn.close()

    # 6) Send email to the requesting viewer (if they have an email on file)
    try:
        conn, cur = get_db_cursor()
        viewer_username = req['username']
        cur.execute("SELECT email FROM users WHERE username = %s", (viewer_username,))
        viewer_row = cur.fetchone()
        conn.close()

        if viewer_row and viewer_row.get('email'):
            viewer_email = viewer_row['email']
            msg = Message(
                subject=f"Your request #{request_id} has been APPROVED",
                recipients=[viewer_email]
            )
            msg.body = (
                f"Hello {viewer_username},\n\n"
                f"Your request for {approved_qty} × {req['product_name']} has been *APPROVED*.\n"
                f"  • Approved quantity: {approved_qty}\n"
                f"  • GST‐exclusive amount: ₹{gst_exclusive:.2f}\n"
                f"  • Total (incl. 18% GST): ₹{total_inclusive:.2f}\n"
                f"  • Admin comment: {admin_comment or '—'}\n\n"
                "Thank you,\nInventory Team"
            )
            mail.send(msg)
    except Exception as e:
        flash(f"⚠️ Could not send approval email to {viewer_username}: {e}", "warning")

    flash("Request approved, stock updated, and email sent to viewer.", "success")
    return redirect(url_for('dashboard'))


@app.route('/reject_request/<int:request_id>', methods=['GET', 'POST'])
def reject_request(request_id):
    if session.get('role') != 'admin':
        return "Unauthorized", 403

    # 1) Fetch the pending request
    conn, cur = get_db_cursor()
    cur.execute("SELECT * FROM request_history WHERE id = %s", (request_id,))
    req = cur.fetchone()
    conn.close()

    if not req or req['status'] != 'pending':
        flash("Request not found or already handled.", "error")
        return redirect(url_for('dashboard'))

    # ─────────────── GET: Show the "Reject" form with comment box ───────────────
    if request.method == 'GET':
        return render_template(
            'reject_request.html',
            req_id          = request_id,
            product_name    = req['product_name'],
            requested_qty   = req['quantity'],
            current_comment = req.get('comment', '') or ''
        )

    # ─────────────── POST: Process the rejection ───────────────
    admin_comment   = request.form.get('admin_comment', '').strip()
    decision_at_str = datetime.now(ZoneInfo("Asia/Kolkata")).strftime('%Y-%m-%d %H:%M:%S')

    conn, cur = get_db_cursor()
    cur.execute(
        """
        UPDATE request_history
        SET 
            status      = 'rejected',
            decision_at = %s,
            decided_by  = %s,
            comment     = %s
        WHERE id = %s
        """,
        (
            decision_at_str,
            session['username'],  # admin’s username
            admin_comment,
            request_id
        )
    )
    conn.commit()
    conn.close()

    # Send rejection email to the viewer, if available
    try:
        conn, cur = get_db_cursor()
        viewer_username = req['username']
        cur.execute("SELECT email FROM users WHERE username = %s", (viewer_username,))
        viewer_row = cur.fetchone()
        conn.close()

        if viewer_row and viewer_row.get('email'):
            viewer_email = viewer_row['email']
            msg = Message(
                subject=f"Your request #{request_id} has been REJECTED",
                recipients=[viewer_email]
            )
            msg.body = (
                f"Hello {viewer_username},\n\n"
                f"Your request for {req['quantity']} × {req['product_name']} has been *REJECTED*.\n"
                f"  • Admin comment: {admin_comment or '—'}\n\n"
                "Please contact the inventory team if you have questions.\n\n"
                "Regards,\nInventory System"
            )
            mail.send(msg)
    except Exception as e:
        flash(f"⚠️ Could not send rejection email to {viewer_username}: {e}", "warning")

    flash("Request rejected, comment saved, and email sent to viewer.", "info")
    return redirect(url_for('dashboard'))



@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully!', 'info')
    return redirect(url_for('login'))

# @app.route('/history')
# def viewer_history():
#     if 'username' not in session:
#         return redirect(url_for('login'))

#     conn, cur = get_db_cursor()
#     if session['role'] == 'viewer':
#         cur.execute('''
#             SELECT *
#             FROM request_history
#             WHERE username = %s
#             ORDER BY requested_at DESC
#         ''', (session['username'],))
#     else:
#         cur.execute('''
#             SELECT *
#             FROM request_history
#             ORDER BY requested_at DESC
#         ''')

#     history_rows = cur.fetchall()
#     conn.close()
#     return render_template('history.html', history=history_rows)


@app.route('/history')
def viewer_history():
    if 'username' not in session:
        return redirect(url_for('login'))

    conn, cur = get_db_cursor()

    # 1) Fetch the history rows
    if session['role'] == 'viewer':
        cur.execute(
            '''
            SELECT *
            FROM request_history
            WHERE username = %s
            ORDER BY requested_at DESC
            ''',
            (session['username'],)
        )
    else:
        cur.execute(
            '''
            SELECT *
            FROM request_history
            ORDER BY requested_at DESC
            '''
        )
    history = cur.fetchall()

    # 2) Build an attachments map: { request_id: [<attachment rows>] }
    attach_map = {}
    for row in history:
        cur.execute(
            "SELECT * FROM attachments WHERE request_id = %s ORDER BY uploaded_at",
            (row['id'],)
        )
        attach_map[row['id']] = cur.fetchall()

    conn.close()

    # 3) Pass both history *and* attachments into the template
    return render_template(
        'history.html',
        history=history,
        attachments=attach_map
    )



@app.route('/api/pending_requests')
def get_pending_requests():
    if session.get('role') != 'admin':
        return "Forbidden", 403

    conn, cur = get_db_cursor()
    cur.execute('''
        SELECT
          id,
          product_id,
          product_name,
          quantity,
          reason,
          sub_reason,
          drone_number,
          username AS requested_by,
          requested_at
        FROM request_history
        WHERE status = 'pending'
        ORDER BY requested_at DESC
    ''')
    rows = cur.fetchall()
    conn.close()

    result = []
    for r in rows:
        # r['requested_at'] is a datetime, convert to string
        requested_at_str = r['requested_at'].strftime('%Y-%m-%d %H:%M:%S')
        result.append({
            "id":                 r["id"],
            "product_id":         r["product_id"],
            "product_name":       r["product_name"],
            "requested_quantity": r["quantity"],       # ← changed key from "quantity" to "requested_quantity"
            "reason":             r["reason"],
            "sub_reason":         r["sub_reason"],
            "drone_number":       r["drone_number"],
            "requested_by":       r["requested_by"],
            "requested_at":       requested_at_str
        })
    return {"requests": result}


@app.route('/api/download-filtered-excel', methods=['POST'])
def download_filtered_excel():
    if 'username' not in session or session.get('role') != 'admin':
        return "Unauthorized", 403

    data = request.json.get('data', [])

    if not data:
        return "No data provided", 400

    # Define column names matching the 13‐column order sent from JS:
    columns = [
        'ID',
        'Product',
        'Qty',
        'Reason',
        'Sub Reason',
        'Drone No.',
        'Status',
        'Requested At',
        'Decision At',
        'Admin',
        'Requested By',
        'Used',
        'Remaining'
    ]

    # Create DataFrame with those columns
    df = pd.DataFrame(data, columns=columns)

    output = BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False, sheet_name='Filtered History')

    output.seek(0)
    return send_file(
        output,
        download_name="filtered_request_history.xlsx",
        as_attachment=True,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


@app.route('/stock_history')
def stock_history():
    if session.get('role') != 'admin':
        return "Unauthorized", 403

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM stock_history ORDER BY changed_at DESC")
    history = c.fetchall()
    conn.close()
    return render_template('stock_history.html', history=history)


@app.route('/download_stock_history')
def download_stock_history():
    if session.get('role') != 'admin':
        return "Unauthorized", 403

    keyword = request.args.get('q', '').strip()

    conn = get_db()
    c = conn.cursor()

    if keyword:
        c.execute('''
            SELECT * FROM stock_history
            WHERE product_name LIKE %s
            ORDER BY changed_at DESC
        ''', (f'%{keyword}%',))
    else:
        c.execute("SELECT * FROM stock_history ORDER BY changed_at DESC")

    rows = c.fetchall()
    conn.close()

    df = pd.DataFrame(rows, columns=[desc[0] for desc in c.description])

    output = BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)

    return send_file(output, as_attachment=True, download_name="stock_history.xlsx", mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

@app.route('/add_to_cart', methods=['POST'])
def add_to_cart():
    if session.get('role') != 'viewer':
        return "Unauthorized", 403

    # 1. Read the new quantity field from the form:
    try:
        requested_qty = int(request.form['quantity'])
        if requested_qty < 1:
            raise ValueError()
    except (KeyError, ValueError):
        flash("Please provide a valid quantity (1 or more).", "error")
        return redirect(url_for('dashboard'))

    product_id = int(request.form['product_id'])
    reason = request.form['reason']
    sub_reason = request.form.get('sub_reason', '')
    drone_number = request.form['drone_number']

    if not reason or not drone_number:
        flash("Reason and Drone Number are required.", "error")
        return redirect(url_for('dashboard'))

    # Initialize cart
    if 'cart' not in session:
        session['cart'] = []

    # Prevent duplicates (same product_id) – you could also allow duplicates if you prefer
    for item in session['cart']:
        if item['product_id'] == product_id:
            flash("This item is already in your cart.", "warning")
            return redirect(url_for('dashboard'))

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT name FROM products WHERE id = %s", (product_id,))
    result = c.fetchone()
    conn.close()

    if not result:
        flash("Product not found.", "error")
        return redirect(url_for('dashboard'))

    product_name = result['name']

    # Add to cart, including the quantity
    session['cart'].append({
        'product_id': product_id,
        'product_name': product_name,
        'quantity': requested_qty,      # <— NEW FIELD
        'reason': reason,
        'sub_reason': sub_reason,
        'drone_number': drone_number
    })

    session.modified = True
    flash("Item added to cart.", "success")
    return redirect(url_for('dashboard'))


@app.route('/submit_cart', methods=['POST'])
def submit_cart():
    if 'username' not in session or session.get('role') != 'viewer':
        return "Unauthorized", 403

    cart = session.get('cart', [])
    if not cart:
        flash("Your cart is empty.", "error")
        return redirect(url_for('view_cart'))

    username  = session['username']
    timestamp = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S")

    conn, cur = get_db_cursor()

    # 1) Insert each item into request_history, capture its new ID
    request_ids = []
    for item in cart:
        cur.execute('''
            INSERT INTO request_history
              (username, product_id, product_name, quantity,
               reason, sub_reason, drone_number, status, requested_at, comment)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s, '')
            RETURNING id
        ''', (
            username,
            item['product_id'],
            item['product_name'],
            item['quantity'],
            item['reason'],
            item['sub_reason'],
            item['drone_number'],
            timestamp
        ))
        new_id = cur.fetchone()['id']
        request_ids.append(new_id)

    # 2) Handle file attachments
    files = request.files.getlist('attachments')
    for idx, req_id in enumerate(request_ids):
        # You could attach *all* files to each request, or better:
        # rotate so that first file goes to first request, etc.
        # Here, we attach *all* files to *every* request:
        for f in files:
            filename = secure_filename(f.filename)
            ext = filename.rsplit('.',1)[-1].lower()
            if filename and ext in ALLOWED_EXT:
                # stored = f"{req_id}_{filename}"
                key = f"{req_id}/{filename}"
                r2.upload_fileobj(f,R2_BUCKET,key)
                cur.execute('''
                    INSERT INTO attachments
                      (request_id, filename, stored_path, uploaded_by)
                    VALUES (%s, %s, %s, %s)
                ''', (req_id, filename, key, username))

    conn.commit()
    conn.close()

    # 3) Notify all admins via email
    try:
        conn2, cur2 = get_db_cursor()
        cur2.execute("SELECT email FROM users WHERE role = 'admin'")
        admins = [r['email'] for r in cur2.fetchall() if r['email']]
        conn2.close()

        if admins:
            lines = [f"User {username} submitted the following requests:"]
            for item in cart:
                lines.append(
                    f"  • {item['quantity']} × {item['product_name']} "
                    f"(Reason: {item['reason']}, Drone: {item['drone_number']})"
                )
            msg = Message(
                subject=f"New Inventory Request from {username}",
                recipients=admins
            )
            msg.body = "\n".join(lines)
            mail.send(msg)
    except Exception as e:
        flash(f"⚠️ Could not email admins: {e}", "warning")

    # 4) Clear cart and redirect
    session['cart'] = []
    flash("All requests submitted and attachments uploaded! Admins have been notified.", "success")
    return redirect(url_for('dashboard'))


@app.route('/view_cart')
def view_cart():
    if 'username' not in session or session.get('role') != 'viewer':
        return "Unauthorized", 403

    cart = session.get('cart', [])
    return render_template('view_cart.html', cart=cart)



# @app.route('/edit_usage/<int:request_id>', methods=['GET', 'POST'])
# def edit_usage(request_id):
#     # 1) Only logged-in viewers can access this
#     if 'username' not in session or session.get('role') != 'viewer':
#         return "Unauthorized", 403

#     conn = get_db()
#     c = conn.cursor()

#     # 2) Fetch that specific request row
#     c.execute("SELECT * FROM request_history WHERE id = %s", (request_id,))
#     req = c.fetchone()

#     # 3) Validate that it exists, belongs to the current viewer, and is approved
#     if not req or req['username'] != session['username'] or req['status'] != 'approved':
#         conn.close()
#         flash("You cannot update usage for this request.", "error")
#         return redirect(url_for('viewer_history'))  # or 'history'

#     # If it’s a GET request, show the form
#     if request.method == 'GET':
#         conn.close()
#         return render_template(
#             'edit_usage.html',
#             req_id=req['id'],
#             used=req['used'],
#             remaining=req['remaining'],
#             approved_qty=req['quantity']
#         )

#     # Otherwise, it’s a POST → process the form submission
#     try:
#         used = int(request.form['used'])
#         remaining = int(request.form['remaining'])
#     except (KeyError, ValueError):
#         flash("Please enter valid integer values.", "error")
#         conn.close()
#         return redirect(url_for('edit_usage', request_id=request_id))

#     # Enforce that used + remaining === approved quantity
#     approved_qty = req['quantity']
#     if used < 0 or remaining < 0 or (used + remaining) != approved_qty:
#         flash("Used + Remaining must exactly equal the approved quantity.", "error")
#         conn.close()
#         return redirect(url_for('edit_usage', request_id=request_id))

#     # Update the row
#     c.execute(
#         "UPDATE request_history SET used = %s, remaining = %s WHERE id = %s",
#         (used, remaining, request_id)
#     )
#     conn.commit()
#     conn.close()

#     flash("Usage updated successfully.", "success")
#     return redirect(url_for('viewer_history'))

@app.route('/edit_usage/<int:request_id>', methods=['GET','POST'])
def edit_usage(request_id):
    if 'username' not in session or session.get('role')!='viewer':
        return "Unauthorized", 403

    conn, cur = get_db_cursor()
    cur.execute("SELECT * FROM request_history WHERE id=%s", (request_id,))
    req = cur.fetchone()

    # must exist, be this user’s, and already approved
    if not req or req['username']!=session['username'] or req['status']!='approved':
        conn.close()
        flash("You cannot update usage for this request.", "error")
        return redirect(url_for('viewer_history'))

    # GET: render form
    if request.method=='GET':
        remark   = req.get('usage_remark','')   or ''
        location = req.get('usage_location','') or ''
        used     = req['used']
        remaining= req['remaining']
        approved = req['quantity']
        conn.close()
        return render_template('edit_usage.html',
                               req_id       = request_id,
                               used         = used,
                               remaining    = remaining,
                               approved_qty = approved,
                               remark       = remark,
                               location     = location)

    # POST: process submission
    # ── debug print ──
    print("▶︎ [edit_usage] POST, form:", dict(request.form))

    try:
        used      = int(request.form['used'])
        remaining = int(request.form['remaining'])
        remark    = request.form.get('remark','').strip()
        location  = request.form.get('location','').strip()
    except ValueError:
        flash("Please enter valid numbers.", "error")
        conn.close()
        return redirect(url_for('edit_usage', request_id=request_id))

    # enforce used+remaining equals approved
    if used<0 or remaining<0 or (used+remaining)!=req['quantity']:
        flash("Used + Remaining must exactly equal approved quantity.", "error")
        conn.close()
        return redirect(url_for('edit_usage', request_id=request_id))

    print(f"→ saving remark: {remark!r}, location: {location!r} for req {request_id}")

    cur.execute("""
      UPDATE request_history
         SET used            = %s,
             remaining       = %s,
             usage_remark    = %s,
             usage_location  = %s
       WHERE id = %s
    """, (used, remaining, remark, location, request_id))

    conn.commit()
    conn.close()

    flash("Usage, remark and location updated successfully.", "success")
    return redirect(url_for('viewer_history'))


@app.route('/test-email')
def test_email():
    """
    A quick route to verify your SMTP setup. Visit /test-email in browser.
    """
    try:
        msg = Message(
            subject    = "Test Email from Flask",
            recipients = ["chetanaggarwal21123@gmail.com"]
        )
        msg.body = "If you see this, SMTP is working!"
        mail.send(msg)
        return "✓ Email sent (check your inbox)."
    except Exception as e:
        return f"Error sending email: {e}"

# @app.route('/analytics')
# def analytics():
#     # Only admins can view analytics
#     if 'username' not in session or session.get('role') != 'admin':
#         return redirect(url_for('dashboard'))

#     # ─── 1) Top 10 Most Requested Items (Last 30 days) ───
#     conn, cur = get_db_cursor()

#     # Compute the "30 days ago" cutoff in Asia/Kolkata
#     thirty_days_ago = (datetime.now(ZoneInfo("Asia/Kolkata")) - timedelta(days=45)).strftime('%Y-%m-%d %H:%M:%S')

#     # Sum up approved quantities per product_name in the last 30 days
#     cur.execute("""
#         SELECT
#           product_name,
#           SUM(quantity) AS total_requested
#         FROM request_history
#         WHERE status = 'approved'
#           AND decision_at::timestamp >= %s
#         GROUP BY product_name
#         ORDER BY total_requested DESC
#         LIMIT 10
#     """, (thirty_days_ago,))
#     top_rows = cur.fetchall()
#     conn.close()

#     top_requested = [
#         { 'product_name': r['product_name'], 'total_requested': int(r['total_requested']) }
#         for r in top_rows
#     ]

#     # ─── 2) Daily Approved Quantity (Last 30 days) ───
#     # First initialize a dict for each of the last 30 calendar dates (YYYY-MM-DD) → 0
#     daily_counts = {}
#     today_date = datetime.now(ZoneInfo("Asia/Kolkata")).date()
#     for i in range(45):
#         day = today_date - timedelta(days=44 - i)
#         daily_counts[day.isoformat()] = 0

#     # Now fetch actual sums, grouping by the “date” portion of decision_at (shifted into Asia/Kolkata)
#     conn, cur = get_db_cursor()
#     cur.execute("""
#         SELECT
#           DATE( (decision_at::timestamp) AT TIME ZONE 'Asia/Kolkata' ) AS day_date,
#           SUM(quantity) AS daily_approved
#         FROM request_history
#         WHERE status = 'approved'
#           AND decision_at::timestamp >= %s
#         GROUP BY day_date
#         ORDER BY day_date
#     """, (thirty_days_ago,))
#     trend_rows = cur.fetchall()
#     conn.close()

#     for tr in trend_rows:
#         day_str = tr['day_date'].isoformat()        # e.g. '2025-05-10'
#         if day_str in daily_counts:
#             daily_counts[day_str] = int(tr['daily_approved'])

#     # Build a list of dicts in date order:
#     usage_trend = [
#         { 'day_date': date_str, 'daily_approved': qty }
#         for date_str, qty in daily_counts.items()
#     ]

#     # ─── 3) Render template with both lists ───
#     return render_template(
#         'analytics.html',
#         top_requested=top_requested,
#         usage_trend=usage_trend
#     )


from flask import request, session, redirect, url_for, render_template
from datetime import date, timedelta, datetime
from zoneinfo import ZoneInfo

@app.route('/analytics')
def analytics():
    # only admins
    if session.get('role') != 'admin':
        return redirect(url_for('dashboard'))

    conn, cur = get_db_cursor()

    # 1) product dropdown
    cur.execute("SELECT name FROM products ORDER BY name")
    product_list = [r['name'] for r in cur.fetchall()]

    # 2) which product?
    selected = request.args.get('product', 'All')

    # 3) last 45 days window
    today = date.today()
    start = today - timedelta(days=44)

    # 4) Top‐10 bar data (always “All”)
    cur.execute("""
      SELECT product_name, SUM(quantity) AS total_requested
      FROM request_history
      WHERE status='approved'
        AND decision_at::timestamp >= %s
      GROUP BY product_name
      ORDER BY total_requested DESC
      LIMIT 10
    """, (start,))
    top_requested = [
      {'product_name': r['product_name'], 'total_requested': int(r['total_requested'])}
      for r in cur.fetchall()
    ]

    # 5) Line data
    if selected == 'All':
        # daily approved
        cur.execute("""
          SELECT
            DATE(decision_at::timestamp AT TIME ZONE 'Asia/Kolkata') AS day,
            SUM(quantity) AS level
          FROM request_history
          WHERE status='approved'
            AND decision_at::timestamp >= %s
          GROUP BY day
          ORDER BY day
        """, (start,))
        usage_trend = [
          {'day': r['day'].strftime('%Y-%m-%d'), 'level': int(r['level'])}
          for r in cur.fetchall()
        ]

    else:
        # per‐product net change, then cumulative
        cur.execute("""
          WITH used AS (
            SELECT
              DATE(decision_at::timestamp AT TIME ZONE 'Asia/Kolkata') AS day,
              SUM(quantity) AS u
            FROM request_history
            WHERE status='approved'
              AND product_name = %s
              AND decision_at::timestamp >= %s
            GROUP BY 1
          ), rec AS (
            SELECT
              DATE(changed_at::timestamp AT TIME ZONE 'Asia/Kolkata') AS day,
              SUM(change_amount) AS r
            FROM stock_history
            WHERE product_name = %s
              AND changed_at::timestamp >= %s
            GROUP BY 1
          ), days AS (
            SELECT generate_series(%s::date, %s::date, '1 day') AS day
          )
          SELECT
            days.day,
            COALESCE(rec.r,0) - COALESCE(used.u,0) AS net_change
          FROM days
          LEFT JOIN used ON used.day = days.day
          LEFT JOIN rec  ON rec.day  = days.day
          ORDER BY days.day
        """, (selected, start, selected, start, start, today))
        rows = cur.fetchall()

        # fetch “current” and back‐compute
        cur.execute("SELECT quantity FROM products WHERE name=%s", (selected,))
        current = cur.fetchone()['quantity']

        # cumulative
        level = current - sum(r['net_change'] for r in rows)
        usage_trend = []
        for r in rows:
            level += r['net_change']
            usage_trend.append({
              'day':   r['day'].strftime('%Y-%m-%d'),
              'level': level
            })

    conn.close()

    return render_template('analytics.html',
                           product_list  = product_list,
                           selected      = selected,
                           top_requested = top_requested,
                           usage_trend   = usage_trend)


# @app.route('/request/<int:request_id>/comments', methods=['GET','POST'])
# def comment_thread(request_id):
#     user = session.get('username')
#     if not user:
#         return redirect(url_for('login'))

#     conn, cur = get_db_cursor()

#     if request.method == 'POST':
#         text = request.form.get('comment', '').strip()
#         if text:
#             cur.execute('''
#               INSERT INTO request_comments
#                 (request_id, commenter, comment_text)
#               VALUES (%s, %s, %s)
#             ''', (request_id, user, text))
#             conn.commit()
#         conn.close()

#         # ← Redirect *after* handling POST to clear the form
#         return redirect(url_for('comment_thread', request_id=request_id))

#     # GET branch: render the page
#     cur.execute("SELECT * FROM request_history WHERE id = %s", (request_id,))
#     req = cur.fetchone()

#     cur.execute("""
#       SELECT * 
#       FROM request_comments 
#       WHERE request_id = %s
#       ORDER BY commented_at
#     """, (request_id,))
#     comments = cur.fetchall()
#     conn.close()

#     return render_template(
#         'comment_thread.html',
#         req=req,
#         comments=comments
#     )



# @app.route('/request/<int:request_id>/comments', methods=['GET','POST'])
# def comment_thread(request_id):
#     user = session.get('username')
#     if not user:
#         return redirect(url_for('login'))

#     conn, cur = get_db_cursor()

#     if request.method == 'POST':
#         text = request.form['comment'].strip()
#         if text:
#             cur.execute(
#               "INSERT INTO request_comments (request_id, commenter, comment_text) VALUES (%s,%s,%s)",
#               (request_id, user, text)
#             )
#             conn.commit()
#         conn.close()
#         return redirect(url_for('comment_thread', request_id=request_id))

#     # — GET: fetch the request & its comments
#     cur.execute("SELECT * FROM request_history WHERE id = %s", (request_id,))
#     req = cur.fetchone()
#     cur.execute("SELECT * FROM request_comments WHERE request_id = %s ORDER BY commented_at", (request_id,))
#     comments = cur.fetchall()

#     # — mark it as read
#     now = datetime.now(ZoneInfo("Asia/Kolkata"))
#     cur.execute("""
#       INSERT INTO discussion_read (request_id, username, last_read_at)
#       VALUES (%s,%s,%s)
#       ON CONFLICT (request_id, username)
#       DO UPDATE SET last_read_at = EXCLUDED.last_read_at
#     """, (request_id, user, now))
#     conn.commit()
#     conn.close()

#     return render_template('comment_thread.html', req=req, comments=comments)


from werkzeug.utils import secure_filename

ALLOWED_EXT = {'png','jpg','jpeg','pdf','docx'}

@app.route('/request/<int:request_id>/comments', methods=['GET','POST'])
def comment_thread(request_id):
    user = session.get('username')
    if not user:
        return redirect(url_for('login'))

    conn, cur = get_db_cursor()

    if request.method == 'POST':
        # 1) Insert the comment and get its new ID
        text = request.form.get('comment','').strip()
        cur.execute(
            """
            INSERT INTO request_comments
              (request_id, commenter, comment_text)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (request_id, user, text)
        )
        comment_id = cur.fetchone()['id']

        # 2) Handle uploaded files
        files = request.files.getlist('files')
        for f in files:
            if not f or not f.filename:
                continue
            fn  = secure_filename(f.filename)
            ext = fn.rsplit('.',1)[-1].lower()
            if ext in ALLOWED_EXT:
                key = f"comments/{comment_id}/{fn}"
                r2.upload_fileobj(f,R2_BUCKET,key)
                cur.execute(
                    """
                    INSERT INTO comment_attachments
                      (comment_id, filename, stored_path, uploaded_by)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (comment_id, fn, key, user)
                )

        conn.commit()
        conn.close()

        # Post-redirect-get so refresh won’t repost files/comments
        return redirect(url_for('comment_thread', request_id=request_id))

    # ── GET branch ──
    # fetch the original request
    cur.execute(
        "SELECT * FROM request_history WHERE id = %s",
        (request_id,)
    )
    req = cur.fetchone()

    # fetch all comments
    cur.execute(
        """
        SELECT * 
          FROM request_comments 
         WHERE request_id = %s
         ORDER BY commented_at
        """,
        (request_id,)
    )
    comments = cur.fetchall()

    # for each comment, fetch its attachments
    attach_map = {}
    for c in comments:
        cur.execute(
            """
            SELECT * 
              FROM comment_attachments
             WHERE comment_id = %s
             ORDER BY uploaded_at
            """,
            (c['id'],)
        )
        attach_map[c['id']] = cur.fetchall()

    # mark discussion as read
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    cur.execute(
        """
        INSERT INTO discussion_read (request_id, username, last_read_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (request_id, username)
        DO UPDATE SET last_read_at = EXCLUDED.last_read_at
        """,
        (request_id, user, now)
    )
    conn.commit()
    conn.close()

    # render with both comments and attachments map
    return render_template(
        'comment_thread.html',
        req=req,
        comments=comments,
        attachments=attach_map
    )


@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)



@app.route('/request/<int:request_id>/attachments')
def view_attachments(request_id):
    # Only logged-in users can view (adjust role logic if needed)
    if 'username' not in session:
        return redirect(url_for('login'))

    conn, cur = get_db_cursor()
    # Fetch the request itself (optional, if you want to show product name)
    cur.execute("SELECT * FROM request_history WHERE id = %s", (request_id,))
    req = cur.fetchone()

    # Fetch all attachments
    cur.execute("""
      SELECT id, filename, stored_path, uploaded_by, uploaded_at
      FROM attachments
      WHERE request_id = %s
      ORDER BY uploaded_at
    """, (request_id,))
    files = cur.fetchall()
    conn.close()

    # generate presigned URLs for each file
    for f in files:
      f['url'] = r2.generate_presigned_url(
        'get_object',
        Params={'Bucket': R2_BUCKET, 'Key': f['stored_path']},
        ExpiresIn=3600
      )
    return render_template(
        'view_attachments.html',
        req=req,
        files=files
    )

# @app.route('/return_remaining/<int:request_id>', methods=['POST'])
# def return_remaining(request_id):
#     if session.get('role') != 'admin':
#         return "Unauthorized", 403

#     conn, cur = get_db_cursor()

#     # 1) Look up the pending request
#     cur.execute("SELECT remaining, product_id, product_name FROM request_history WHERE id = %s", (request_id,))
#     req = cur.fetchone()
#     if not req or req['remaining'] <= 0:
#         conn.close()
#         flash("Nothing to return on that request.", "warning")
#         return redirect(url_for('viewer_history'))

#     returned_qty = req['remaining']
#     prod_id      = req['product_id']
#     prod_name    = req['product_name']

#     # 2) Fetch old stock level
#     cur.execute("SELECT quantity FROM products WHERE id = %s", (prod_id,))
#     prod = cur.fetchone()
#     old_qty = prod['quantity'] if prod else 0

#     # 3) Update products table
#     new_qty = old_qty + returned_qty
#     cur.execute(
#         "UPDATE products SET quantity = %s WHERE id = %s",
#         (new_qty, prod_id)
#     )

#     # 4) Zero‐out the “remaining” in the request_history
#     cur.execute(
#         "UPDATE request_history SET remaining = 0 WHERE id = %s",
#         (request_id,)
#     )

#     # 5) Log it in stock_history
#     changed_at = datetime.now(ZoneInfo("Asia/Kolkata")).strftime('%Y-%m-%d %H:%M:%S')
#     cur.execute('''
#         INSERT INTO stock_history (
#           product_id, product_name, changed_by,
#           old_quantity, new_quantity, change_amount, changed_at
#         ) VALUES (%s, %s, %s, %s, %s, %s, %s)
#     ''', (
#         prod_id,
#         prod_name,
#         session['username'],
#         old_qty,
#         new_qty,
#         returned_qty,
#         changed_at
#     ))

#     conn.commit()
#     conn.close()

#     flash(f"Returned {returned_qty} unit(s) of “{prod_name}” back to stock.", "success")
#     return redirect(url_for('viewer_history'))


from datetime import datetime
from zoneinfo   import ZoneInfo

@app.route('/return_remaining/<int:request_id>', methods=['POST'])
def return_remaining(request_id):
    if session.get('role') != 'admin':
        return "Unauthorized", 403

    conn, cur = get_db_cursor()

    # 1) Fetch the request row
    cur.execute("""
      SELECT remaining, product_id, product_name
        FROM request_history
       WHERE id = %s
    """, (request_id,))
    req = cur.fetchone()

    if not req or req['remaining'] <= 0:
        conn.close()
        flash("Nothing left to return.", "warning")
        return redirect(url_for('dashboard'))

    returned_qty = req['remaining']
    prod_id      = req['product_id']
    prod_name    = req['product_name']

    # 2) Get current stock
    cur.execute("SELECT quantity FROM products WHERE id = %s", (prod_id,))
    old_qty = cur.fetchone()['quantity']

    # 3) Update the product’s quantity
    new_qty = old_qty + returned_qty
    cur.execute(
      "UPDATE products SET quantity = %s WHERE id = %s",
      (new_qty, prod_id)
    )

    # 4) Zero out remaining on the request & save our return_comment
    comment = f"Returned {returned_qty} item(s) to stock"
    cur.execute("""
      UPDATE request_history
         SET remaining      = 0,
             return_comment = %s
       WHERE id = %s
    """, (comment, request_id))

    # 5) Log into stock_history *with* the remark column
    changed_at = datetime.now(ZoneInfo("Asia/Kolkata"))\
                       .strftime('%Y-%m-%d %H:%M:%S')
    cur.execute("""
      INSERT INTO stock_history
        (product_id,
         product_name,
         changed_by,
         old_quantity,
         new_quantity,
         change_amount,
         changed_at,
         remark)
      VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
    """, (
      prod_id,
      prod_name,
      session['username'],
      old_qty,
      new_qty,
      returned_qty,
      changed_at,
      comment
    ))

    conn.commit()
    conn.close()

    flash(comment + ".", "success")
    return redirect(url_for('dashboard'))

@app.route('/remove_from_cart/<int:product_id>', methods=['POST'])
def remove_from_cart(product_id):
    if 'username' not in session or session.get('role') != 'viewer':
        return "Unauthorized", 403

    cart = session.get('cart', [])
    new_cart = [i for i in cart if i['product_id'] != product_id]
    session['cart'] = new_cart
    session.modified = True
    flash("Item removed from cart.", "info")
    return redirect(url_for('view_cart'))

@app.route('/receive_stock', methods=['GET','POST'])
def receive_stock():
    if session.get('role') != 'admin':
        return "Forbidden", 403

    conn, cur = get_db_cursor()

    if request.method == 'POST':
        # 1) grab the uploaded invoice
        f = request.files.get('invoice')
        if not f or f.filename == '':
            flash("Please upload an invoice PDF.", "error")
            return redirect(url_for('receive_stock'))

        ext = f.filename.rsplit('.',1)[-1].lower()
        if ext not in ALLOWED_INVOICE_EXT:
            flash("Only PDF invoices are allowed.", "error")
            return redirect(url_for('receive_stock'))

        invoice_fn = secure_filename(f.filename)
        invoice_stored = f"{int(time.time())}_{invoice_fn}"
        # invoice_path = os.path.join(app.config['UPLOAD_FOLDER'], invoice_stored)
        # f.save(invoice_path)
        invoice_key = f"invoices/{invoice_stored}"
        r2.upload_fileobj(f,R2_BUCKET, invoice_key)

        # 2) for each product row in the form:
        for pid, qty_str in request.form.items():
            if not pid.startswith('qty_'): continue
            product_id = int(pid.split('_',1)[1])
            try:
                received_qty = int(qty_str)
            except ValueError:
                continue
            if received_qty <= 0:
                continue

            # fetch old quantity
            cur.execute("SELECT name, quantity FROM products WHERE id = %s", (product_id,))
            prod = cur.fetchone()
            if not prod: continue

            old_q = prod['quantity']
            new_q = old_q + received_qty

            # update product
            cur.execute("UPDATE products SET quantity=%s WHERE id=%s", (new_q, product_id))

            # insert into stock_history
            now = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S")
            cur.execute("""
              INSERT INTO stock_history
                (product_id, product_name, changed_by,
                 old_quantity, new_quantity, change_amount, changed_at,
                 invoice_filename, invoice_path)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
              product_id,
              prod['name'],
              session['username'],
              old_q, new_q, received_qty, now,
              invoice_fn, invoice_key
            ))

        conn.commit()
        conn.close()
        flash("Stock received and invoice saved.", "success")
        return redirect(url_for('stock_history'))

    # GET: show form
    cur.execute("SELECT id,name,quantity FROM products ORDER BY name")
    products = cur.fetchall()
    conn.close()
    return render_template('receive_stock.html', products=products)

# ─── Job Assign ───────────────────────────────────────────────────────────────

from flask import abort

@app.route('/jobs', methods=['GET', 'POST'])
def jobs():
    # 1) Must be logged in
    if 'username' not in session:
        return redirect(url_for('login'))

    conn, cur = get_db_cursor()

    # 2) Handle creation: only admins may POST to create
    if request.method == 'POST':
        if session['role'] != 'admin':
            abort(403)
        title       = request.form['title']
        desc        = request.form.get('description','').strip()
        assigned_to = request.form['assigned_to']
        due_date    = request.form.get('due_date') or None
        priority    = request.form['priority']

        cur.execute("""
          INSERT INTO job_assignment
            (title, description, assigned_to, due_date, priority)
          VALUES (%s,%s,%s,%s,%s)
        """, (title, desc, assigned_to, due_date, priority))

        # notify via email
        cur.execute("SELECT email FROM users WHERE username=%s", (assigned_to,))
        row = cur.fetchone()
        if row and row.get('email'):
            msg = Message(
              subject=f"[Inventory] New Job Assigned: {title}",
              recipients=[row['email']]
            )
            msg.body = (
              f"Hi {assigned_to},\n\n"
              f"You have a new job:\n"
              f"  • Title: {title}\n"
              f"  • Due:   {due_date or 'No due date'}\n"
              f"  • Priority: {priority}\n\n"
              f"{desc}\n\n"
              "Log in to mark it completed."
            )
            mail.send(msg)

        conn.commit()
        flash("Job created and notified.", "success")
        # reload GET
        return redirect(url_for('jobs'))

    # 3) GET: split admin vs viewer
    if session['role'] == 'admin':
        # — admin sees filters + full list —
        search   = request.args.get('q','').strip()
        status_f = request.args.get('status','All')
        assignee = request.args.get('assigned_to','All')

        filters = []
        params  = []
        if search:
            filters.append("(title ILIKE %s OR description ILIKE %s)")
            params += [f"%{search}%", f"%{search}%"]
        if status_f != 'All':
            filters.append("status=%s"); params.append(status_f)
        if assignee != 'All':
            filters.append("assigned_to=%s"); params.append(assignee)

        sql = "SELECT * FROM job_assignment"
        if filters:
            sql += " WHERE " + " AND ".join(filters)
        sql += " ORDER BY created_at DESC"
        cur.execute(sql, params)
        jobs = cur.fetchall()

        # fetch viewer list for the “assign to” dropdown
        cur.execute("SELECT username FROM users WHERE role='viewer' ORDER BY username")
        viewers = [r['username'] for r in cur.fetchall()]

    else:
        # — viewer sees only their own jobs —
        cur.execute("""
          SELECT * FROM job_assignment
           WHERE assigned_to = %s
           ORDER BY created_at DESC
        """, (session['username'],))
        jobs = cur.fetchall()
        viewers = []  # not used for viewers

    conn.close()
    return render_template('jobs.html',
                           jobs=jobs,
                           viewers=viewers,
                           search=search if session['role']=='admin' else '',
                           status_f=status_f if session['role']=='admin' else 'All',
                           assignee=assignee if session['role']=='admin' else 'All')

@app.route('/jobs/<int:job_id>/complete', methods=['POST'])
def complete_job(job_id):
    # 1) Must be a logged‐in viewer
    if 'username' not in session or session.get('role') != 'viewer':
        return redirect(url_for('login'))

    conn, cur = get_db_cursor()
    # 2) Ensure they own this job
    cur.execute("SELECT assigned_to FROM job_assignment WHERE id = %s", (job_id,))
    row = cur.fetchone()
    if not row or row['assigned_to'] != session['username']:
        conn.close()
        flash("You’re not allowed to complete that job.", "error")
        return redirect(url_for('jobs'))

    # 3) Mark it complete
    cur.execute("UPDATE job_assignment SET status = 'completed' WHERE id = %s", (job_id,))
    conn.commit()
    conn.close()

    flash("Job marked as completed!", "success")
    return redirect(url_for('jobs'))

if __name__ == '__main__':
    # Render (and other PaaS) will set the PORT env var
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)