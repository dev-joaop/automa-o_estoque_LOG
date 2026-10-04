# Doca — controle de recebimento de cargas

## Como rodar
```
pip install -r requirements.txt
python app.py
```
Abra http://localhost:5000 — login inicial: `admin` / `admin123` (troque!).
Do celular (mesmo Wi-Fi): http://IP-DO-SEU-PC:5000

Criar outro usuário: `flask --app app criar-usuario`

## Como funciona
- **Movimentações, não números soltos**: cada recebimento/saída/ajuste é uma linha na tabela `movimentacoes`. O saldo é a soma. O histórico vem de graça.
- **Estorno em vez de apagar**: lançamento errado gera um lançamento oposto, mantendo a auditoria.
- **Divergência**: informe o "esperado na NF" na entrada; se for diferente do recebido, o histórico destaca.
- **Excel**: botão "Baixar Excel" no histórico (respeita filtros) + aba com saldo atual. O banco real é o SQLite (`recebimento.db`).

## Antes de usar de verdade
- Defina `SECRET_KEY` (variável de ambiente) e troque a senha do admin.
- Faça backup periódico do arquivo `recebimento.db` e da pasta `static/uploads`.
- Para vários usuários simultâneos na empresa, hospedar com gunicorn/waitress (não o servidor de desenvolvimento) e HTTPS.
