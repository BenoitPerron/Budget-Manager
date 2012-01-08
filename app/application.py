from flask import abort, flash, Flask, g, Markup, redirect, render_template, request, url_for
from flask.ext import admin
from importer import FieldMappingForm, FileUploadForm, load_from_file, prepare_data, import_data
from itertools import islice
from models import db
from models import Account, Category, TransactionType, Transaction, TransactionTag
from os import path, remove
from re import compile, IGNORECASE
from server.database import connect_db, query_db
from sqlalchemy import or_
from werkzeug import secure_filename
from wtforms.ext.sqlalchemy.fields import QuerySelectField
from wtforms.fields import SelectField
from wtforms.validators import Required

MANAGED_MODELS = [Account, Category, TransactionType, Transaction, TransactionTag]
MANAGED_MODEL_MAP = dict(account=Account,
                            category=Category,
                            transactiontype=TransactionType,
                            transaction=Transaction,
                            transactiontag=TransactionTag)
VALID_TABLENAME = compile("^[a-z]+[a-z0-9_]$", IGNORECASE)


def create_app(config_path=None, name=None):
    """Creates a flask application instance and loads its config from config_path

    :param config_path: A string representing either a file path or an import path
    :returns: An instance of `flask.Flask`."""

    app = Flask(name or __name__)
    app.config.from_object('server.config.DebugConfig')
    app.config.from_envvar(config_path or '', silent=True)

    db.init_app(app)

    admin_blueprint = admin.create_admin_blueprint(MANAGED_MODELS, db.session)

    app.register_blueprint(admin_blueprint, url_prefix="/tables")

    configure_routes(app, db)
    configure_request_details(app)

    return app


def configure_request_details(app):
    @app.before_request
    def before_request():
        g.db = connect_db(app)

    @app.teardown_request
    def teardown_request(exception):
        g.db.close()

    return app


def configure_routes(app, db):

    @app.route("/")
    def index():
        results = query_db("""
            SELECT name
                , tbl_name
            FROM sqlite_master
            WHERE name NOT LIKE 'sqlite%'
            AND type = 'table'""")

        li = Markup("""<li><a href="{}">{}</a></li>""")
        results = [li.format(url_for("view_table", table_name=tbl_name), name) for name, tbl_name in islice(results, 1, None)]

        results = Markup("<ul>") + Markup("\n").join(results) + Markup("</ul>")

        return render_template("layout.html", content=results)

    @app.route("/search/<model>", methods=["GET", "POST"])
    @app.route("/search", methods=["GET", "POST"])
    def search(model=None):
        query = request.form.get("query", None) or request.args.get("query", None)
        results = []
        if not query:
            return render_template("search.html")

        def generate_search_terms(model):
            search_terms = []
            for term in ["name", "description", "amount", "transactionDate"]:
                if hasattr(model, term):
                    search_terms.append(term)
            return [getattr(model, term).like(query) for term in search_terms]

        flash("""Searching for "{}" ...""".format(query))
        if query.count("%") == 0:
            query = "%{}%".format(query)
        if model is None:
            for model_name, model in MANAGED_MODEL_MAP.items():
                search_terms = generate_search_terms(model)
                results.append((model_name, db.session.query(model).filter(or_(*search_terms)).all()))
            return render_template("search.html", results=results)
        else:
            if not model.lower() in MANAGED_MODEL_MAP:
                return abort(404)
            flash("""Searching for "{}" in {} ...""".format(query, model))
            model_ = MANAGED_MODEL_MAP.get(model)
            search_terms = generate_search_terms(model_)
            results = [(model, db.session.query(model_).filter(or_(*search_terms)))]
            return render_template("search.html", results=results)

    @app.route("/import", methods=["GET", "POST"])
    def provide_file():
        form = FileUploadForm(request.form)

        if request.method == "POST" and form.validate():
            f = request.files["file"]
            filename = secure_filename(f.filename)
            if filename is None or not filename:
                flash("Please provide a file", category="error")
            else:
                f.save(path.join(app.config["UPLOAD_FOLDER"], filename))
                return redirect(url_for("data_mapping", filename=filename, mode=form.mode.data))

        return render_template("edit_layout.html", form=form, title="Import Data")

    @app.route("/import/<path:filename>", methods=["GET", "POST"])
    def data_mapping(filename=None):
        filepath = path.join(app.config["UPLOAD_FOLDER"], filename)

        try:
            data_file = open(filepath, "r")
        except IOError:
            return abort(404)

        data_file = load_from_file(data_file)
        headers = next(data_file)
        lowercase_headers = [h.lower() for h in headers]
        headers_ = [""] + headers
        header_list = zip(headers_, headers_)
        table_columns = Transaction.__table__.columns
        mode = request.args.get("mode", None)

        class NewForm(FieldMappingForm):
            pass

        for field in table_columns:
            if not field.primary_key:
                i = lowercase_headers.index(field.name.lower()) if field.name.lower() in lowercase_headers else -1
                default_ = headers[i] if i >= 0 else None
                NewForm = _generate_field(NewForm, field, choices=header_list, default=default_, mode=mode)

        form = NewForm(request.form)

        if request.method == "POST" and form.validate():
            stream = prepare_data(data_file,
                                    mode,
                                    form["spend_transactions"].data,
                                    form["income_transactions"].data,
                                    form["accountID"].data)
            total_imported = import_data(stream)
            flash("{} records have been imported".format(total_imported))

            data_file.close()
            remove(filepath)

            return redirect(url_for('admin.list', model_name='Transaction'))

        return render_template("edit_layout.html", form=form, title="Map Fields")

    @app.route("/<table_name>")
    def view_table(table_name=None):
        if table_name is None or not VALID_TABLENAME.findall(table_name):
            return abort(404)
        else:

            results = []

            try:
                results = query_db("SELECT * FROM {}".format(table_name))
                results = list(results)
            except Exception:
                abort(404)

            if len(results) < 2:
                flash("The table contains no data")

            return render_template("report_layout.html",
                                    results=results,
                                    title="Table View for {}".format(table_name))

    return app


def _generate_field(form, db_field, choices=None, default=None, mode=None):
    if db_field.name.lower() == "transactiontypeid":
        income_transactions = lambda val: lambda: TransactionType.query.filter(TransactionType.isIncome == val)
        income_field = QuerySelectField("Map income values to", query_factory=income_transactions(1), allow_blank=True, validators=[Required()])
        spend_field = QuerySelectField("Map spending values to", query_factory=income_transactions(0), allow_blank=True, validators=[Required()])
        setattr(form, "income_transactions", income_field)
        setattr(form, "spend_transactions", spend_field)
        return form
    elif db_field.name.lower() == "amount" and mode == "seperate":
        income_column = SelectField("Field containing income values", choices=choices, default=default)
        spend_column = SelectField("Field containing spending values", choices=choices, default=default)
        setattr(form, "income_column", income_column)
        setattr(form, "spend_column", spend_column)
        return form
    elif db_field.name.lower() == "accountid":
        field = QuerySelectField("Account", query_factory=lambda: Account.query, allow_blank=True, validators=[Required("Please select an account")])
    else:
        field = SelectField(db_field.name, choices=choices, default=default)

    setattr(form, db_field.name, field)
    return form
