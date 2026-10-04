"""Sistema de controle de recebimento de cargas.

Ideia central: NUNCA guardamos "a quantidade do produto" como um número que
é sobrescrito. Guardamos cada movimentação (entrada, saída, ajuste) e o saldo
é a SOMA delas. Assim o histórico existe por construção e erros podem ser
auditados e corrigidos (estorno) sem perder o rastro.
"""
import io
import os
import sqlite3
import uuid
from datetime import datetime
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_file, session, url_for)
from openpyxl import Workbook
from werkzeug.security import check_password_hash, generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "recebimento.db")
UPLOADS = os.path.join(BASE, "static", "uploads")
EXTENSOES = {"png", "jpg", "jpeg", "webp"}

app = Flask(__name__)
app.config.update(
    # A chave assina o cookie de sessão (login). Em produção, defina SECRET_KEY no ambiente.
    SECRET_KEY=os.environ.get("SECRET_KEY", "troque-esta-chave-em-producao"),
    MAX_CONTENT_LENGTH=5 * 1024 * 1024,  # fotos de até 5 MB
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS usuarios (
    id INTEGER PRIMARY KEY, nome TEXT UNIQUE NOT NULL, senha_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS produtos (
    id INTEGER PRIMARY KEY, sku TEXT UNIQUE NOT NULL, nome TEXT NOT NULL,
    foto TEXT, estoque_minimo INTEGER NOT NULL DEFAULT 0, ativo INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS movimentacoes (
    id INTEGER PRIMARY KEY,
    produto_id INTEGER NOT NULL REFERENCES produtos(id),
    tipo TEXT NOT NULL CHECK (tipo IN ('entrada','saida','ajuste')),
    quantidade INTEGER NOT NULL,          -- com sinal: entrada +, saída -, ajuste +/-
    esperado INTEGER,                     -- quantidade da nota fiscal (só entradas)
    nota_fiscal TEXT, fornecedor TEXT, lote TEXT, validade TEXT, observacao TEXT,
    usuario_id INTEGER NOT NULL REFERENCES usuarios(id),
    criado_em TEXT NOT NULL,
    estorno_de INTEGER REFERENCES movimentacoes(id));
CREATE INDEX IF NOT EXISTS idx_mov_produto ON movimentacoes(produto_id);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row  # acessar colunas por nome: linha["nome"]
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def fechar_db(_):
    db = g.pop("db", None)
    if db:
        db.close()


def init_db():
    os.makedirs(UPLOADS, exist_ok=True)
    db = sqlite3.connect(DB)
    db.executescript(SCHEMA)
    if not db.execute("SELECT 1 FROM usuarios").fetchone():
        senha = os.environ.get("ADMIN_SENHA", "admin123")
        db.execute("INSERT INTO usuarios(nome, senha_hash) VALUES (?, ?)",
                   ("admin", generate_password_hash(senha)))
        print("Usuário inicial criado: admin /", senha, "(troque a senha!)")
    db.commit()
    db.close()


init_db()


@app.cli.command("criar-usuario")
def criar_usuario():
    """Uso: flask --app app criar-usuario  (pergunta nome e senha)"""
    import getpass
    nome = input("Nome do usuário: ").strip()
    senha = getpass.getpass("Senha: ")
    db = sqlite3.connect(DB)
    db.execute("INSERT INTO usuarios(nome, senha_hash) VALUES (?, ?)",
               (nome, generate_password_hash(senha)))
    db.commit()
    print("Usuário criado.")


def login_required(f):
    @wraps(f)
    def interna(*a, **k):
        if "uid" not in session:
            return redirect(url_for("login"))
        return f(*a, **k)
    return interna


@app.template_filter("br")
def formato_br(valor):
    """2026-10-03 14:30:00 -> 03/10/2026 14:30"""
    try:
        return datetime.strptime(valor, "%Y-%m-%d %H:%M:%S").strftime("%d/%m/%Y %H:%M")
    except (TypeError, ValueError):
        return valor or ""


def salvar_foto(arquivo):
    if not arquivo or not arquivo.filename:
        return None
    ext = arquivo.filename.rsplit(".", 1)[-1].lower()
    if ext not in EXTENSOES:
        return None
    nome = f"{uuid.uuid4().hex}.{ext}"  # nome aleatório evita colisão e nomes maliciosos
    arquivo.save(os.path.join(UPLOADS, nome))
    return nome


def saldo_do(produto_id):
    return get_db().execute(
        "SELECT COALESCE(SUM(quantidade), 0) FROM movimentacoes WHERE produto_id = ?",
        (produto_id,)).fetchone()[0]


# ---------- autenticação ----------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = get_db().execute("SELECT * FROM usuarios WHERE nome = ?",
                             (request.form["nome"].strip(),)).fetchone()
        if u and check_password_hash(u["senha_hash"], request.form["senha"]):
            session["uid"], session["nome"] = u["id"], u["nome"]
            return redirect(url_for("painel"))
        flash("Usuário ou senha incorretos.", "erro")
    return render_template("login.html")


@app.route("/sair")
def sair():
    session.clear()
    return redirect(url_for("login"))


# ---------- painel ----------
@app.route("/")
@login_required
def painel():
    q = request.args.get("q", "").strip()
    sql = """SELECT p.*, COALESCE(SUM(m.quantidade), 0) AS saldo
             FROM produtos p LEFT JOIN movimentacoes m ON m.produto_id = p.id
             WHERE p.ativo = 1"""
    params = []
    if q:
        sql += " AND (p.nome LIKE ? OR p.sku LIKE ?)"
        params += [f"%{q}%"] * 2
    sql += " GROUP BY p.id ORDER BY p.nome"
    produtos = get_db().execute(sql, params).fetchall()
    baixos = sum(1 for p in produtos if p["saldo"] <= p["estoque_minimo"])
    return render_template("painel.html", produtos=produtos, q=q, baixos=baixos)


# ---------- produtos ----------
@app.route("/produto/novo", methods=["GET", "POST"])
@app.route("/produto/<int:pid>/editar", methods=["GET", "POST"])
@login_required
def produto_form(pid=None):
    db = get_db()
    produto = db.execute("SELECT * FROM produtos WHERE id = ?", (pid,)).fetchone() if pid else None
    if pid and not produto:
        abort(404)
    if request.method == "POST":
        sku, nome = request.form["sku"].strip(), request.form["nome"].strip()
        minimo = int(request.form.get("estoque_minimo") or 0)
        foto = salvar_foto(request.files.get("foto"))
        if not sku or not nome:
            flash("Informe código (SKU) e nome.", "erro")
        else:
            try:
                if produto:
                    db.execute("UPDATE produtos SET sku=?, nome=?, estoque_minimo=?, foto=? WHERE id=?",
                               (sku, nome, minimo, foto or produto["foto"], pid))
                else:
                    db.execute("INSERT INTO produtos(sku, nome, estoque_minimo, foto) VALUES (?,?,?,?)",
                               (sku, nome, minimo, foto))
                db.commit()
                flash("Produto salvo.", "ok")
                return redirect(url_for("painel"))
            except sqlite3.IntegrityError:
                flash("Já existe um produto com esse código (SKU).", "erro")
    return render_template("produto_form.html", p=produto)


# ---------- movimentar / receber ----------
@app.route("/movimentar", methods=["GET", "POST"])
@login_required
def movimentar():
    db = get_db()
    produtos = db.execute("SELECT id, sku, nome FROM produtos WHERE ativo = 1 ORDER BY nome").fetchall()
    if request.method == "POST":
        f = request.form
        try:
            pid, qtd = int(f["produto_id"]), int(f["quantidade"])
            esperado = int(f["esperado"]) if f.get("esperado") else None
        except (ValueError, KeyError):
            flash("Confira produto e quantidades (use apenas números).", "erro")
            return redirect(url_for("movimentar"))
        tipo = f.get("tipo")
        if tipo not in ("entrada", "saida", "ajuste") or qtd == 0 or (tipo != "ajuste" and qtd < 0):
            flash("Quantidade inválida para esse tipo de movimentação.", "erro")
            return redirect(url_for("movimentar", produto=pid))
        delta = -qtd if tipo == "saida" else qtd
        if saldo_do(pid) + delta < 0:
            flash("Saldo insuficiente: essa saída deixaria o estoque negativo.", "erro")
            return redirect(url_for("movimentar", produto=pid))
        db.execute(
            """INSERT INTO movimentacoes(produto_id, tipo, quantidade, esperado, nota_fiscal,
               fornecedor, lote, validade, observacao, usuario_id, criado_em)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, tipo, delta, esperado if tipo == "entrada" else None,
             f.get("nota_fiscal", "").strip(), f.get("fornecedor", "").strip(),
             f.get("lote", "").strip(), f.get("validade") or None,
             f.get("observacao", "").strip(), session["uid"],
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        db.commit()
        if tipo == "entrada" and esperado is not None and esperado != qtd:
            flash(f"Entrada registrada com DIVERGÊNCIA: esperado {esperado}, recebido {qtd}.", "aviso")
        else:
            flash("Movimentação registrada.", "ok")
        return redirect(url_for("movimentar", produto=pid))
    return render_template("movimentar.html", produtos=produtos,
                           selecionado=request.args.get("produto", type=int))


# ---------- histórico ----------
def consulta_historico():
    """Monta o SQL com filtros. Os '?' (parâmetros) impedem SQL injection."""
    f = {k: request.args.get(k, "").strip() for k in ("produto", "tipo", "de", "ate", "nf")}
    onde, params = [], []
    if f["produto"]:
        onde.append("m.produto_id = ?"); params.append(f["produto"])
    if f["tipo"]:
        onde.append("m.tipo = ?"); params.append(f["tipo"])
    if f["de"]:
        onde.append("date(m.criado_em) >= ?"); params.append(f["de"])
    if f["ate"]:
        onde.append("date(m.criado_em) <= ?"); params.append(f["ate"])
    if f["nf"]:
        onde.append("m.nota_fiscal LIKE ?"); params.append(f"%{f['nf']}%")
    sql = """SELECT m.*, p.nome AS produto, p.sku, u.nome AS usuario,
                    EXISTS(SELECT 1 FROM movimentacoes e WHERE e.estorno_de = m.id) AS estornada
             FROM movimentacoes m JOIN produtos p ON p.id = m.produto_id
             JOIN usuarios u ON u.id = m.usuario_id"""
    if onde:
        sql += " WHERE " + " AND ".join(onde)
    return f, sql + " ORDER BY m.id DESC", params


@app.route("/historico")
@login_required
def historico():
    f, sql, params = consulta_historico()
    db = get_db()
    produtos = db.execute("SELECT id, nome FROM produtos ORDER BY nome").fetchall()
    return render_template("historico.html", f=f, produtos=produtos,
                           movs=db.execute(sql + " LIMIT 300", params).fetchall())


@app.route("/estornar/<int:mid>", methods=["POST"])
@login_required
def estornar(mid):
    """Corrige um lançamento errado criando o lançamento oposto (nada é apagado)."""
    db = get_db()
    m = db.execute("SELECT * FROM movimentacoes WHERE id = ?", (mid,)).fetchone()
    if not m or m["estorno_de"] or db.execute(
            "SELECT 1 FROM movimentacoes WHERE estorno_de = ?", (mid,)).fetchone():
        flash("Esse lançamento não pode ser estornado.", "erro")
    elif saldo_do(m["produto_id"]) - m["quantidade"] < 0:
        flash("Estorno deixaria o estoque negativo.", "erro")
    else:
        db.execute(
            """INSERT INTO movimentacoes(produto_id, tipo, quantidade, observacao,
               usuario_id, criado_em, estorno_de) VALUES (?, 'ajuste', ?, ?, ?, ?, ?)""",
            (m["produto_id"], -m["quantidade"], f"Estorno do lançamento #{mid}",
             session["uid"], datetime.now().strftime("%Y-%m-%d %H:%M:%S"), mid))
        db.commit()
        flash(f"Lançamento #{mid} estornado.", "ok")
    return redirect(request.referrer or url_for("historico"))


@app.route("/exportar")
@login_required
def exportar():
    f, sql, params = consulta_historico()
    wb = Workbook()
    ws = wb.active
    ws.title = "Histórico"
    ws.append(["#", "Data", "Produto", "SKU", "Tipo", "Quantidade", "Esperado (NF)",
               "Nota fiscal", "Fornecedor", "Lote", "Validade", "Observação", "Usuário"])
    for m in get_db().execute(sql, params):
        ws.append([m["id"], formato_br(m["criado_em"]), m["produto"], m["sku"], m["tipo"],
                   m["quantidade"], m["esperado"], m["nota_fiscal"], m["fornecedor"],
                   m["lote"], m["validade"], m["observacao"], m["usuario"]])
    ws2 = wb.create_sheet("Saldo atual")
    ws2.append(["SKU", "Produto", "Saldo", "Estoque mínimo"])
    for p in get_db().execute(
            """SELECT p.sku, p.nome, COALESCE(SUM(m.quantidade),0) s, p.estoque_minimo
               FROM produtos p LEFT JOIN movimentacoes m ON m.produto_id = p.id
               WHERE p.ativo = 1 GROUP BY p.id ORDER BY p.nome"""):
        ws2.append(list(p))
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="recebimento.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


if __name__ == "__main__":
    # host 0.0.0.0 deixa o celular (na mesma rede Wi-Fi) acessar pelo IP do computador
    app.run(host="0.0.0.0", port=5000, debug=True)
