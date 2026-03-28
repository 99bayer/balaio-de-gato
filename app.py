from flask import Flask, request, jsonify, send_from_directory, render_template_string
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import os, json, requests, secrets

app = Flask(__name__, static_folder='static')
app.secret_key = os.environ.get("SECRET_KEY", "carimbo-troque-em-producao")

# ── Banco ───────────────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///carimbo.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+pg8000://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+pg8000://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# Mercado Pago
MP_ACCESS_TOKEN = os.environ.get("MP_ACCESS_TOKEN", "")
MP_PUBLIC_KEY   = os.environ.get("MP_PUBLIC_KEY", "")
MP_API          = "https://api.mercadopago.com"

# E-mail notificação
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_PASS = os.environ.get("GMAIL_PASS", "")
EMAIL_LOJA = os.environ.get("EMAIL_LOJA", "")  # e-mail do seu pai para receber pedidos

# ── Models ──────────────────────────────────────────────────────────────────
class Pedido(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    numero       = db.Column(db.String(20), unique=True, nullable=False)
    status       = db.Column(db.String(20), default="pendente")
    # Carimbo
    tamanho      = db.Column(db.String(20))
    categoria    = db.Column(db.String(20))
    layout       = db.Column(db.String(20))
    textos       = db.Column(db.Text)   # JSON
    preco_prod   = db.Column(db.Float)
    # Entrega
    frete_nome   = db.Column(db.String(50))
    frete_preco  = db.Column(db.Float)
    end_nome     = db.Column(db.String(100))
    end_email    = db.Column(db.String(150))
    end_tel      = db.Column(db.String(20))
    end_cep      = db.Column(db.String(10))
    end_rua      = db.Column(db.String(150))
    end_comp     = db.Column(db.String(100))
    end_bairro   = db.Column(db.String(100))
    end_cidade   = db.Column(db.String(100))
    # Pagamento
    pagamento    = db.Column(db.String(20))
    mp_pref_id   = db.Column(db.String(100))
    mp_payment_id= db.Column(db.String(100))
    total              = db.Column(db.Float)
    tinta              = db.Column(db.Boolean, default=False)
    whatsapp_cliente   = db.Column(db.String(20), nullable=True)
    itens_json         = db.Column(db.Text, nullable=True)
    criado_em    = db.Column(db.DateTime, default=datetime.utcnow)

# ── Página principal ────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

# ── Criar pedido + preferência Mercado Pago ─────────────────────────────────
@app.route("/api/pedido", methods=["POST"])
def criar_pedido():
    data = request.get_json() or {}

    # Validar campos obrigatórios
    required = ["frete_nome","frete_preco","end_nome","end_email","end_rua","end_cidade","preco_prod"]
    for field in required:
        if not data.get(field) and data.get(field) != 0:
            return jsonify({"erro": f"Campo obrigatório: {field}"}), 400

    # Gerar número do pedido
    numero = "BG" + datetime.now().strftime("%y%m%d") + secrets.token_hex(2).upper()
    total  = float(data["preco_prod"]) + float(data["frete_preco"])

    # Suporte a múltiplos itens (carrinho)
    itens = data.get("itens", [])
    if itens:
        textos_str = " | ".join([
            f"[{it.get('tamanho')}mm] " + " / ".join(t for t in it.get("textos",[]) if t.strip())
            for it in itens
        ])
        textos = [t for it in itens for t in it.get("textos",[]) if t.strip()]
        tamanho = itens[0].get("tamanho","") if itens else ""
        categoria = itens[0].get("categoria","") if itens else ""
        layout = itens[0].get("layout","") if itens else ""
        tinta = any(it.get("tinta") for it in itens)
    else:
        textos = data.get("textos", [])
        textos_str = " | ".join(textos) if isinstance(textos, list) else textos
        tamanho = data.get("tamanho","")
        categoria = data.get("categoria","")
        layout = data.get("layout","")
        tinta = bool(data.get("tinta", False))

    # Criar pedido no banco
    pedido = Pedido(
        numero      = numero,
        tamanho     = tamanho,
        categoria   = categoria,
        layout      = layout,
        textos      = json.dumps(textos, ensure_ascii=False),
        preco_prod  = float(data["preco_prod"]),
        frete_nome  = data.get("frete_nome"),
        frete_preco = float(data["frete_preco"]),
        end_nome    = data.get("end_nome"),
        end_email   = data.get("end_email"),
        end_tel     = data.get("end_tel",""),
        end_cep     = data.get("end_cep",""),
        end_rua     = data.get("end_rua"),
        end_comp    = data.get("end_comp",""),
        end_bairro  = data.get("end_bairro",""),
        end_cidade  = data.get("end_cidade"),
        pagamento   = data.get("pagamento","mercadopago"),
        tinta       = tinta,
        total       = total,
    )
    # Campos novos — só atribui se a coluna existir (migration pode não ter rodado)
    try:
        pedido.itens_json       = json.dumps(itens, ensure_ascii=False)
        pedido.whatsapp_cliente = data.get("whatsapp_cliente","")
    except Exception:
        pass
    db.session.add(pedido)
    db.session.commit()

    # Criar preferência no Mercado Pago
    base_url = request.host_url.rstrip("/")
    pref_data = {
        "items": [{
            "title": f"{len(itens) if itens else 1} Carimbo(s) — Balaio de Gato",
            "quantity": 1,
            "unit_price": float(total),
            "currency_id": "BRL",
        }],
        "payer": {
            "name":  data.get("end_nome",""),
            "email": data.get("end_email",""),
            "phone": {"number": data.get("end_tel","").replace("(","").replace(")","").replace(" ","").replace("-","")},
            "address": {
                "street_name": data.get("end_rua",""),
                "zip_code":    data.get("end_cep","").replace("-",""),
            }
        },
        "back_urls": {
            "success": f"{base_url}/pedido/sucesso/{numero}",
            "failure": f"{base_url}/pedido/falha/{numero}",
            "pending": f"{base_url}/pedido/pendente/{numero}",
        },
        "auto_return": "approved",
        "external_reference": numero,
        "notification_url": f"{base_url}/webhook/mercadopago",
        "statement_descriptor": "FACA SEU CARIMBO",
        "payment_methods": {
            "excluded_payment_types": [],
            "installments": 1,
        },
    }

    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type":  "application/json",
        "X-Idempotency-Key": numero,
    }

    try:
        resp = requests.post(f"{MP_API}/checkout/preferences",
                             json=pref_data, headers=headers, timeout=10)
        resp_data = resp.json()

        if resp.status_code != 201:
            return jsonify({"erro": "Erro ao criar preferência MP",
                            "detalhe": resp_data}), 500

        pref_id  = resp_data["id"]
        init_url = resp_data["init_point"]  # URL de pagamento real

        pedido.mp_pref_id = pref_id
        db.session.commit()

        return jsonify({
            "ok": True,
            "numero": numero,
            "pref_id": pref_id,
            "checkout_url": init_url,
            "total": total,
        })

    except Exception as e:
        return jsonify({"erro": str(e)}), 500

