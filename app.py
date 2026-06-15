import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

from flask import Flask, render_template, request, redirect, url_for, flash
from werkzeug.utils import secure_filename

from extensions import db
from models import Vendor, PriceSheet, CanonicalCut, CutMapping, LineItem
from file_parser import parse_file, clean_price
from ai_matcher import identify_columns, match_cuts

ALLOWED_EXTENSIONS = {'xlsx', 'xls', 'xlsm', 'csv', 'pdf'}

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-change-in-prod')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///vendor_prices.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
db.init_app(app)

with app.app_context():
    db.create_all()


def allowed_file(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# ── Routes ──────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return redirect(url_for('comparison'))


@app.route('/vendors')
def vendors():
    all_vendors = Vendor.query.order_by(Vendor.name).all()
    return render_template('vendors.html', vendors=all_vendors)


@app.route('/vendors/add', methods=['POST'])
def add_vendor():
    name = request.form.get('name', '').strip()
    if not name:
        flash('Vendor name is required.', 'danger')
        return redirect(url_for('vendors'))
    if Vendor.query.filter_by(name=name).first():
        flash(f'Vendor "{name}" already exists.', 'warning')
        return redirect(url_for('vendors'))
    vendor = Vendor(name=name)
    db.session.add(vendor)
    db.session.commit()
    flash(f'Vendor "{name}" added.', 'success')
    return redirect(url_for('vendors'))


@app.route('/vendors/<int:vendor_id>/delete', methods=['POST'])
def delete_vendor(vendor_id):
    vendor = db.get_or_404(Vendor, vendor_id)
    db.session.delete(vendor)
    db.session.commit()
    flash(f'Vendor "{vendor.name}" deleted.', 'success')
    return redirect(url_for('vendors'))


@app.route('/vendors/<int:vendor_id>')
def vendor_detail(vendor_id):
    vendor = db.get_or_404(Vendor, vendor_id)
    sheets = PriceSheet.query.filter_by(vendor_id=vendor_id).order_by(PriceSheet.uploaded_at.desc()).all()
    return render_template('vendor_detail.html', vendor=vendor, sheets=sheets)


@app.route('/vendors/<int:vendor_id>/upload', methods=['GET', 'POST'])
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

        # Identify columns via AI
        try:
            col_map = identify_columns(rows)
        except Exception as e:
            flash(f'AI column detection failed: {e}', 'danger')
            return redirect(request.url)

        desc_field = col_map.get('description_field')
        price_field = col_map.get('price_field')
        unit_field = col_map.get('unit_field')

        if not desc_field or not price_field:
            flash('Could not identify description or price columns in this file.', 'danger')
            return redirect(request.url)

        # Extract line items
        raw_items = []
        for row in rows:
            desc = row.get(desc_field)
            price_raw = row.get(price_field)
            unit_raw = row.get(unit_field) if unit_field else None

            if not desc or not price_raw:
                continue
            price = clean_price(price_raw)
            if price is None:
                continue

            unit = str(unit_raw).strip().lower() if unit_raw else 'lb'
            raw_items.append({
                'raw_description': str(desc).strip(),
                'price': price,
                'unit': unit,
            })

        if not raw_items:
            flash('No valid price rows found in the file.', 'danger')
            return redirect(request.url)

        # Check cached mappings first
        existing_cuts = {c.name: c for c in CanonicalCut.query.all()}
        cached_maps = {
            m.raw_description: m
            for m in CutMapping.query.filter_by(vendor_id=vendor_id).all()
        }

        need_matching = [
            item['raw_description']
            for item in raw_items
            if item['raw_description'] not in cached_maps
        ]

        # AI match only un-cached descriptions
        match_lookup = {}
        if need_matching:
            existing_cut_list = [
                {'name': c.name, 'category': c.category}
                for c in existing_cuts.values()
            ]
            try:
                ai_results = match_cuts(need_matching, existing_cut_list)
            except Exception as e:
                flash(f'AI cut matching failed: {e}', 'danger')
                return redirect(request.url)

            for result in ai_results:
                match_lookup[result['raw']] = result

        # Persist new canonical cuts
        for result in match_lookup.values():
            if result['category'] == 'skip':
                continue
            cname = result['canonical']
            if cname not in existing_cuts:
                cut = CanonicalCut(name=cname, category=result['category'])
                db.session.add(cut)
                db.session.flush()
                existing_cuts[cname] = cut

        # Deactivate previous active sheets for this vendor
        PriceSheet.query.filter_by(vendor_id=vendor_id, is_active=True).update({'is_active': False})

        # Create new price sheet
        sheet = PriceSheet(vendor_id=vendor_id, filename=filename)
        db.session.add(sheet)
        db.session.flush()

        saved = 0
        for item in raw_items:
            raw_desc = item['raw_description']

            # Resolve canonical cut
            if raw_desc in cached_maps:
                mapping = cached_maps[raw_desc]
                cut = mapping.canonical_cut
            elif raw_desc in match_lookup:
                result = match_lookup[raw_desc]
                if result['category'] == 'skip':
                    continue
                cut = existing_cuts.get(result['canonical'])
                if cut:
                    # Cache this mapping
                    m = CutMapping(
                        raw_description=raw_desc,
                        canonical_cut_id=cut.id,
                        vendor_id=vendor_id,
                    )
                    db.session.add(m)
            else:
                cut = None

            li = LineItem(
                price_sheet_id=sheet.id,
                raw_description=raw_desc,
                price=item['price'],
                unit=item['unit'],
                canonical_cut_id=cut.id if cut else None,
            )
            db.session.add(li)
            saved += 1

        sheet.item_count = saved
        db.session.commit()

        flash(f'Imported {saved} items from "{filename}".', 'success')
        return redirect(url_for('comparison'))

    return render_template('upload.html', vendor=vendor)


@app.route('/vendors/<int:vendor_id>/sheets/<int:sheet_id>/activate', methods=['POST'])
def activate_sheet(vendor_id, sheet_id):
    PriceSheet.query.filter_by(vendor_id=vendor_id, is_active=True).update({'is_active': False})
    sheet = db.get_or_404(PriceSheet, sheet_id)
    sheet.is_active = True
    db.session.commit()
    flash('Price sheet activated.', 'success')
    return redirect(url_for('vendor_detail', vendor_id=vendor_id))


@app.route('/comparison')
def comparison():
    category_filter = request.args.get('category', 'all')
    vendors = Vendor.query.order_by(Vendor.name).all()

    # Most recent active price sheet per vendor
    active_sheets = {}
    for v in vendors:
        sheet = (
            PriceSheet.query
            .filter_by(vendor_id=v.id, is_active=True)
            .order_by(PriceSheet.uploaded_at.desc())
            .first()
        )
        if sheet:
            active_sheets[v.id] = sheet

    # Build {cut_id: {vendor_id: LineItem}}
    cut_query = CanonicalCut.query.order_by(CanonicalCut.category, CanonicalCut.name)
    if category_filter != 'all':
        cut_query = cut_query.filter_by(category=category_filter)
    cuts = cut_query.all()

    price_map = {}
    for v in vendors:
        sheet = active_sheets.get(v.id)
        if not sheet:
            continue
        items = LineItem.query.filter_by(price_sheet_id=sheet.id).all()
        for item in items:
            if item.canonical_cut_id:
                price_map.setdefault(item.canonical_cut_id, {})[v.id] = item

    # Filter to cuts that have data from at least one vendor
    cuts_with_data = [c for c in cuts if price_map.get(c.id)]

    # Group by category
    grouped = {}
    for cut in cuts_with_data:
        grouped.setdefault(cut.category, []).append(cut)

    return render_template(
        'comparison.html',
        grouped=grouped,
        vendors=vendors,
        price_map=price_map,
        active_sheets=active_sheets,
        category_filter=category_filter,
    )


@app.route('/canonical-cuts')
def canonical_cuts():
    cuts = CanonicalCut.query.order_by(CanonicalCut.category, CanonicalCut.name).all()
    return render_template('canonical_cuts.html', cuts=cuts)


@app.route('/canonical-cuts/add', methods=['POST'])
def add_canonical_cut():
    name = request.form.get('name', '').strip()
    category = request.form.get('category', 'beef')
    if not name:
        flash('Cut name is required.', 'danger')
        return redirect(url_for('canonical_cuts'))
    if CanonicalCut.query.filter_by(name=name).first():
        flash(f'"{name}" already exists.', 'warning')
        return redirect(url_for('canonical_cuts'))
    cut = CanonicalCut(name=name, category=category)
    db.session.add(cut)
    db.session.commit()
    flash(f'"{name}" added.', 'success')
    return redirect(url_for('canonical_cuts'))


@app.route('/canonical-cuts/<int:cut_id>/delete', methods=['POST'])
def delete_canonical_cut(cut_id):
    cut = db.get_or_404(CanonicalCut, cut_id)
    db.session.delete(cut)
    db.session.commit()
    flash(f'"{cut.name}" deleted.', 'success')
    return redirect(url_for('canonical_cuts'))


@app.route('/canonical-cuts/<int:cut_id>/edit', methods=['POST'])
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
