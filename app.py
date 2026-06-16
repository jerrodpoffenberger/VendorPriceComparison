import json
import os
import uuid
from datetime import datetime
from functools import wraps

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, render_template, request, redirect, url_for, flash, session
from werkzeug.utils import secure_filename

from extensions import db
from models import Vendor, PriceSheet, CanonicalCut, CutMapping, LineItem
from file_parser import parse_file, clean_price

ALLOWED_EXTENSIONS = {'xlsx', 'xls', 'xlsm', 'csv', 'pdf'}

APP_USERNAME = os.environ.get('APP_USERNAME', 'admin')
APP_PASSWORD = os.environ.get('APP_PASSWORD', 'admin')


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-change-in-prod')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///vendor_prices.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

TMP_DIR = os.path.join(os.path.dirname(__file__), 'tmp')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(TMP_DIR, exist_ok=True)

db.init_app(app)

_SEED_CUTS = [
    ("Beef Ribs",                                    "beef"),
    ("CHOICE Boneless Ribeye",                       "beef"),
    ("CHOICE Boneless Sirloin",                      "beef"),
    ("CHOICE NY Strip",                              "beef"),
    ("CHOICE Tenderloin",                            "beef"),
    ("Chuck Roast, Shoulder Clod",                   "beef"),
    ("Chuck Steak, Shoulder Clod",                   "beef"),
    ("Cutlets",                                      "beef"),
    ("Fajita; Inside Skirt",                         "beef"),
    ("PRIME Boneless Ribeye",                        "beef"),
    ("SELECT Bone-In Ribeye",                        "beef"),
    ("SELECT Boneless Ribeye",                       "beef"),
    ("SELECT Short Loins",                           "beef"),
    ("Bone-In Pork Chops, Center Cut Loin",          "pork"),
    ("Boneless Pork Chop, Center Cut Boneless Loin", "pork"),
    ("Country Ribs, Rib Ends",                       "pork"),
]

with app.app_context():
    db.create_all()
    for name, category in _SEED_CUTS:
        if not CanonicalCut.query.filter_by(name=name).first():
            db.session.add(CanonicalCut(name=name, category=category))
    db.session.commit()


# ── Temp session helpers ──────────────────────────────────────────────────────

def _tmp_path(token):
    return os.path.join(TMP_DIR, f'{token}.json')

def _save_tmp(token, data):
    with open(_tmp_path(token), 'w') as f:
        json.dump(data, f)

def _load_tmp(token):
    p = _tmp_path(token)
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)

def _clean_tmp(token):
    p = _tmp_path(token)
    if os.path.exists(p):
        os.remove(p)


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# ── Auth ─────────────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('logged_in'):
        return redirect(url_for('comparison'))
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if username == APP_USERNAME and password == APP_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('comparison'))
        error = 'Invalid username or password.'
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
@login_required
def index():
    return redirect(url_for('comparison'))


# ── Vendors ──────────────────────────────────────────────────────────────────

@app.route('/vendors')
@login_required
def vendors():
    all_vendors = Vendor.query.order_by(Vendor.name).all()
    return render_template('vendors.html', vendors=all_vendors)


@app.route('/vendors/add', methods=['POST'])
@login_required
def add_vendor():
    name = request.form.get('name', '').strip()
    if not name:
        flash('Vendor name is required.', 'danger')
        return redirect(url_for('vendors'))
    if Vendor.query.filter_by(name=name).first():
        flash(f'Vendor "{name}" already exists.', 'warning')
        return redirect(url_for('vendors'))
    db.session.add(Vendor(name=name))
    db.session.commit()
    flash(f'Vendor "{name}" added.', 'success')
    return redirect(url_for('vendors'))


@app.route('/vendors/<int:vendor_id>/delete', methods=['POST'])
@login_required
def delete_vendor(vendor_id):
    vendor = db.get_or_404(Vendor, vendor_id)
    db.session.delete(vendor)
    db.session.commit()
    flash(f'Vendor "{vendor.name}" deleted.', 'success')
    return redirect(url_for('vendors'))


@app.route('/vendors/<int:vendor_id>')
@login_required
def vendor_detail(vendor_id):
    vendor = db.get_or_404(Vendor, vendor_id)
    sheets = PriceSheet.query.filter_by(vendor_id=vendor_id).order_by(PriceSheet.uploaded_at.desc()).all()
    return render_template('vendor_detail.html', vendor=vendor, sheets=sheets)