# ── Webhook Mercado Pago ─────────────────────────────────────────────────────
@app.route("/webhook/mercadopago", methods=["POST"])
def webhook_mp():
    data  = request.get_json(force=True) or {}
    topic = data.get("type") or request.args.get("topic","")
    pid   = data.get("data",{}).get("id") or request.args.get("id","")

    if topic not in ("payment","merchant_order"):
        return jsonify({"ok": True}), 200

    try:
        headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
        resp = requests.get(f"{MP_API}/v1/payments/{pid}",
                            headers=headers, timeout=8)
        pay  = resp.json()
        ext_ref = pay.get("external_reference","")
        status  = pay.get("status","")

        pedido = Pedido.query.filter_by(numero=ext_ref).first()
        if not pedido:
            return jsonify({"ok": True}), 200

        pedido.mp_payment_id = str(pid)

        if status == "approved":
            pedido.status = "pago"
            db.session.commit()
            enviar_emails(pedido)
        elif status in ("rejected","cancelled"):
            pedido.status = "cancelado"
            db.session.commit()
        else:
            pedido.status = "pendente"
            db.session.commit()

    except Exception as e:
        print(f"Webhook MP erro: {e}")

    return jsonify({"ok": True}), 200

# ── Páginas de retorno ───────────────────────────────────────────────────────
@app.route("/pedido/sucesso/<numero>")
def pedido_sucesso(numero):
    pedido = Pedido.query.filter_by(numero=numero).first()
    if pedido and pedido.status != "pago":
        pedido.status = "pago"
        db.session.commit()
        enviar_emails(pedido)
    return send_from_directory("static", "sucesso.html")

