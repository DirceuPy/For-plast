import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import date
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "estoque.db"
HOST = "127.0.0.1"
PORT = 8000
MAX_REQUEST_SIZE = 4096
DATABASE_LOCK = Lock()

PRODUCTS = [
    "CORPO INTERNO 6L PREMIUM",
    "DIVISÓRIA GAVETEIRO",
    "FRENTE - LISA",
    "FRENTE - RATAN",
    "GAVETA MINI GAVETEIRO",
    "MESA MONOBLOCO",
    "MESA QUADRADA",
    "MESA REDONDA",
    "MESA RETANGULAR",
    "OMBREIRA",
    "ORGANIZADOR 10L",
    "ORGANIZADOR 16L",
    "ORGANIZADOR 25L",
    "ORGANIZADORA 10L C/TAMPA",
    "ORGANIZADORA 16L C/TAMPA",
    "ORGANIZADORA 25L C/TAMPA",
    "PERNA MESA",
    "PÉS COM RODÍZIO",
    "POLTRONA BOL",
    "POLTRONA PALHINHA",
    "POLTRONA TROPICAL",
    "PORTA GAVETA",
    "PUXADOR 34L ANTIGO",
    "TAMPA LIXEIRA 100L",
    "TAMPA LIXEIRA 15L",
    "TAMPA LIXEIRA 50L",
    "TAMPA MINI GAVETEIRO",
    "TAMPA SUPERIOR GAVETEIRO",
    "TAMPA TÉRMICA 15L PREMIUM",
    "TAMPA TÉRMICA 34L PREMIUM",
    "TAMPA TÉRMICA 6L PREMIUM",
    "TAMPA ORGANIZADORA 25L, 16L",
]


def connect_database():
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def database_session():
    connection = connect_database()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database():
    with DATABASE_LOCK, database_session() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS movements (
                id INTEGER PRIMARY KEY,
                product_id INTEGER NOT NULL REFERENCES products(id),
                movement_date TEXT NOT NULL,
                movement_type TEXT NOT NULL
                    CHECK (movement_type IN ('entrada', 'retirada')),
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS movements_product_idx
                ON movements(product_id);
            CREATE INDEX IF NOT EXISTS movements_date_idx
                ON movements(movement_date, id);
            """
        )
        connection.executemany(
            "INSERT OR IGNORE INTO products (name) VALUES (?)",
            [(product,) for product in PRODUCTS],
        )


class InventoryHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Tamanho da solicitação inválido.") from error
        if content_length < 1 or content_length > MAX_REQUEST_SIZE:
            raise ValueError("Tamanho da solicitação inválido.")
        try:
            payload = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError("Envie os dados em formato JSON válido.") from error
        if not isinstance(payload, dict):
            raise ValueError("Os dados enviados são inválidos.")
        return payload

    def do_GET(self):
        if urlparse(self.path).path == "/api/state":
            try:
                with database_session() as connection:
                    products = connection.execute(
                        """
                        SELECT p.name,
                            COALESCE(SUM(CASE
                                WHEN m.movement_type = 'entrada' THEN m.quantity
                                WHEN m.movement_type = 'retirada' THEN -m.quantity
                                ELSE 0
                            END), 0) AS quantity
                        FROM products p
                        LEFT JOIN movements m ON m.product_id = p.id
                        GROUP BY p.id
                        ORDER BY p.id
                        """
                    ).fetchall()
                    movements = connection.execute(
                        """
                        SELECT m.movement_date AS date, p.name AS product,
                            m.movement_type AS type, m.quantity
                        FROM movements m
                        JOIN products p ON p.id = m.product_id
                        ORDER BY m.movement_date DESC, m.id DESC
                        """
                    ).fetchall()
                self._send_json(
                    200,
                    {
                        "products": [dict(row) for row in products],
                        "movements": [dict(row) for row in movements],
                    },
                )
            except sqlite3.Error:
                logging.exception("Falha ao consultar o banco de dados.")
                self._send_json(500, {"error": "Não foi possível consultar o estoque."})
            return
        super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path != "/api/movements":
            self._send_json(404, {"error": "Endereço não encontrado."})
            return

        try:
            payload = self._read_json()
            movement_date = payload.get("date")
            product = payload.get("product")
            movement_type = payload.get("type")
            quantity = payload.get("quantity")

            if not isinstance(movement_date, str):
                raise ValueError("Informe uma data válida.")
            try:
                date.fromisoformat(movement_date)
            except ValueError as error:
                raise ValueError("Informe uma data válida.") from error
            if not isinstance(product, str) or not product.strip():
                raise ValueError("Selecione um produto.")
            if movement_type not in ("entrada", "retirada"):
                raise ValueError("Selecione entrada ou retirada.")
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
                raise ValueError("A quantidade deve ser um número inteiro maior que zero.")

            with DATABASE_LOCK, database_session() as connection:
                connection.execute("BEGIN IMMEDIATE")
                product_row = connection.execute(
                    "SELECT id FROM products WHERE name = ?",
                    (product,),
                ).fetchone()
                if product_row is None:
                    raise ValueError("O produto selecionado não está cadastrado.")

                if movement_type == "retirada":
                    stock_row = connection.execute(
                        """
                        SELECT COALESCE(SUM(CASE
                            WHEN movement_type = 'entrada' THEN quantity
                            ELSE -quantity
                        END), 0) AS quantity
                        FROM movements
                        WHERE product_id = ?
                        """,
                        (product_row["id"],),
                    ).fetchone()
                    if quantity > stock_row["quantity"]:
                        raise ValueError(
                            "Estoque insuficiente. Saldo disponível: "
                            f"{stock_row['quantity']} unidade(s)."
                        )

                connection.execute(
                    """
                    INSERT INTO movements
                        (product_id, movement_date, movement_type, quantity)
                    VALUES (?, ?, ?, ?)
                    """,
                    (product_row["id"], movement_date, movement_type, quantity),
                )
            self._send_json(201, {"message": "Movimentação registrada com sucesso."})
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
        except sqlite3.Error:
            logging.exception("Falha ao salvar movimentação no banco de dados.")
            self._send_json(500, {"error": "Não foi possível salvar a movimentação."})

    def log_message(self, format_string, *args):
        logging.info("%s - %s", self.address_string(), format_string % args)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    initialize_database()
    server = ThreadingHTTPServer((HOST, PORT), InventoryHandler)
    print(f"Controle de estoque: http://{HOST}:{PORT}/Estq.html")
    print(f"Banco de dados: {DATABASE_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor encerrado.")
    finally:
        server.server_close()