@app.route('/vendors/<int:vendor_id>/sheets/<int:sheet_id>/activate', methods=['POST'])
@login_required
def activate_sheet(vendor_id, sheet_id):
    PriceSheet.query.filter_by(vendor_id=vendor_id, is_active=True).update({'is_active': False})
    sheet = db.get_or_404(PriceSheet, sheet_id)
    sheet.is_active = True
    db.session.commit()
    flash('Price sheet activated.', 'success')
    return redirect(url_for('vendor_detail', vendor_id=vendor_id))


# ── Upload wizard: Step 1 — pick file ────────────────────────────────────────

@app.route('/vendors/<int:vendor_id>/upload', methods=['GET', 'POST'])
@login_required
def upload(vendor_id):
    vendor = db.get_or_404(Vendor, vendor_id)

    if request.method == 'POST':
        f = request.files.get('file')
        if not f or not f.filename:
            flash('No file selected.', 'danger')
            return redirect(request.url)
        if not allowed_file(f.filename):
            flash('Unsupported file type. Please upload Excel, CSV, or PDF.', 'danger')
            return redirect(request.url)

        filename = secure_filename(f.filename)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        save_path = os.path.join(app.config['UPLOAD_FOLDER'], f'{vendor_id}_{timestamp}_{filename}')
        f.save(save_path)

        try:
            rows = parse_file(save_path)
        except Exception as e:
            flash(f'Could not read file: {e}', 'danger')
            return redirect(request.url)

        if not rows:
            flash('No data found in file.', 'danger')
            return redirect(request.url)

        token = str(uuid.uuid4())
        _save_tmp(token, {
            'vendor_id': vendor_id,
            'filename': filename,
            'save_path': save_path,
            'rows': rows,
        })
        return redirect(url_for('column_picker', token=token))

    return render_template('upload.html', vendor=vendor)


# ── Upload wizard: Step 2 — pick columns ─────────────────────────────────────

@app.route('/upload/<token>/columns', methods=['GET', 'POST'])
@login_required
def column_picker(token):
    data = _load_tmp(token)
    if not data:
        flash('Upload session expired. Please upload again.', 'danger')
        return redirect(url_for('vendors'))

    vendor = db.get_or_404(Vendor, data['vendor_id'])
    rows = data['rows']
    columns = list(rows[0].keys()) if rows else []
    preview = rows[:6]

    if request.method == 'POST':
        desc_field = request.form.get('desc_field', '').strip()
        price_field = request.form.get('price_field', '').strip()
        unit_field = request.form.get('unit_field', '').strip() or None

        if not desc_field or not price_field:
            flash('Please select both a description and a price column.', 'danger')
            return redirect(request.url)

        # Extract unique items using the chosen columns
        items = []
        seen = set()
        for row in rows:
            desc = row.get(desc_field)
            price_raw = row.get(price_field)
            unit_raw = row.get(unit_field) if unit_field else None

            if not desc or not price_raw:
                continue
            price = clean_price(price_raw)
            if price is None:
                continue

            desc = str(desc).strip()
            if not desc or desc in seen:
                continue
            seen.add(desc)

            unit = str(unit_raw).strip().lower() if unit_raw else 'lb'
            items.append({'raw_description': desc, 'price': price, 'unit': unit})

        if not items:
            flash('No valid price rows found with the selected columns.', 'danger')
            return redirect(request.url)

        data.update({'desc_field': desc_field, 'price_field': price_field,
                     'unit_field': unit_field, 'items': items})
        _save_tmp(token, data)
        return redirect(url_for('cut_mapper', token=token))

    return render_template('column_picker.html',
                           vendor=vendor, token=token,
                           columns=columns, preview=preview,
                           filename=data['filename'])


# ── Upload wizard: Step 3 — map cut names ────────────────────────────────────