@app.route("/pedido/falha/<numero>")
def pedido_falha(numero):
    return send_from_directory("static", "falha.html")

@app.route("/pedido/pendente/<numero>")
def pedido_pendente(numero):
    return send_from_directory("static", "pendente.html")

# ── Status do pedido (consulta) ──────────────────────────────────────────────
@app.route("/api/pedido/<numero>")
def status_pedido(numero):
    p = Pedido.query.filter_by(numero=numero).first()
    if not p: return jsonify({"erro":"não encontrado"}), 404
    return jsonify({"numero":p.numero,"status":p.status,"total":p.total})

# ── Public Key para o frontend ───────────────────────────────────────────────
@app.route("/api/config")
def config():
    return jsonify({"public_key": MP_PUBLIC_KEY})

# ── E-mails ──────────────────────────────────────────────────────────────────
def enviar_emails(pedido):
    textos = json.loads(pedido.textos or "[]")
    txt_str = "<br>".join(textos) if isinstance(textos,list) else textos
    brl = lambda v: f"R$ {v:.2f}".replace(".",",")

    # E-mail para o cliente
    if pedido.end_email:
        html_cliente = f"""
<div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;padding:28px">
  <h2 style="color:#1B3A5C">✅ Pedido confirmado!</h2>
  <p>Olá, <strong>{pedido.end_nome}</strong>! Seu pedido foi recebido e está em produção.</p>
  <div style="background:#F5F0E8;border-radius:10px;padding:16px;margin:16px 0">
    <div style="font-size:.8rem;color:#9A8870;margin-bottom:8px">PEDIDO Nº {pedido.numero}</div>
    <div><strong>Carimbo {pedido.tamanho}mm</strong></div>
    <div style="font-family:monospace;margin:8px 0;color:#1B3A5C">{txt_str}</div>
    <div style="margin-top:8px;font-size:.85rem;color:#5A5040">
      Frete: {pedido.frete_nome}<br>
      Total pago: <strong>{brl(pedido.total)}</strong>
    </div>
  </div>
  <p style="font-size:.85rem;color:#5A5040">
    Prazo de produção: <strong>3 a 5 dias úteis</strong> após confirmação.<br>
    Dúvidas? WhatsApp: <strong>(43) 99690-5591</strong>
  </p>
  <p style="font-size:.8rem;color:#9A8870">Faça seu Carimbo — Jandaia do Sul/PR</p>
</div>"""
        _enviar_email(pedido.end_email,
                      f"Pedido {pedido.numero} confirmado — Faça seu Carimbo",
                      html_cliente)

    # E-mail para a loja
    if EMAIL_LOJA:
        html_loja = f"""
<div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;padding:28px">
  <h2 style="color:#C0392B">🛎 Novo pedido recebido!</h2>
  <table style="width:100%;font-size:.9rem;border-collapse:collapse">
    <tr><td style="padding:6px 0;color:#9A8870">Pedido</td><td><strong>{pedido.numero}</strong></td></tr>
    <tr><td style="padding:6px 0;color:#9A8870">Carimbo</td><td>{pedido.tamanho}mm — {pedido.categoria}</td></tr>
    <tr><td style="padding:6px 0;color:#9A8870">Texto</td><td style="font-family:monospace">{txt_str}</td></tr>
    <tr><td style="padding:6px 0;color:#9A8870">Cliente</td><td>{pedido.end_nome}</td></tr>
    <tr><td style="padding:6px 0;color:#9A8870">E-mail</td><td>{pedido.end_email}</td></tr>
    <tr><td style="padding:6px 0;color:#9A8870">Telefone</td><td>{pedido.end_tel}</td></tr>
    <tr><td style="padding:6px 0;color:#9A8870">Endereço</td>
      <td>{pedido.end_rua}, {pedido.end_comp}<br>{pedido.end_bairro}<br>{pedido.end_cidade} — CEP {pedido.end_cep}</td></tr>
    <tr><td style="padding:6px 0;color:#9A8870">Frete</td><td>{pedido.frete_nome} — {brl(pedido.frete_preco)}</td></tr>
    <tr><td style="padding:6px 0;color:#9A8870">Total</td><td><strong style="color:#1B3A5C">{brl(pedido.total)}</strong></td></tr>
    <tr><td style="padding:6px 0;color:#9A8870">Pagamento</td><td style="color:#27AE60"><strong>✅ APROVADO</strong></td></tr>
  </table>
</div>"""
        _enviar_email(EMAIL_LOJA,
                      f"[NOVO PEDIDO] {pedido.numero} — {pedido.tamanho}mm — {brl(pedido.total)}",
                      html_loja)

