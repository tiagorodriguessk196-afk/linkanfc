import io
import os
import re
import secrets
import sqlite3
import time
from functools import wraps
from urllib.parse import urlparse
import qrcode
from flask import (Flask, g, jsonify, redirect, render_template_string,
                   request, send_file, send_from_directory, session)

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("DB_PATH", os.path.join(BASE, "linkanfc.db"))
NEGOCIO = os.environ.get("BUSINESS_NAME", "Meu Negócio")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
SENHA = os.environ.get("ADMIN_PASSWORD") or "Souza@2007"
if not os.environ.get("ADMIN_PASSWORD"):
    print(f"[AVISO] ADMIN_PASSWORD não definida. Senha temporária: {SENHA}")
TIPOS = {"google", "instagram", "whatsapp", "pix", "cardapio"}

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=os.environ.get("HTTPS", "0") == "1",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
)


# ---------- Banco de dados ----------
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def fechar(_):
    c = g.pop("db", None)
    if c:
        c.close()


def init_db():
    with sqlite3.connect(DB) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS placas(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            codigo TEXT UNIQUE NOT NULL,
            nome TEXT NOT NULL,
            tipo TEXT NOT NULL,
            url TEXT NOT NULL,
            cliques INTEGER NOT NULL DEFAULT 0,
            ativa INTEGER NOT NULL DEFAULT 1,
            criada TEXT DEFAULT CURRENT_TIMESTAMP)""")


init_db()


@app.after_request
def seguranca(r):
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["Referrer-Policy"] = "same-origin"
    return r


# ---------- Autenticação ----------
def auth(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get("admin"):
            return jsonify(erro="Não autenticado."), 401
        return f(*a, **k)
    return w


falhas = {}


@app.post("/api/login")
def login():
    ip = request.remote_addr
    n, t = falhas.get(ip, (0, 0))
    if time.time() - t >= 300:
        n = 0
    if n >= 5:
        return jsonify(erro="Muitas tentativas. Aguarde 5 minutos."), 429
    senha = str((request.get_json(silent=True) or {}).get("senha", ""))
    if secrets.compare_digest(senha.encode(), SENHA.encode()):
        falhas.pop(ip, None)
        session.permanent = True
        session["admin"] = True
        return jsonify(ok=True)
    falhas[ip] = (n + 1, time.time())
    return jsonify(erro="Senha incorreta."), 401


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/config")
@auth
def config():
    return jsonify(negocio=NEGOCIO, base=PUBLIC_URL)


# ---------- Validação ----------
def validar(d, criar=False):
    nome = str(d.get("nome") or "").strip()[:80]
    tipo = d.get("tipo")
    url = str(d.get("url") or "").strip()[:2000]
    if not nome or not url or tipo not in TIPOS:
        return None, "Preencha todos os campos corretamente."
    if tipo != "pix":  # PIX guarda a chave "copia e cola", não uma URL
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.netloc:
            return None, "Informe uma URL válida começando com http:// ou https://"
    out = dict(nome=nome, tipo=tipo, url=url)
    if criar:
        cod = str(d.get("codigo") or "").strip().upper().lstrip("#")
        if not re.fullmatch(r"[A-Z0-9_-]{3,30}", cod):
            return None, "Código inválido (3-30 letras, números, - ou _)."
        out["codigo"] = cod
    return out, None


# ---------- API do painel ----------
@app.get("/api/placas")
@auth
def listar():
    rows = db().execute("SELECT * FROM placas ORDER BY id DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/placas")
@auth
def criar():
    d, e = validar(request.get_json(silent=True) or {}, criar=True)
    if e:
        return jsonify(erro=e), 400
    try:
        c = db().execute(
            "INSERT INTO placas(codigo,nome,tipo,url) VALUES(?,?,?,?)",
            (d["codigo"], d["nome"], d["tipo"], d["url"]))
        db().commit()
    except sqlite3.IntegrityError:
        return jsonify(erro="Já existe uma placa com esse código."), 409
    return jsonify(id=c.lastrowid), 201


# COLE O NOVO CÓDIGO AQUI EMBAIXO (sem recuo/espaço na margem esquerda):
@app.post("/api/placas/lote")
@auth
def criar_lote():
    """Gera um lote de placas em branco (ex: PLACA01 até PLACA50) para você já deixar impresso."""
    j = request.get_json(silent=True) or {}
    prefixo = str(j.get("prefixo") or "PLACA").strip().upper()
    inicio = int(j.get("inicio", 1))
    fim = int(j.get("fim", 10))

    criadas = 0
    with db() as con:
        for i in range(inicio, fim + 1):
            codigo = f"{prefixo}{i:02d}"
            try:
                con.execute(
                    "INSERT INTO placas(codigo, nome, tipo, url, ativa) VALUES(?, ?, ?, ?, ?)",
                    (codigo, f"Não configurada ({codigo})",
                     "google", "https://google.com", 0)
                )
                criadas += 1
            except sqlite3.IntegrityError:
                pass
        con.commit()

    return jsonify(ok=True, criadas=criadas)


@app.put("/api/placas/<int:pid>")
@auth
def editar(pid):
    j = request.get_json(silent=True) or {}
    d, e = validar(j)
    if e:
        return jsonify(erro=e), 400
    c = db().execute(
        "UPDATE placas SET nome=?,tipo=?,url=?,ativa=? WHERE id=?",
        (d["nome"], d["tipo"], d["url"], 1 if j.get("ativa", True) else 0, pid))
    db().commit()
    if not c.rowcount:
        return jsonify(erro="Placa não encontrada."), 404
    return jsonify(ok=True)


@app.delete("/api/placas/<int:pid>")
@auth
def excluir(pid):
    db().execute("DELETE FROM placas WHERE id=?", (pid,))
    db().commit()
    return jsonify(ok=True)

# <--- COLE A ROTA AQUI EMBAIXO (sem espaços na margem esquerda) --->


@app.get("/api/placas/<codigo>/qrcode")
@auth
def gerar_qrcode(codigo):
    cod = codigo.upper()
    p = db().execute("SELECT codigo FROM placas WHERE codigo=?", (cod,)).fetchone()
    if not p:
        return jsonify(erro="Placa não encontrada."), 404

    base = PUBLIC_URL if PUBLIC_URL else request.host_url.rstrip("/")
    link_destino = f"{base}/r/{cod}"

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(link_destino)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    return send_file(
        buf,
        mimetype="image/png",
        as_attachment=False,
        download_name=f"qrcode_{cod}.png"
    )


# ---------- Redirecionamento público (o que o NFC/QR abre) ----------
PAGINA = """<!doctype html><html lang="pt-BR"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{{t}}</title>
<body style="font-family:system-ui;background:#0f172a;color:#e2e8f0;display:grid;place-items:center;min-height:100vh;margin:0;text-align:center;padding:1rem">
<main><h1>{{t}}</h1><p>{{m}}</p>
{% if chave %}<code id="k" style="display:block;word-break:break-all;background:#1e293b;padding:1rem;border-radius:12px;max-width:360px;margin:1rem auto">{{chave}}</code>
<button onclick="navigator.clipboard.writeText(document.getElementById('k').textContent).then(()=>this.textContent='Copiado!')" style="padding:.8rem 1.4rem;border:0;border-radius:12px;background:#4f46e5;color:#fff;font-size:1rem">Copiar chave PIX</button>{% endif %}
</main></body></html>"""


@app.get("/r/<codigo>")
def redirecionar(codigo):
    p = db().execute("SELECT * FROM placas WHERE codigo=?", (codigo.upper(),)).fetchone()
    if not p:
        return render_template_string(PAGINA, t="Placa não encontrada", m="Este código não existe no sistema."), 404

    # Se a placa está inativa ou ainda com o link padrão de não configurada
    if not p["ativa"] or (p["url"] == "https://google.com" and "Não configurada" in p["nome"]):
        return render_template_string(
            PAGINA,
            t="Placa aguardando configuração",
            m=f"A placa <code>{p['codigo']}</code> ainda não foi configurada pelo estabelecimento."
        ), 200

    if not p["ativa"]:
        return render_template_string(PAGINA, t="Placa inativa", m="Esta placa foi desativada pelo estabelecimento."), 410

    db().execute("UPDATE placas SET cliques=cliques+1 WHERE id=?", (p["id"],))
    db().commit()

    if p["tipo"] == "pix":
        r = render_template_string(
            PAGINA, t=p["nome"], m="Copie a chave e cole no app do seu banco.", chave=p["url"])
        return r, 200, {"Cache-Control": "no-store"}

    resp = redirect(p["url"], 302)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/")
def index():
    return send_from_directory(os.path.join(BASE, "public"), "index.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