@app.route('/upload/<token>/mapping', methods=['GET', 'POST'])
@login_required
def cut_mapper(token):
    data = _load_tmp(token)
    if not data:
        flash('Upload session expired. Please upload again.', 'danger')
        return redirect(url_for('vendors'))

    vendor_id = data['vendor_id']
    vendor = db.get_or_404(Vendor, vendor_id)
    items = data.get('items', [])
    filename = data['filename']

    cached = {
        m.raw_description: m
        for m in CutMapping.query.filter_by(vendor_id=vendor_id).all()
    }
    unmapped = [item for item in items if item['raw_description'] not in cached]
    auto_count = len(items) - len(unmapped)
    canonical_cuts = CanonicalCut.query.order_by(CanonicalCut.category, CanonicalCut.name).all()

    if request.method == 'POST':
        cut_by_id = {c.id: c for c in CanonicalCut.query.all()}
        cut_by_name = {c.name: c for c in cut_by_id.values()}

        # Process user-supplied mappings for unmapped items
        for i, item in enumerate(unmapped):
            cut_val = request.form.get(f'cut_{i}', 'skip')
            if cut_val == 'skip' or not cut_val:
                continue

            if cut_val == 'new':
                new_name = request.form.get(f'new_name_{i}', '').strip()
                new_cat = request.form.get(f'new_cat_{i}', 'beef')
                if not new_name:
                    continue
                if new_name not in cut_by_name:
                    cut = CanonicalCut(name=new_name, category=new_cat)
                    db.session.add(cut)
                    db.session.flush()
                    cut_by_name[new_name] = cut
                cut = cut_by_name[new_name]
            else:
                cut = cut_by_id.get(int(cut_val))
                if not cut:
                    continue

            if not CutMapping.query.filter_by(raw_description=item['raw_description'],
                                               vendor_id=vendor_id).first():
                db.session.add(CutMapping(
                    raw_description=item['raw_description'],
                    canonical_cut_id=cut.id,
                    vendor_id=vendor_id,
                ))

        db.session.flush()

        # Refresh cache after new mappings are flushed
        cached = {
            m.raw_description: m
            for m in CutMapping.query.filter_by(vendor_id=vendor_id).all()
        }

        # Deactivate old sheets, create new one
        PriceSheet.query.filter_by(vendor_id=vendor_id, is_active=True).update({'is_active': False})
        sheet = PriceSheet(vendor_id=vendor_id, filename=filename)
        db.session.add(sheet)
        db.session.flush()

        saved = 0
        for item in items:
            mapping = cached.get(item['raw_description'])
            db.session.add(LineItem(
                price_sheet_id=sheet.id,
                raw_description=item['raw_description'],
                price=item['price'],
                unit=item['unit'],
                canonical_cut_id=mapping.canonical_cut_id if mapping else None,
            ))
            saved += 1

        sheet.item_count = saved
        db.session.commit()
        _clean_tmp(token)

        flash(f'Imported {saved} items from "{filename}".', 'success')
        return redirect(url_for('comparison'))

    return render_template('cut_mapper.html',
                           vendor=vendor, token=token,
                           filename=filename, unmapped=unmapped,
                           auto_count=auto_count,
                           canonical_cuts=canonical_cuts)


# ── Comparison ────────────────────────────────────────────────────────────────

@app.route('/comparison')
@login_required
def comparison():
    category_filter = request.args.get('category', 'all')
    vendors = Vendor.query.order_by(Vendor.name).all()

    active_sheets = {}
    for v in vendors:
        sheet = (PriceSheet.query
                 .filter_by(vendor_id=v.id, is_active=True)
                 .order_by(PriceSheet.uploaded_at.desc())
                 .first())
        if sheet:
            active_sheets[v.id] = sheet

    cut_query = CanonicalCut.query.order_by(CanonicalCut.category, CanonicalCut.name)
    if category_filter != 'all':
        cut_query = cut_query.filter_by(category=category_filter)
    cuts = cut_query.all()

    price_map = {}
    for v in vendors:
        sheet = active_sheets.get(v.id)
        if not sheet:
            continue
        for item in LineItem.query.filter_by(price_sheet_id=sheet.id).all():
            if item.canonical_cut_id:
                price_map.setdefault(item.canonical_cut_id, {})[v.id] = item

    cuts_with_data = [c for c in cuts if price_map.get(c.id)]
    grouped = {}
    for cut in cuts_with_data:
        grouped.setdefault(cut.category, []).append(cut)

    return render_template('comparison.html',
                           grouped=grouped, vendors=vendors,
                           price_map=price_map, active_sheets=active_sheets,
                           category_filter=category_filter)