def _enviar_email(dest, subject, html):
    if not GMAIL_USER or not GMAIL_PASS:
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"Faça seu Carimbo <{GMAIL_USER}>"
        msg["To"]      = dest
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_PASS)
            s.sendmail(GMAIL_USER, dest, msg.as_string())
    except Exception as e:
        print(f"Erro e-mail: {e}")

# ── ADMIN ───────────────────────────────────────────────────────────────────
@app.route("/admin")
def admin_login_page():
    return send_from_directory("static", "admin.html")

@app.route("/admin/login", methods=["POST"])
def admin_login():
    data = request.get_json() or {}
    admin_key = os.environ.get("ADMIN_KEY","")
    if not admin_key or data.get("senha") != admin_key:
        return jsonify({"erro":"Senha incorreta"}), 401
    from flask import session
    session["admin"] = True
    return jsonify({"ok": True})

@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    from flask import session
    session.pop("admin", None)
    return jsonify({"ok": True})

@app.route("/api/admin/pedidos")
def admin_pedidos():
    from flask import session
    if not session.get("admin"):
        return jsonify({"erro":"não autorizado"}), 401
    
    status_filtro = request.args.get("status","")
    q = Pedido.query.order_by(Pedido.criado_em.desc())
    if status_filtro:
        q = q.filter_by(status=status_filtro)
    
    pedidos = []
    for p in q.limit(100).all():
        textos = json.loads(p.textos or "[]")
        pedidos.append({
            "id": p.id,
            "numero": p.numero,
            "status": p.status,
            "tamanho": p.tamanho,
            "categoria": p.categoria,
            "textos": textos,
            "tinta": getattr(p, 'tinta', False),
            "end_nome": p.end_nome,
            "end_email": p.end_email,
            "end_tel": p.end_tel,
            "end_cep": p.end_cep,
            "end_rua": p.end_rua,
            "end_comp": p.end_comp,
            "end_bairro": p.end_bairro,
            "end_cidade": p.end_cidade,
            "whatsapp_cliente": p.whatsapp_cliente or "",
            "frete_nome": p.frete_nome,
            "frete_preco": p.frete_preco,
            "total": p.total,
            "pagamento": p.pagamento,
            "criado_em": p.criado_em.strftime("%d/%m/%Y %H:%M") if p.criado_em else "",
        })
    
    resumo = {
        "total": Pedido.query.count(),
        "pagos": Pedido.query.filter_by(status="pago").count(),
        "pendentes": Pedido.query.filter_by(status="pendente").count(),
        "faturamento": sum(p.total or 0 for p in Pedido.query.filter_by(status="pago").all()),
    }
    
    return jsonify({"pedidos": pedidos, "resumo": resumo})

