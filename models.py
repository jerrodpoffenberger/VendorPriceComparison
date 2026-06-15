from datetime import datetime
from extensions import db


class Vendor(db.Model):
    __tablename__ = 'vendors'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    price_sheets = db.relationship('PriceSheet', back_populates='vendor', lazy='dynamic')


class PriceSheet(db.Model):
    __tablename__ = 'price_sheets'
    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey('vendors.id'), nullable=False)
    filename = db.Column(db.String(255))
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_active = db.Column(db.Boolean, default=True)
    item_count = db.Column(db.Integer, default=0)
    vendor = db.relationship('Vendor', back_populates='price_sheets')
    line_items = db.relationship('LineItem', back_populates='price_sheet', lazy='dynamic')


class CanonicalCut(db.Model):
    __tablename__ = 'canonical_cuts'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, unique=True)
    category = db.Column(db.String(20), default='beef')  # beef, pork, other
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    line_items = db.relationship('LineItem', back_populates='canonical_cut')


class CutMapping(db.Model):
    __tablename__ = 'cut_mappings'
    id = db.Column(db.Integer, primary_key=True)
    raw_description = db.Column(db.String(500), nullable=False)
    canonical_cut_id = db.Column(db.Integer, db.ForeignKey('canonical_cuts.id'))
    vendor_id = db.Column(db.Integer, db.ForeignKey('vendors.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    canonical_cut = db.relationship('CanonicalCut')
    __table_args__ = (
        db.UniqueConstraint('raw_description', 'vendor_id', name='uq_mapping'),
    )


class LineItem(db.Model):
    __tablename__ = 'line_items'
    id = db.Column(db.Integer, primary_key=True)
    price_sheet_id = db.Column(db.Integer, db.ForeignKey('price_sheets.id'), nullable=False)
    raw_description = db.Column(db.String(500))
    price = db.Column(db.Float)
    unit = db.Column(db.String(50), default='lb')
    canonical_cut_id = db.Column(db.Integer, db.ForeignKey('canonical_cuts.id'), nullable=True)
    price_sheet = db.relationship('PriceSheet', back_populates='line_items')
    canonical_cut = db.relationship('CanonicalCut', back_populates='line_items')
