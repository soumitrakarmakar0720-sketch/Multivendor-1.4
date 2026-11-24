"""Isolated merchant workspace; never uses legacy payment credentials or tables."""
import os, secrets, hmac, re
from decimal import Decimal, InvalidOperation
from functools import wraps
from flask import Blueprint, request, session, abort, redirect, url_for, render_template, flash
from werkzeug.security import generate_password_hash, check_password_hash

def install(app, db):
    bp = Blueprint('merchant', __name__, url_prefix='/merchant')
    class Merchant(db.Model):
        __tablename__ = 'saas_merchant'
        id = db.Column(db.Integer, primary_key=True)
        slug = db.Column(db.String(64), unique=True, nullable=False)
        name = db.Column(db.String(120), nullable=False)
        email = db.Column(db.String(160), unique=True, nullable=False)
        password_hash = db.Column(db.String(256), nullable=False)
        address = db.Column(db.String(300), default='')
        registration_ref = db.Column(db.String(100), default='')
        status = db.Column(db.String(20), nullable=False, default='pending')
    class MerchantConfig(db.Model):
        __tablename__ = 'saas_merchant_config'
        merchant_id = db.Column(db.Integer, db.ForeignKey('saas_merchant.id'), primary_key=True)
        billing_type = db.Column(db.String(20), nullable=False, default='retail')
        gateway = db.Column(db.String(20), nullable=False, default='none')
        cycle = db.Column(db.String(20), nullable=False, default='monthly')
        price = db.Column(db.Numeric(12,2), nullable=False, default=0)
        paid_until = db.Column(db.Date, nullable=True)
        upi_enabled = db.Column(db.Boolean, nullable=False, default=False)
        upi_id = db.Column(db.String(120), default='')
    class Bill(db.Model):
        __tablename__ = 'saas_bill'
        id = db.Column(db.Integer, primary_key=True)
        merchant_id = db.Column(db.Integer, db.ForeignKey('saas_merchant.id'), nullable=False, index=True)
        token = db.Column(db.String(64), unique=True, nullable=False)
        customer = db.Column(db.String(120), nullable=False)
        description = db.Column(db.String(300), nullable=False)
        total = db.Column(db.Numeric(12,2), nullable=False)
        brand = db.Column(db.String(120), nullable=False)
        address = db.Column(db.String(300), default='')
    class Catalog(db.Model):
        __tablename__ = 'saas_catalog'
        id = db.Column(db.Integer, primary_key=True)
        merchant_id = db.Column(db.Integer, db.ForeignKey('saas_merchant.id'), nullable=False, index=True)
        name = db.Column(db.String(120), nullable=False)
        price = db.Column(db.Numeric(12,2), nullable=False)
        stock = db.Column(db.Integer, nullable=False, default=0)
    class DiningTable(db.Model):
        __tablename__ = 'saas_dining_table'
        id = db.Column(db.Integer, primary_key=True)
        merchant_id = db.Column(db.Integer, db.ForeignKey('saas_merchant.id'), nullable=False, index=True)
        name = db.Column(db.String(80), nullable=False)
        token = db.Column(db.String(64), unique=True, nullable=False)
        __table_args__ = (db.UniqueConstraint('merchant_id','name'),)
    class Ticket(db.Model):
        __tablename__ = 'saas_ticket'
        id = db.Column(db.Integer, primary_key=True)
        merchant_id = db.Column(db.Integer, db.ForeignKey('saas_merchant.id'), nullable=False, index=True)
        table_id = db.Column(db.Integer, db.ForeignKey('saas_dining_table.id'))
        description = db.Column(db.String(300), nullable=False)
        total = db.Column(db.Numeric(12,2), nullable=False)
        state = db.Column(db.String(20), default='open', nullable=False)
        kitchen = db.Column(db.String(20), default='new', nullable=False)
        bill_id = db.Column(db.Integer, db.ForeignKey('saas_bill.id'))
    def money(key):
        try:
            v=Decimal(field(key,30))
            if v.is_finite() and 0<v<=9999999999 and v==v.quantize(Decimal('.01')): return v
        except InvalidOperation: pass
        abort(400,'Invalid amount')
    def restaurant(m):
        c=db.session.get(MerchantConfig,m.id)
        if not c or c.billing_type!='restaurant': abort(403)
    @bp.route('/workspace/<section>', methods=['GET','POST'])
    def workspace(section):
        m=owner(); c=db.session.get(MerchantConfig,m.id)
        if section not in ('billing','invoices','branding','payments','catalog','tables','kitchen','orders','reports'): abort(404)
        if section in ('tables','kitchen','orders'): restaurant(m)
        if request.method=='POST':
            if section=='catalog':
                name=field('name',120)
                try: stock=int(field('stock',10) or '0')
                except ValueError: abort(400)
                if not name or not 0<=stock<=100000000: abort(400)
                db.session.add(Catalog(merchant_id=m.id,name=name,price=money('price'),stock=stock))
            elif section=='tables':
                name=field('name',80)
                if not name or DiningTable.query.filter_by(merchant_id=m.id,name=name).first(): abort(400)
                db.session.add(DiningTable(merchant_id=m.id,name=name,token=secrets.token_urlsafe(32)))
            elif section=='orders':
                tid=field('table_id',20); table=None
                if tid:
                    table=DiningTable.query.filter_by(merchant_id=m.id,id=tid).with_for_update().first_or_404()
                desc=field('description',300)
                if not desc: abort(400)
                db.session.add(Ticket(merchant_id=m.id,table_id=table.id if table else None,description=desc,total=money('total')))
            else: abort(405)
            db.session.commit(); flash('Saved successfully.','success'); return redirect(url_for('merchant.workspace',section=section))
        bills=Bill.query.filter_by(merchant_id=m.id).order_by(Bill.id.desc()).all()
        return page('dashboard',section=section,m=m,config=c,bills=bills,total=sum(b.total for b in bills),catalog=Catalog.query.filter_by(merchant_id=m.id).all(),tables=DiningTable.query.filter_by(merchant_id=m.id).all(),tickets=Ticket.query.filter_by(merchant_id=m.id).order_by(Ticket.id.desc()).all())
    @bp.post('/ticket/<int:tid>/ready')
    def ticket_ready(tid):
        m=owner(); restaurant(m); t=Ticket.query.filter_by(id=tid,merchant_id=m.id).with_for_update().first_or_404()
        t.kitchen='ready'; db.session.commit(); return redirect(url_for('merchant.workspace',section='kitchen'))
    @bp.post('/ticket/<int:tid>/invoice')
    def ticket_invoice(tid):
        m=owner(); restaurant(m); t=Ticket.query.filter_by(id=tid,merchant_id=m.id).with_for_update().first_or_404()
        if t.bill_id: abort(409)
        b=Bill(merchant_id=m.id,token=secrets.token_urlsafe(32),customer='Dine-in' if t.table_id else 'Parcel',description=t.description,total=t.total,brand=m.name,address=m.address)
        db.session.add(b); db.session.flush(); t.bill_id=b.id; t.state='invoiced'; db.session.commit(); return redirect(url_for('merchant.workspace',section='orders'))
    @bp.get('/table/<int:tid>/qr')
    def table_qr(tid):
        import io, qrcode
        from flask import send_file
        m=owner(); restaurant(m); t=DiningTable.query.filter_by(id=tid,merchant_id=m.id).first_or_404()
        buf=io.BytesIO(); qrcode.make(url_for('merchant.table_view',token=t.token,_external=True)).save(buf,format='PNG'); buf.seek(0)
        return send_file(buf,mimetype='image/png',as_attachment=True,download_name=f'table-{t.id}.png')
    @bp.get('/table/view/<token>')
    def table_view(token):
        t=DiningTable.query.filter_by(token=token).first_or_404(); m=db.session.get(Merchant,t.merchant_id)
        if not m or m.status!='approved': abort(404)
        return page('table_public',m=m,table=t)
    def page(kind, **kw): return render_template('merchant.html', kind=kind, **kw)
    def field(key, n): return request.form.get(key,'').strip()[:n]
    def owner():
        m = db.session.get(Merchant, session.get('merchant_id')) if session.get('merchant_id') else None
        if not m or m.status != 'approved': abort(403)
        return m
    def platform():
        if not session.get('platform_admin') or not os.environ.get('PLATFORM_ADMIN_PASSWORD_HASH'): abort(403)
        if not hmac.compare_digest(session['platform_admin'], __import__('hashlib').sha256(os.environ['PLATFORM_ADMIN_PASSWORD_HASH'].encode()).hexdigest()): abort(403)
    @bp.route('/register', methods=['GET','POST'])
    def register():
        if request.method == 'POST':
            slug, email, name = field('slug',64).lower(), field('email',160).lower(), field('name',120)
            password = request.form.get('password','')
            if not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,63}',slug) or slug=='owlstore' or not name or '@' not in email or not 12<=len(password)<=128: abort(400)
            if Merchant.query.filter((Merchant.slug==slug)|(Merchant.email==email)).first(): abort(409)
            db.session.add(Merchant(slug=slug,email=email,name=name,password_hash=generate_password_hash(password),address=field('address',300),registration_ref=field('registration_ref',100)))
            db.session.commit(); return page('pending')
        return page('register')
    @bp.route('/login', methods=['GET','POST'])
    def login():
        if request.method=='POST':
            m=Merchant.query.filter_by(email=field('email',160).lower()).first()
            if m and m.status=='approved' and check_password_hash(m.password_hash,request.form.get('password','')):
                session.clear(); session['merchant_id']=m.id; return redirect(url_for('merchant.dashboard'))
            abort(403)
        return page('login')
    @bp.route('/platform', methods=['GET','POST'])
    def admin():
        if request.method=='POST':
            p=os.environ.get('PLATFORM_ADMIN_PASSWORD_HASH','')
            if not p or not check_password_hash(p,request.form.get('password','')): abort(403)
            session.clear(); session['platform_admin']=__import__('hashlib').sha256(p.encode()).hexdigest()
            return redirect(url_for('merchant.admin'))
        if not session.get('platform_admin'): return page('platform_login')
        platform()
        return platform_page('home')
    def platform_page(section, selected=None):
        platform()
        merchants=Merchant.query.order_by(Merchant.id).all()
        invoice_count, invoice_total = db.session.query(db.func.count(Bill.id), db.func.coalesce(db.func.sum(Bill.total),0)).one()
        invoice_rows = db.session.query(Bill, Merchant.name).join(Merchant, Merchant.id==Bill.merchant_id).order_by(Bill.id.desc()).limit(100).all()
        return render_template('platform_admin.html', section=section, merchants=merchants, selected=selected, configs={c.merchant_id:c for c in MerchantConfig.query.all()}, invoice_count=invoice_count, invoice_total=invoice_total, invoice_rows=invoice_rows)
    @bp.get('/platform/<section>')
    def section_page(section):
        platform()
        if section not in ('merchants','kyc','subscriptions','gateways','customers','reports','transactions','settings','payouts','refunds'): abort(404)
        return platform_page(section)
    @bp.get('/platform/merchants/<int:mid>')
    def merchant_profile(mid):
        platform()
        return platform_page('profile', Merchant.query.get_or_404(mid))
    @bp.route('/platform/add', methods=['GET','POST'])
    def add_merchant():
        if request.method=='GET': return platform_page('add')
        platform()
        slug, email, name = field('slug',64).lower(), field('email',160).lower(), field('name',120)
        password = request.form.get('password','')
        errors = []
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,63}',slug) or slug=='owlstore': errors.append('Use a unique merchant handle of 3–64 letters, numbers or hyphens, starting with a letter or number. Example: royal-restaurant.')
        if not name: errors.append('Business name is required.')
        if not re.fullmatch(r'[^\s@]+@[^\s@]+',email): errors.append('Enter a valid email address.')
        if not 12<=len(password)<=128: errors.append('Password must contain 12–128 characters.')
        kind=field('billing_type',20)
        if kind not in ('retail','restaurant'): errors.append('Select Retail or Restaurant.')
        if errors:
            for error in errors: flash(error,'error')
            return redirect(url_for('merchant.add_merchant'))
        if Merchant.query.filter((Merchant.slug==slug)|(Merchant.email==email)).first():
            flash('Merchant email or handle already exists.','error'); return redirect(url_for('merchant.add_merchant'))
        m=Merchant(slug=slug,email=email,name=name,password_hash=generate_password_hash(password),address=field('address',300),registration_ref=field('registration_ref',100),status='pending')
        db.session.add(m); db.session.flush()
        db.session.add(MerchantConfig(merchant_id=m.id,billing_type=kind))
        db.session.commit(); flash('Merchant created. Complete verification and approve below.')
        return redirect(url_for('merchant.section_page',section='merchants'))
    @bp.post('/platform/<int:mid>/status')
    def status(mid):
        platform(); m=Merchant.query.get_or_404(mid); state=field('status',20)
        if state not in ('approved','rejected','suspended'): abort(400)
        if state=='approved' and request.form.get('reviewed')!='yes':
            flash('Confirm offline business verification before approval.','error'); return redirect(url_for('merchant.merchant_profile',mid=mid))
        m.status=state; db.session.commit(); flash('Merchant status updated.','success'); return redirect(url_for('merchant.merchant_profile',mid=mid))
    @bp.post('/platform/<int:mid>/config')
    def config(mid):
        platform(); Merchant.query.get_or_404(mid)
        from datetime import date
        kind, gateway, cycle = field('billing_type',20), field('gateway',20), field('cycle',20)
        if kind not in ('retail','restaurant') or gateway not in ('none','platform','custom') or cycle not in ('monthly','yearly'): abort(400)
        try:
            price=Decimal(field('price',30))
            if not price.is_finite() or not 0<=price<=9999999999 or price!=price.quantize(Decimal('.01')): abort(400)
            until=date.fromisoformat(field('paid_until',10)) if field('paid_until',10) else None
        except (ValueError,InvalidOperation): abort(400)
        c=db.session.get(MerchantConfig,mid) or MerchantConfig(merchant_id=mid)
        c.billing_type=kind; c.gateway=gateway; c.cycle=cycle; c.price=price; c.paid_until=until
        db.session.add(c); db.session.commit(); flash('Configuration saved.','success'); return redirect(url_for('merchant.merchant_profile',mid=mid))
    @bp.post('/upi-settings')
    def upi_settings():
        m=owner(); c=db.session.get(MerchantConfig,m.id) or MerchantConfig(merchant_id=m.id)
        vpa=field('upi_id',120); enabled=request.form.get('enabled')=='yes'
        if enabled and not re.fullmatch(r'[A-Za-z0-9._-]+@[A-Za-z0-9.-]+',vpa): abort(400)
        c.upi_id=vpa; c.upi_enabled=enabled; db.session.add(c); db.session.commit(); return redirect(url_for('merchant.dashboard'))
    @bp.get('/dashboard')
    def dashboard():
        m=owner(); bills=Bill.query.filter_by(merchant_id=m.id).order_by(Bill.id.desc()).all(); return page('dashboard',section='home',m=m,bills=bills,total=sum(b.total for b in bills), config=db.session.get(MerchantConfig,m.id))
    @bp.post('/settings')
    def settings():
        m=owner(); name=field('name',120)
        if not name: abort(400)
        m.name=name; m.address=field('address',300); db.session.commit(); return redirect(url_for('merchant.dashboard'))
    @bp.post('/bills')
    def create_bill():
        m=owner()
        try:
            amount=Decimal(field('total',30)); assert amount.is_finite() and 0<amount<=9999999999 and amount==amount.quantize(Decimal('.01'))
        except (InvalidOperation,AssertionError): abort(400)
        customer,desc=field('customer',120),field('description',300)
        if not customer or not desc: abort(400)
        db.session.add(Bill(merchant_id=m.id,token=secrets.token_urlsafe(32),customer=customer,description=desc,total=amount,brand=m.name,address=m.address)); db.session.commit(); return redirect(url_for('merchant.dashboard'))
    @bp.get('/invoice/<token>')
    def invoice(token):
        b=Bill.query.filter_by(token=token).first_or_404(); m=db.session.get(Merchant,b.merchant_id)
        if not m or m.status!='approved': abort(404)
        return page('invoice',b=b)
    @bp.post('/logout')
    def logout(): session.clear(); return redirect(url_for('merchant.login'))
    @app.before_request
    def separate_sessions():
        if (session.get('merchant_id') or session.get('platform_admin')) and request.blueprint!='merchant' and request.endpoint not in ('static','health','cashfree_webhook'): abort(403)
    @app.cli.command('upgrade-saas')
    def upgrade():
        for model in (Merchant,Bill,MerchantConfig,Catalog,DiningTable,Ticket): model.__table__.create(db.engine,checkfirst=True)
        print('Merchant tables ready; legacy tables unchanged')
    app.register_blueprint(bp)