@app.route("/api/admin/pedido/<numero>/status", methods=["POST"])
def admin_atualizar_status(numero):
    from flask import session
    if not session.get("admin"):
        return jsonify({"erro":"não autorizado"}), 401
    data = request.get_json() or {}
    p = Pedido.query.filter_by(numero=numero).first()
    if not p:
        return jsonify({"erro":"não encontrado"}), 404
    novo_status = data.get("status", p.status)
    # Status válidos para carimbos
    validos = ["novo","confeccao","enviado","finalizado","cancelado","pago","pendente"]
    if novo_status in validos:
        p.status = novo_status
        db.session.commit()
    return jsonify({"ok": True})


# ── MODEL MÓVEIS ────────────────────────────────────────────────────────────
class Movel(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    nome        = db.Column(db.String(150), nullable=False)
    descricao   = db.Column(db.Text, nullable=True)
    preco       = db.Column(db.Float, nullable=False)
    condicao    = db.Column(db.String(10), default="usado")  # novo/usado
    fotos_json  = db.Column(db.Text, nullable=True)  # JSON array de base64
    vendido     = db.Column(db.Boolean, default=False)
    criado_em   = db.Column(db.DateTime, default=datetime.utcnow)

# ── CATÁLOGO PÚBLICO ────────────────────────────────────────────────────────
@app.route("/moveis")
def catalogo_moveis():
    return send_from_directory("static", "moveis.html")

@app.route("/api/moveis")
def api_moveis():
    condicao = request.args.get("condicao", "")
    q = Movel.query.filter_by(vendido=False).order_by(Movel.criado_em.desc())
    if condicao in ("novo", "usado"):
        q = q.filter_by(condicao=condicao)
    moveis = []
    for m in q.all():
        # fotos_json pode não existir ainda — tratar com segurança
        try:
            fotos = json.loads(m.fotos_json or "[]")
        except Exception:
            fotos = []
        # compatibilidade com coluna foto_base64 antiga
        if not fotos:
            try:
                old_foto = getattr(m, "foto_base64", None)
                if old_foto:
                    fotos = [old_foto]
            except Exception:
                pass
        moveis.append({
            "id": m.id,
            "nome": m.nome,
            "descricao": m.descricao or "",
            "preco": m.preco,
            "condicao": m.condicao,
            "fotos": fotos,
            "foto": fotos[0] if fotos else "",
            "criado_em": m.criado_em.strftime("%d/%m/%Y") if m.criado_em else "",
        })
    return jsonify({"moveis": moveis, "total": len(moveis)})

# ── ADMIN MÓVEIS ─────────────────────────────────────────────────────────────
@app.route("/api/admin/moveis", methods=["GET"])
def admin_moveis_list():
    from flask import session
    if not session.get("admin"):
        return jsonify({"erro": "não autorizado"}), 401
    moveis = Movel.query.order_by(Movel.criado_em.desc()).all()
    result = []
    for m in moveis:
        try:
            fotos = json.loads(m.fotos_json or "[]")
        except Exception:
            fotos = []
        if not fotos:
            try:
                old_foto = getattr(m, "foto_base64", None)
                if old_foto: fotos = [old_foto]
            except Exception:
                pass
        result.append({
            "id": m.id, "nome": m.nome, "preco": m.preco,
            "condicao": m.condicao, "vendido": m.vendido,
            "fotos": fotos,
            "criado_em": m.criado_em.strftime("%d/%m/%Y") if m.criado_em else "",
        })
    return jsonify({"moveis": result})

@app.route("/api/admin/moveis", methods=["POST"])
def admin_movel_criar():
    from flask import session
    if not session.get("admin"):
        return jsonify({"erro": "não autorizado"}), 401
    data = request.get_json() or {}
    if not data.get("nome") or not data.get("preco"):
        return jsonify({"erro": "nome e preco obrigatórios"}), 400
    fotos = data.get("fotos", [])
    if data.get("foto") and not fotos:
        fotos = [data["foto"]]
    m = Movel(
        nome      = data["nome"].strip(),
        descricao = data.get("descricao","").strip(),
        preco     = float(data["preco"]),
        condicao  = data.get("condicao","usado"),
        fotos_json = json.dumps(fotos, ensure_ascii=False),
    )
    db.session.add(m)
    db.session.commit()
    return jsonify({"ok": True, "id": m.id})

@app.route("/api/admin/moveis/<int:mid>", methods=["POST"])
def admin_movel_editar(mid):
    from flask import session
    if not session.get("admin"):
        return jsonify({"erro": "não autorizado"}), 401
    m = Movel.query.get_or_404(mid)
    data = request.get_json() or {}
    if "nome"      in data: m.nome      = data["nome"].strip()
    if "descricao" in data: m.descricao = data["descricao"].strip()
    if "preco"     in data: m.preco     = float(data["preco"])
    if "condicao"  in data: m.condicao  = data["condicao"]
    if "vendido"   in data: m.vendido   = bool(data["vendido"])
    if "fotos"     in data: m.fotos_json  = json.dumps(data["fotos"], ensure_ascii=False)
    db.session.commit()
    return jsonify({"ok": True})

@app.route("/api/admin/moveis/<int:mid>", methods=["DELETE"])
def admin_movel_deletar(mid):
    from flask import session
    if not session.get("admin"):
        return jsonify({"erro": "não autorizado"}), 401
    m = Movel.query.get_or_404(mid)
    db.session.delete(m)
    db.session.commit()
    return jsonify({"ok": True})


# ── Init ────────────────────────────────────────────────────────────────────
with app.app_context():
    db.create_all()
    # ── Migration: garantir colunas novas existam ──────────────────────────
    try:
        from sqlalchemy import text, inspect
        inspector = inspect(db.engine)
        # Movel: fotos_json
        movel_cols = [col['name'] for col in inspector.get_columns('movel')]
        if 'fotos_json' not in movel_cols:
            with db.engine.connect() as conn:
                conn.execute(text("ALTER TABLE movel ADD COLUMN fotos_json TEXT"))
                conn.commit()
        if 'foto_base64' in movel_cols and 'fotos_json' in movel_cols:
            with db.engine.connect() as conn:
                # Migrar foto_base64 → fotos_json se necessário
                conn.execute(text("""
                    UPDATE movel SET fotos_json = '[' || '"' || foto_base64 || '"' || ']'
                    WHERE foto_base64 IS NOT NULL AND foto_base64 != ''
                    AND (fotos_json IS NULL OR fotos_json = '' OR fotos_json = '[]')
                """))
                conn.commit()
        # Pedido: whatsapp_cliente, itens_json
        pedido_cols = [col['name'] for col in inspector.get_columns('pedido')]
        with db.engine.connect() as conn:
            if 'whatsapp_cliente' not in pedido_cols:
                conn.execute(text("ALTER TABLE pedido ADD COLUMN whatsapp_cliente VARCHAR(20)"))
                conn.commit()
            if 'itens_json' not in pedido_cols:
                conn.execute(text("ALTER TABLE pedido ADD COLUMN itens_json TEXT"))
                conn.commit()
    except Exception as e:
        print(f"Migration info: {e}")

if __name__ == "__main__":
    app.run(debug=True, port=5002)