# ── Reports ───────────────────────────────────────────────────────────────────

def _build_report_data():
    """Shared data logic for /reports and /reports/print."""
    vendors = Vendor.query.order_by(Vendor.name).all()
    active_sheets = {}
    for v in vendors:
        sheet = (PriceSheet.query
                 .filter_by(vendor_id=v.id, is_active=True)
                 .order_by(PriceSheet.uploaded_at.desc())
                 .first())
        if sheet:
            active_sheets[v.id] = sheet

    price_map = {}
    for v in vendors:
        sheet = active_sheets.get(v.id)
        if not sheet:
            continue
        for item in LineItem.query.filter_by(price_sheet_id=sheet.id).all():
            if item.canonical_cut_id:
                price_map.setdefault(item.canonical_cut_id, {})[v.id] = item

    cuts = CanonicalCut.query.order_by(CanonicalCut.category, CanonicalCut.name).all()
    vendor_map = {v.id: v for v in vendors}

    by_vendor = {}
    for cut in cuts:
        cut_prices = price_map.get(cut.id, {})
        if not cut_prices:
            continue
        best_vid = min(cut_prices, key=lambda vid: cut_prices[vid].price)
        by_vendor.setdefault(best_vid, []).append({
            "cut": cut,
            "item": cut_prices[best_vid],
        })

    sorted_vendors = sorted(by_vendor.keys(), key=lambda vid: vendor_map[vid].name)
    return by_vendor, sorted_vendors, vendor_map, active_sheets


@app.route('/reports')
@login_required
def reports():
    by_vendor, sorted_vendors, vendor_map, active_sheets = _build_report_data()
    return render_template('reports.html',
                           by_vendor=by_vendor,
                           sorted_vendors=sorted_vendors,
                           vendor_map=vendor_map,
                           active_sheets=active_sheets,
                           has_data=bool(by_vendor))


@app.route('/reports/print')
@login_required
def reports_print():
    by_vendor, sorted_vendors, vendor_map, active_sheets = _build_report_data()
    return render_template('reports_print.html',
                           by_vendor=by_vendor,
                           sorted_vendors=sorted_vendors,
                           vendor_map=vendor_map,
                           active_sheets=active_sheets,
                           has_data=bool(by_vendor),
                           generated=datetime.now())


# ── Canonical cuts ────────────────────────────────────────────────────────────

@app.route('/canonical-cuts')
@login_required
def canonical_cuts():
    cuts = CanonicalCut.query.order_by(CanonicalCut.category, CanonicalCut.name).all()
    return render_template('canonical_cuts.html', cuts=cuts)


@app.route('/canonical-cuts/add', methods=['POST'])
@login_required
def add_canonical_cut():
    name = request.form.get('name', '').strip()
    category = request.form.get('category', 'beef')
    if not name:
        flash('Cut name is required.', 'danger')
        return redirect(url_for('canonical_cuts'))
    if CanonicalCut.query.filter_by(name=name).first():
        flash(f'"{name}" already exists.', 'warning')
        return redirect(url_for('canonical_cuts'))
    db.session.add(CanonicalCut(name=name, category=category))
    db.session.commit()
    flash(f'"{name}" added.', 'success')
    return redirect(url_for('canonical_cuts'))


@app.route('/canonical-cuts/<int:cut_id>/delete', methods=['POST'])
@login_required
def delete_canonical_cut(cut_id):
    cut = db.get_or_404(CanonicalCut, cut_id)
    db.session.delete(cut)
    db.session.commit()
    flash(f'"{cut.name}" deleted.', 'success')
    return redirect(url_for('canonical_cuts'))


@app.route('/canonical-cuts/<int:cut_id>/edit', methods=['POST'])
@login_required
def edit_canonical_cut(cut_id):
    cut = db.get_or_404(CanonicalCut, cut_id)
    name = request.form.get('name', '').strip()
    category = request.form.get('category', cut.category)
    if name:
        cut.name = name
    cut.category = category
    db.session.commit()
    flash('Cut updated.', 'success')
    return redirect(url_for('canonical_cuts'))


if __name__ == '__main__':
    app.run(debug=True, port=5005)
